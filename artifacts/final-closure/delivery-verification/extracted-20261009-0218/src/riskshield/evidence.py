"""Source observations and a small, evidence-bound graph retrieval path."""

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from riskshield.store import Store


class CollectionError(ValueError):
    pass


class CollectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", max_length=100)
    adapter: str = Field(pattern=r"^(sec_filing|arctic_shift_post|optum_status_update)$")
    post_id: str | None = Field(default=None, pattern=r"^[a-z0-9]{5,12}$")
    update_id: str | None = Field(default=None, pattern=r"^[a-z0-9]{12}$")


class LabelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", max_length=100)
    label: str = Field(pattern=r"^(positive|neutral|negative)$")
    reviewer: str = Field(min_length=1, max_length=100)
    rationale: str = Field(min_length=1, max_length=500)


class GraphQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=2, max_length=300)


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _sec_path(url):
    parsed = urlparse(str(url))
    if (parsed.scheme != "https" or parsed.hostname != "www.sec.gov" or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.fragment
            or not parsed.path.startswith("/Archives/edgar/data/")):
        raise CollectionError("SEC 适配器只接受 sec.gov 的 EDGAR 原件。")
    return parsed.path.replace("/data/0000731766/", "/data/731766/")


def _status_path(url):
    parsed = urlparse(str(url))
    if (parsed.scheme != "https" or parsed.hostname not in
            {"status.changehealthcare.com", "solution-status.optum.com"}
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment or
            parsed.path.rstrip("/") != "/incidents/hqpjz25fn3n7"):
        raise CollectionError("状态页适配器只接受已审计的 Optum 事件页。")
    return "/incidents/hqpjz25fn3n7"


def _fetch(url, *, max_bytes=1_000_000):
    try:
        response = httpx.get(url, timeout=15, follow_redirects=False,
                             headers={"User-Agent": "RiskShield/0.1 public-source-verification"})
        if response.status_code != 200:
            raise CollectionError(f"公开来源返回 HTTP {response.status_code}。")
        if len(response.content) > max_bytes:
            raise CollectionError("来源响应超过该适配器的大小限制。")
        return response
    except httpx.HTTPError:
        raise CollectionError("公开来源请求失败。") from None


def _filing_content(content: bytes) -> bytes:
    """Hash filing markup without request-specific script and pixel tags."""
    return re.sub(rb"<(?:script|noscript)\b[^>]*>.*?</(?:script|noscript)\s*>", b"", content,
                  flags=re.IGNORECASE | re.DOTALL)


