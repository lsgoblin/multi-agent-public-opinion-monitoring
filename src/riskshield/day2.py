"""Day 2 source observations and a small, evidence-bound graph retrieval path."""

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
    adapter: str = Field(pattern=r"^(sec_filing|arctic_shift_post)$")
    post_id: str | None = Field(default=None, pattern=r"^[a-z0-9]{5,12}$")


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


def _fetch(url):
    try:
        response = httpx.get(url, timeout=15, follow_redirects=False,
                             headers={"User-Agent": "RiskShieldDay2/0.1 public-source-verification"})
        if response.status_code != 200:
            raise CollectionError(f"公开来源返回 HTTP {response.status_code}。")
        if len(response.content) > 1_000_000:
            raise CollectionError("来源响应超过 1 MB 限制。")
        return response
    except httpx.HTTPError:
        raise CollectionError("公开来源请求失败。") from None


class Day2Pipeline:
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
        source_path = _sec_path(record.source_url)
        if request.adapter == "sec_filing":
            if request.post_id is not None:
                raise CollectionError("SEC 适配器不使用 post_id。")
            source_url = str(record.source_url)
            response = _fetch(source_url)
            content = response.content
            source_key = source_path
            title = record.title
            source_observed_at = None
        else:
            if request.post_id is None:
                raise CollectionError("帖子存档适配器需要 post_id。")
            source_url = f"https://arctic-shift.photon-reddit.com/api/posts/ids?ids={request.post_id}"
            response = _fetch(source_url)
            try:
                posts = response.json()["data"]
                post = next(p for p in posts if p.get("id") == request.post_id)
                target = str(post["url"])
                target_parsed = urlparse(target)
                if (target_parsed.scheme != "https" or target_parsed.hostname != "www.sec.gov"
                        or target_parsed.username or target_parsed.password):
                    raise CollectionError("存档目标不是 SEC 原件链接。")
                linked = parse_qs(target_parsed.query).get("doc", [target])[0]
                if _sec_path("https://www.sec.gov" + linked if linked.startswith("/") else linked) != source_path:
                    raise CollectionError("存档帖子没有指向该输入的 SEC 原件。")
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
        witnessed_ids = {row["record_id"] for row in observations
                         if row["adapter"] == "arctic_shift_post" and row["source_observed_at"]
                         and datetime.fromisoformat(row["source_observed_at"]) <= cutoff}
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
            nodes.extend([{"id": source_id, "type": "source", "label": row["title"]},
                          {"id": claim_id, "type": "claim", "label": row["summary"]},
                          {"id": publisher_id, "type": "publisher", "label": row["publisher"]}])
            edges.extend([{"from": event_id, "to": claim_id, "relation": "has_claim"},
                          {"from": claim_id, "to": source_id, "relation": "supported_by"},
                          {"from": source_id, "to": publisher_id, "relation": "published_by"}])
            claims.append({"claim_id": claim_id, "record_id": row["record_id"],
                           "text": row["summary"], "title": row["title"],
                           "source_url": row["source_url"], "available_at": row["available_at"],
                           "observation_ids": [o["observation_id"] for o in observations
                                               if o["record_id"] == row["record_id"]]})
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
        selected = {hit["claim_id"] for hit in hits[:5]}
        edges = [edge for edge in graph["edges"] if edge["from"] in selected or edge["to"] in selected]
        node_ids = {edge[key] for edge in edges for key in ("from", "to")}
        return {"graph_id": graph_id, "question": question, "cutoff": graph["cutoff"],
                "hits": hits[:5], "nodes": [node for node in graph["nodes"] if node["id"] in node_ids],
                "edges": edges, "retrieval_kind": "lexical_graph_neighborhood_with_source_citations"}