class EvidencePipeline:
    def __init__(self, store: Store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS observations (
                    observation_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
                    record_id TEXT NOT NULL, adapter TEXT NOT NULL,
                    source_key TEXT NOT NULL, source_url TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL, title TEXT,
                    source_observed_at TEXT, first_collected_at TEXT NOT NULL,
                    last_collected_at TEXT NOT NULL, fetch_count INTEGER NOT NULL,
                    UNIQUE(case_id, record_id, adapter, source_key, content_sha256)
                );
                CREATE TABLE IF NOT EXISTS manual_labels (
                    case_id TEXT NOT NULL, record_id TEXT NOT NULL,
                    reviewer TEXT NOT NULL, label TEXT NOT NULL,
                    rationale TEXT NOT NULL, labeled_at TEXT NOT NULL,
                    PRIMARY KEY(case_id, record_id, reviewer)
                );
                CREATE TABLE IF NOT EXISTS graphs (
                    graph_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
                    cutoff TEXT NOT NULL, version TEXT NOT NULL,
                    built_at TEXT NOT NULL, body TEXT NOT NULL
                );
            """)

    def _record(self, case_id, record_id):
        rows = self.store.records(case_id)
        record = next((row for row in rows if row.record_id == record_id), None)
        if record is None:
            raise KeyError(record_id)
        if record.role != "input_candidate":
            raise CollectionError("仅对候选输入采集公开来源；后续评测材料不得进入构图路径。")
        return record

    def collect(self, case_id: str, request: CollectRequest):
        record = self._record(case_id, request.record_id)
        source_kind = "sec" if urlparse(str(record.source_url)).hostname == "www.sec.gov" else "status"
        source_path = (_sec_path(record.source_url) if source_kind == "sec"
                       else _status_path(record.source_url))
        if request.adapter == "sec_filing":
            if source_kind != "sec" or request.post_id is not None or request.update_id is not None:
                raise CollectionError("SEC 适配器只接受 SEC 记录，不使用 post_id 或 update_id。")
            source_url = str(record.source_url)
            response = _fetch(source_url)
            content = _filing_content(response.content)
            source_key = source_path
            title = record.title
            source_observed_at = None
        elif request.adapter == "optum_status_update":
            if (source_kind != "status" or request.update_id != "9j1b644m46kx"
                    or request.post_id is not None):
                raise CollectionError("状态页适配器只接受已审计事件的指定 update_id。")
            source_url = "https://solution-status.optum.com/api/v2/incidents/hqpjz25fn3n7.json"
            response = _fetch(source_url, max_bytes=2_000_000)
            try:
                incident = response.json()["incident"]
                if incident["id"] != "hqpjz25fn3n7":
                    raise ValueError
                update = next(u for u in incident["incident_updates"]
                              if u.get("id") == request.update_id)
                cutoff = datetime.fromisoformat(self.store.snapshot(case_id)["cutoff"])
                times = [datetime.fromisoformat(update[key]) for key in
                         ("created_at", "updated_at", "display_at")]
                if any(value > cutoff for value in times) or not isinstance(update["body"], str):
                    raise ValueError
            except (KeyError, StopIteration, TypeError, ValueError):
                raise CollectionError("状态更新缺少可核对时间、正文，或版本晚于 cutoff。") from None
            content = json.dumps({key: update[key] for key in
                                  ("id", "body", "created_at", "updated_at", "display_at")},
                                 sort_keys=True).encode()
            source_key = request.update_id
            title = record.title
            source_observed_at = update["updated_at"]
        else:
            if request.post_id is None or request.update_id is not None:
                raise CollectionError("帖子存档适配器需要 post_id，且不使用 update_id。")
            source_url = f"https://arctic-shift.photon-reddit.com/api/posts/ids?ids={request.post_id}"
            response = _fetch(source_url)
            try:
                posts = response.json()["data"]
                post = next(p for p in posts if p.get("id") == request.post_id)
                target = str(post["url"])
                target_parsed = urlparse(target)
                if source_kind == "sec":
                    if (target_parsed.scheme != "https" or target_parsed.hostname != "www.sec.gov"
                            or target_parsed.username or target_parsed.password):
                        raise CollectionError("存档目标不是 SEC 原件链接。")
                    linked = parse_qs(target_parsed.query).get("doc", [target])[0]
                    matched = _sec_path("https://www.sec.gov" + linked
                                        if linked.startswith("/") else linked) == source_path
                else:
                    matched = _status_path(target) == source_path
                if not matched:
                    raise CollectionError("存档帖子没有指向该输入的已审计原件。")
                retrieved = int(post["retrieved_on"])
                if retrieved < 0 or not isinstance(post["title"], str):
                    raise ValueError
            except (KeyError, StopIteration, TypeError, ValueError):
                raise CollectionError("帖子存档缺少可核对的链接、标题或抓取时间。") from None
            # Only retain the audited public metadata, never account names or comments.
            content = json.dumps({"id": request.post_id, "title": post["title"],
                                  "url": target, "retrieved_on": retrieved}, sort_keys=True).encode()
            source_key = request.post_id
            title = post["title"][:300]
            source_observed_at = datetime.fromtimestamp(retrieved, timezone.utc).isoformat()

        digest = hashlib.sha256(content).hexdigest()
        observation_id = hashlib.sha256(
            f"{case_id}|{record.record_id}|{request.adapter}|{source_key}|{digest}".encode()
        ).hexdigest()[:24]
        now = _utc_now()
        with self.store.connect() as db:
            db.execute("""INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(case_id, record_id, adapter, source_key, content_sha256)
                DO UPDATE SET last_collected_at=excluded.last_collected_at,
                              fetch_count=fetch_count+1""",
                       (observation_id, case_id, record.record_id, request.adapter, source_key,
                        source_url, digest, title, source_observed_at, now, now))
            row = db.execute("SELECT * FROM observations WHERE observation_id=?", (observation_id,)).fetchone()
        return {**dict(row), "realtime_latency_seconds": None,
                "latency_note": "历史来源没有首次在线可访问与发现时间，不能据此计算实时采集延迟。"}

    def observations(self, case_id):
        self.store.get_case(case_id)
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM observations WHERE case_id=? ORDER BY record_id, adapter", (case_id,))
            return [dict(row) for row in rows]

    def label(self, case_id, request: LabelRequest):
        self._record(case_id, request.record_id)
        with self.store.connect() as db:
            existing = db.execute("""SELECT label, rationale FROM manual_labels
                WHERE case_id=? AND record_id=? AND reviewer=?""",
                (case_id, request.record_id, request.reviewer)).fetchone()
            if existing and (existing["label"] != request.label or existing["rationale"] != request.rationale):
                raise CollectionError("已有该复核者的不同标签；请保留原记录并使用新复核版本标识。")
            if not existing:
                db.execute("INSERT INTO manual_labels VALUES (?, ?, ?, ?, ?, ?)",
                           (case_id, request.record_id, request.reviewer, request.label,
                            request.rationale, _utc_now()))
        return {"status": "recorded", "label_kind": "submitted_label_identity_unverified_not_formal_truth"}

    def labels(self, case_id):
        self.store.get_case(case_id)
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM manual_labels WHERE case_id=? ORDER BY record_id, reviewer", (case_id,))
            return [dict(row) for row in rows]

    def build_graph(self, case_id):
        snapshot = self.store.snapshot(case_id)
        observations = self.observations(case_id)
        cutoff = datetime.fromisoformat(snapshot["cutoff"])
        eligible = [row for row in observations
                    if row["adapter"] == "sec_filing" or
                    (row["adapter"] in {"arctic_shift_post", "optum_status_update"}
                     and row["source_observed_at"] and
                     datetime.fromisoformat(row["source_observed_at"]) <= cutoff)]
        witnessed_ids = {row["record_id"] for row in eligible if row["adapter"] == "arctic_shift_post"}
        records = [row for row in snapshot["records"] if row["record_id"] in witnessed_ids]
        if not records:
            raise CollectionError("没有同时具备 cutoff 合格输入与截止前独立公开见证的材料。")
        event_id = "event:" + case_id
        nodes = [{"id": event_id, "type": "event", "label": self.store.get_case(case_id)["title"]}]
        edges = []
        claims = []
        for row in records:
            source_id = "source:" + row["record_id"]
            claim_id = "claim:" + row["record_id"]
            publisher_id = "publisher:" + hashlib.sha256(row["publisher"].encode()).hexdigest()[:12]
            source_observations = [o for o in eligible if o["record_id"] == row["record_id"]]
            observation_nodes = [{"id": "observation:" + o["observation_id"], "type": "observation",
                                  "adapter": o["adapter"], "source_url": o["source_url"],
                                  "content_sha256": o["content_sha256"],
                                  "source_observed_at": o["source_observed_at"],
                                  "first_collected_at": o["first_collected_at"]}
                                 for o in source_observations]
            nodes.extend([{"id": source_id, "type": "source", "label": row["title"]},
                          {"id": claim_id, "type": "claim", "label": row["summary"]},
                          {"id": publisher_id, "type": "publisher", "label": row["publisher"]},
                          *observation_nodes])
            edges.extend([{"from": event_id, "to": claim_id, "relation": "has_claim"},
                          {"from": claim_id, "to": source_id, "relation": "supported_by"},
                          {"from": source_id, "to": publisher_id, "relation": "published_by"},
                          *[{"from": source_id, "to": o["id"], "relation": "observed_in"}
                            for o in observation_nodes]])
            claims.append({"claim_id": claim_id, "record_id": row["record_id"],
                           "text": row["summary"], "title": row["title"],
                           "source_url": row["source_url"], "available_at": row["available_at"],
                           "source_node_id": source_id, "publisher_node_id": publisher_id,
                           "observation_ids": [o["observation_id"] for o in source_observations],
                           "observation_node_ids": [o["id"] for o in observation_nodes],
                           "evidence_adapters": sorted({o["adapter"] for o in source_observations})})
        body = {"case_id": case_id, "cutoff": snapshot["cutoff"],
                "source_version": snapshot["version"], "nodes": nodes, "edges": edges,
                "claims": claims, "excluded_counts": snapshot["excluded_counts"]}
        graph_id = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:24]
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO graphs VALUES (?, ?, ?, ?, ?, ?)",
                       (graph_id, case_id, snapshot["cutoff"], snapshot["version"],
                        _utc_now(), json.dumps(body, ensure_ascii=False)))
        return {"graph_id": graph_id, **body}

    def graph(self, graph_id):
        with self.store.connect() as db:
            row = db.execute("SELECT body FROM graphs WHERE graph_id=?", (graph_id,)).fetchone()
        if row is None:
            raise KeyError(graph_id)
        return {"graph_id": graph_id, **json.loads(row["body"])}

    def query_graph(self, graph_id, question):
        graph = self.graph(graph_id)
        tokens = [token.lower() for token in re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]+", question)
                  if len(token) > 2]
        hits = []
        for claim in graph["claims"]:
            haystack = (claim["title"] + " " + claim["text"]).lower()
            score = sum(token in haystack for token in tokens)
            if score and datetime.fromisoformat(claim["available_at"]) <= datetime.fromisoformat(graph["cutoff"]):
                hits.append({**claim, "score": score})
        hits.sort(key=lambda item: (-item["score"], item["record_id"]))
        node_ids = {"event:" + graph["case_id"]}
        for hit in hits[:5]:
            node_ids.update([hit["claim_id"], hit["source_node_id"], hit["publisher_node_id"]])
            node_ids.update(hit["observation_node_ids"])
        edges = [edge for edge in graph["edges"]
                 if edge["from"] in node_ids and edge["to"] in node_ids]
        return {"graph_id": graph_id, "question": question, "cutoff": graph["cutoff"],
                "hits": hits[:5], "nodes": [node for node in graph["nodes"] if node["id"] in node_ids],
                "edges": edges, "retrieval_kind": "lexical_graph_neighborhood_with_source_citations"}


# Legacy class name for callers still importing the former Day 2 API.
Day2Pipeline = EvidencePipeline
