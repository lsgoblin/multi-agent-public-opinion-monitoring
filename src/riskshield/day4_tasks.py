"""Persisted Day 4 offline task execution.

The local dynamic substitute accepts synthetic runs and strictly cutoff-checked
real historical cases. It performs no model calls, network requests,
notification delivery, or deployment.
"""

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from riskshield.day2 import CollectionError, Day2Pipeline
from riskshield.day3 import Day3Simulation, DecisionResult, SimulationError
from riskshield.day4_alerts import AlertError, Day4Alerts
from riskshield.day4_report import Day4Reports, ReportError
from riskshield.schemas import AgentDecision, CaseImport
from riskshield.store import ConflictError, Store


TASK_MODE = "offline_dynamic_substitute"
DEMO_CASE_ID = "day4_demo_fictional_synthetic_v1"
DEMO_GRAPH_ID = "day4_demo_fictional_synthetic_graph_v1"
DEMO_RECORD_ID = "day4_demo_fictional_notice_v1"

_FAILURE_CATEGORIES = frozenset({
    "simulation_error",
    "alert_error",
    "report_error",
    "data_integrity_error",
    "unexpected_error",
    "interrupted_on_restart",
    "thread_start_error",
})


class TaskError(ValueError):
    """A validation or state transition error in the local task runner."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _seconds_between(start: str, end: str) -> float:
    start_at, end_at = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if (start_at.tzinfo is None or start_at.utcoffset() is None
            or end_at.tzinfo is None or end_at.utcoffset() is None):
        raise TaskError("task timestamps must include a timezone")
    return round((end_at - start_at).total_seconds(), 6)


def _failure_category(exc: Exception) -> str:
    if isinstance(exc, TaskError):
        return "data_integrity_error"
    if isinstance(exc, SimulationError):
        return "simulation_error"
    if isinstance(exc, AlertError):
        return "alert_error"
    if isinstance(exc, ReportError):
        return "report_error"
    if isinstance(exc, (KeyError, ConflictError, json.JSONDecodeError)):
        return "data_integrity_error"
    return "unexpected_error"


class OfflineDynamicSubstitute:
    """Local observation-driven backend used for offline engineering evidence."""

    mode = "test_substitute"
    reservation_cny = Decimal("0")
    input_cny_per_million = Decimal("0")
    output_cny_per_million = Decimal("0")
    price_version = "offline-zero-cost-v1"
    model = TASK_MODE

    @staticmethod
    def describe() -> dict:
        return {
            "type": TASK_MODE,
            "decision_basis": "current_state_persona_runtime_messages_and_own_memory",
            "model_calls": 0,
            "network_requests": 0,
        }

    def decide(self, observation: dict) -> DecisionResult:
        agent_id = observation["agent_id"]
        messages = observation["messages"]
        memories = observation["memories"]
        persona = observation["persona"]
        state = observation["state"]

        if messages:
            message = sorted(messages, key=lambda item: item["message_id"])[0]
            if state["emotion"] <= -0.45 and persona["role"] == "customer":
                action = "express_complaint_intent"
                reason = "The current negative state and a new runtime message trigger complaint intent."
            else:
                action = "comment"
                reason = "A newly received runtime message changes this agent's current response."
            reference = message["message_id"]
        elif memories:
            memory = sorted(memories, key=lambda item: (item["round"], item["memory_id"]),
                            reverse=True)[0]
            action = "seek_clarification"
            reference = memory["memory_id"]
            reason = "This agent's own persisted memory prompts a clarification request."
        else:
            evidence = observation["evidence"][0]
            reference = evidence["evidence_id"]
            if persona["role"] == "netizen":
                action = "share"
                reason = "This persona shares the currently visible fictional source evidence."
            elif persona["role"] == "customer" and state["emotion"] < 0:
                action = "comment"
                reason = "This customer's current state prompts a comment on visible evidence."
            else:
                action = "observe"
                reason = "This persona observes the fictional evidence before receiving social context."

        return DecisionResult(
            AgentDecision(agent_id=agent_id, action=action,
                          evidence_ids=[reference], reason=reason),
            usage={"prompt_tokens": 0, "completion_tokens": 0},
            model=TASK_MODE,
        )


class Day4Tasks:
    """Queue and execute persisted synthetic Day 4 jobs on the local machine."""

    def __init__(self, store: Store):
        self.store = store
        self.simulation = Day3Simulation(store)
        self.alerts = Day4Alerts(store)
        self.reports = Day4Reports(store)
        self.day2 = Day2Pipeline(store)
        self._threads: dict[str, threading.Thread] = {}
        self._threads_lock = threading.Lock()
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS day4_tasks (
                    job_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'complete', 'failed')),
                    mode TEXT NOT NULL,
                    queued_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL,
                    alert_id TEXT,
                    report_id TEXT,
                    failure_category TEXT,
                    model_calls INTEGER NOT NULL DEFAULT 0,
                    network_requests INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS day4_tasks_status
                    ON day4_tasks(status, queued_at);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(day4_tasks)")}
            for name, declaration in (
                    ("input_received_at", "TEXT"),
                    ("report_generated_at", "TEXT"),
                    ("elapsed_seconds", "REAL")):
                if name not in columns:
                    db.execute(f"ALTER TABLE day4_tasks ADD COLUMN {name} {declaration}")
            timestamp = _now()
            db.execute(
                "UPDATE day4_tasks SET status='failed', completed_at=?, updated_at=?, "
                "failure_category='interrupted_on_restart' "
                "WHERE status IN ('queued', 'running')",
                (timestamp, timestamp),
            )

    def _row(self, job_id: str) -> dict:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM day4_tasks WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def _present(self, row: dict) -> dict:
        try:
            summary = self.simulation.summary(row["run_id"])
        except KeyError:
            summary = None
        target_rounds = summary["config"]["rounds"] if summary else None
        completed_rounds = summary["completed_rounds"] if summary else None
        simulation_status = summary["status"] if summary else "missing"
        progress = (min(1.0, completed_rounds / target_rounds)
                    if completed_rounds is not None and target_rounds else 0.0)
        return {
            **row,
            "case_id": summary["case_id"] if summary else None,
            "graph_id": summary["graph_id"] if summary else None,
            "data_mode": summary["config"].get("data_mode") if summary else None,
            "execution_mode": TASK_MODE,
            "completed_rounds": completed_rounds,
            "target_rounds": target_rounds,
            "simulation_status": simulation_status,
            "progress": progress,
            "error": row["failure_category"],
        }

    def get(self, job_id: str) -> dict:
        return self._present(self._row(job_id))

    def list(self) -> list[dict]:
        with self.store.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM day4_tasks ORDER BY queued_at DESC, job_id DESC"
            )]
        return [self._present(row) for row in rows]

    def _validate_run_evidence(self, run: dict, metadata: dict | None = None) -> dict:
        """Validate every graph claim before any observation reaches an Agent."""
        try:
            metadata = metadata or self.store.get_case(run["case_id"])
            snapshot = self.store.snapshot(run["case_id"])
            graph = self.day2.graph(run["graph_id"])
            config = json.loads(run["config"])
            cutoff = datetime.fromisoformat(snapshot["cutoff"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise TaskError("case, graph, or run evidence is unavailable") from None
        if (metadata.get("data_mode") not in {"synthetic", "real_historical"}
                or config.get("data_mode") != snapshot["data_mode"]
                or graph.get("case_id") != run["case_id"]
                or graph.get("cutoff") != snapshot["cutoff"]
                or graph.get("source_version") != snapshot["version"]
                or config.get("cutoff") != snapshot["cutoff"]
                or config.get("source_version") != snapshot["version"]):
            raise TaskError("run, graph, case, cutoff, or source version do not match")
        eligible = {record["record_id"]: record for record in snapshot["records"]}
        claims = graph.get("claims")
        if not isinstance(claims, list) or not claims:
            raise TaskError("graph contains no cutoff-eligible source claims")
        seen = set()
        for claim in claims:
            if not isinstance(claim, dict):
                raise TaskError("graph claim is malformed")
            record_id = claim.get("record_id")
            record = eligible.get(record_id)
            if (record is None or record_id in seen
                    or record.get("role") != "input_candidate"
                    or record.get("available_at") is None
                    or record.get("data_mode") != snapshot["data_mode"]
                    or (snapshot["data_mode"] == "real_historical"
                        and record.get("historical_integrity") != "archived")):
                raise TaskError("graph contains unknown, unverified, or non-input evidence")
            try:
                available_at = datetime.fromisoformat(record["available_at"])
                published_at = datetime.fromisoformat(record["published_at"])
                claim_available_at = datetime.fromisoformat(claim["available_at"])
            except (KeyError, TypeError, ValueError):
                raise TaskError("graph source time is missing or malformed") from None
            if (available_at.tzinfo is None or available_at.utcoffset() is None
                    or published_at.tzinfo is None or published_at.utcoffset() is None
                    or claim_available_at.tzinfo is None or claim_available_at.utcoffset() is None
                    or available_at > cutoff or published_at > cutoff
                    or claim_available_at != available_at
                    or claim.get("text") != record.get("summary")
                    or claim.get("title") != record.get("title")
                    or claim.get("source_url") != record.get("source_url")):
                raise TaskError("graph contains future, altered, or cutoff-ineligible evidence")
            seen.add(record_id)
        return {"snapshot": snapshot, "graph": graph, "config": config}

    def _queue(self, run_id: str, *, input_received_at: str | None = None) -> tuple[dict, bool]:
        timestamp = _now()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM day4_tasks WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing is not None:
                return dict(existing), False
            run = db.execute(
                "SELECT simulation_runs.*, cases.metadata AS case_metadata "
                "FROM simulation_runs JOIN cases ON cases.case_id=simulation_runs.case_id "
                "WHERE simulation_runs.run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            config = json.loads(run["config"])
            metadata = json.loads(run["case_metadata"])
            if run["status"] != "created":
                raise TaskError("Day 4 task requires a newly created simulation run")
            if config.get("data_mode") != metadata.get("data_mode"):
                raise TaskError("simulation and case data modes do not match")
            received = input_received_at or run["created_at"] or timestamp
            job_id = "job_" + uuid.uuid4().hex[:20]
            db.execute(
                "INSERT INTO day4_tasks "
                "(job_id, run_id, status, mode, queued_at, updated_at, model_calls, "
                "network_requests, input_received_at) "
                "VALUES (?, ?, 'queued', ?, ?, ?, 0, 0, ?)",
                (job_id, run_id, TASK_MODE, timestamp, timestamp, received),
            )
            row = db.execute("SELECT * FROM day4_tasks WHERE job_id=?", (job_id,)).fetchone()
        return dict(row), True

    def _launch(self, row: dict, created: bool) -> dict:
        if not created:
            return self._present(row)
        job_id = row["job_id"]
        thread = threading.Thread(
            target=self._execute, args=(job_id,), daemon=True,
            name=f"day4-offline-{job_id}",
        )
        with self._threads_lock:
            self._threads[job_id] = thread
        try:
            thread.start()
        except Exception:
            timestamp = _now()
            with self.store.connect() as db:
                db.execute(
                    "UPDATE day4_tasks SET status='failed', completed_at=?, updated_at=?, "
                    "failure_category='thread_start_error' WHERE job_id=? AND status='queued'",
                    (timestamp, timestamp, job_id),
                )
            with self._threads_lock:
                self._threads.pop(job_id, None)
        return self.get(job_id)

    def start(self, run_id: str, *, input_received_at: str | None = None) -> dict:
        """Idempotently queue ``run_id`` and start one daemon worker."""
        row, created = self._queue(run_id, input_received_at=input_received_at)
        return self._launch(row, created)

    def _build_archived_projection(self, case_id: str) -> dict:
        """Build a labeled local evidence projection when no archived observation rows exist."""
        snapshot = self.store.snapshot(case_id)
        metadata = self.store.get_case(case_id)
        if snapshot["data_mode"] != "real_historical":
            raise TaskError("archived source projection is only for real historical cases")
        records = [record for record in snapshot["records"]
                   if record["historical_integrity"] == "archived"
                   and record["available_at"] is not None]
        if not records:
            raise TaskError("case has no archived input with a known cutoff visibility time")
        event_id = "event:" + case_id
        nodes = [{"id": event_id, "type": "event", "label": metadata["title"]}]
        edges, claims = [], []
        for record in records:
            source_id = "source:" + record["record_id"]
            claim_id = "claim:" + record["record_id"]
            publisher_id = "publisher:" + hashlib.sha256(
                record["publisher"].encode("utf-8")).hexdigest()[:12]
            nodes.extend([
                {"id": source_id, "type": "source", "label": record["title"]},
                {"id": claim_id, "type": "claim", "label": record["summary"]},
                {"id": publisher_id, "type": "publisher", "label": record["publisher"]},
            ])
            edges.extend([
                {"from": event_id, "to": claim_id, "relation": "has_claim"},
                {"from": claim_id, "to": source_id, "relation": "supported_by"},
                {"from": source_id, "to": publisher_id, "relation": "published_by"},
            ])
            claims.append({
                "claim_id": claim_id, "record_id": record["record_id"],
                "text": record["summary"], "title": record["title"],
                "source_url": record["source_url"], "available_at": record["available_at"],
                "source_node_id": source_id, "publisher_node_id": publisher_id,
                "observation_ids": [], "observation_node_ids": [],
                "evidence_adapters": ["local_archived_record_projection"],
                "availability_basis": record["availability_basis"],
                "historical_integrity": record["historical_integrity"],
                "acquisition_method": record["acquisition_method"],
                "published_at": record["published_at"],
                "collected_at": record["collected_at"],
            })
        body = {
            "case_id": case_id, "cutoff": snapshot["cutoff"],
            "source_version": snapshot["version"], "data_mode": snapshot["data_mode"],
            "build_mode": "offline_archived_record_projection",
            "nodes": nodes, "edges": edges, "claims": claims,
            "excluded_counts": snapshot["excluded_counts"],
        }
        graph_id = hashlib.sha256(_dump(body).encode("utf-8")).hexdigest()[:24]
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO graphs VALUES (?, ?, ?, ?, ?, ?)", (
                graph_id, case_id, snapshot["cutoff"], snapshot["version"], _now(), _dump(body)))
            saved = db.execute("SELECT body, case_id, cutoff, version FROM graphs "
                               "WHERE graph_id=?", (graph_id,)).fetchone()
        if (saved is None or saved["case_id"] != case_id
                or saved["cutoff"] != snapshot["cutoff"]
                or saved["version"] != snapshot["version"]
                or json.loads(saved["body"]) != body):
            raise TaskError("archived projection graph conflicts with existing content")
        return {"graph_id": graph_id, **body}

    def start_historical_case(self, case_id: str, *, agent_count: int = 10,
                              rounds: int = 3, concurrency: int = 4,
                              budget_cny: Decimal = Decimal("5.00")) -> dict:
        """Accept one local historical case, build its cutoff graph, then run the substitute."""
        input_received_at = _now()
        try:
            metadata = self.store.get_case(case_id)
        except KeyError:
            raise
        if metadata.get("data_mode") != "real_historical":
            raise TaskError("historical Day 4 task requires a real_historical case")
        try:
            graph = self.day2.build_graph(case_id)
        except CollectionError:
            graph = self._build_archived_projection(case_id)
        run_id = self.simulation.create_run(
            case_id, graph["graph_id"], agent_count=agent_count, rounds=rounds,
            concurrency=concurrency, budget_cny=budget_cny,
        )
        return self.start(run_id, input_received_at=input_received_at)

    def run_sync(self, run_id: str, *, input_received_at: str | None = None) -> dict:
        """Deterministic synchronous entry point for local validation and tests."""
        row, created = self._queue(run_id, input_received_at=input_received_at)
        if created or row["status"] == "queued":
            self._execute(row["job_id"])
        return self.get(row["job_id"])

    def wait(self, job_id: str, timeout: float | None = None) -> dict:
        """Wait for a worker started by this instance, if one is still present."""
        with self._threads_lock:
            thread = self._threads.get(job_id)
        if thread is not None:
            thread.join(timeout)
        return self.get(job_id)

    def _execute(self, job_id: str) -> None:
        timestamp = _now()
        with self.store.connect() as db:
            changed = db.execute(
                "UPDATE day4_tasks SET status='running', started_at=?, updated_at=? "
                "WHERE job_id=? AND status='queued'",
                (timestamp, timestamp, job_id),
            ).rowcount
            row = db.execute("SELECT run_id, input_received_at FROM day4_tasks "
                             "WHERE job_id=?", (job_id,)).fetchone()
        if not changed or row is None:
            return
        run_id = row["run_id"]
        try:
            with self.store.connect() as db:
                run = db.execute("SELECT * FROM simulation_runs WHERE run_id=?", (run_id,)).fetchone()
                case_row = db.execute("SELECT metadata FROM cases WHERE case_id=?",
                                      (run["case_id"],)).fetchone() if run else None
            if run is None or case_row is None:
                raise TaskError("case or simulation run disappeared before execution")
            self._validate_run_evidence(dict(run), json.loads(case_row["metadata"]))
            result = self.simulation.advance(run_id, OfflineDynamicSubstitute())
            if result["status"] != "complete":
                raise SimulationError("offline synthetic run did not complete")
            alert = self.alerts.assess(run_id)
            report = self.reports.build(run_id, alert)
            report_generated_at = _now()
            timestamp = _now()
            with self.store.connect() as db:
                db.execute(
                    "UPDATE day4_tasks SET status='complete', completed_at=?, updated_at=?, "
                    "alert_id=?, report_id=?, report_generated_at=?, elapsed_seconds=?, "
                    "failure_category=NULL "
                    "WHERE job_id=? AND status='running'",
                    (timestamp, timestamp, alert["alert_id"], report["report_id"],
                     report_generated_at,
                     _seconds_between(row["input_received_at"], report_generated_at), job_id),
                )
        except Exception as exc:
            category = _failure_category(exc)
            if category not in _FAILURE_CATEGORIES:
                category = "unexpected_error"
            timestamp = _now()
            with self.store.connect() as db:
                db.execute(
                    "UPDATE day4_tasks SET status='failed', completed_at=?, updated_at=?, "
                    "failure_category=? WHERE job_id=? AND status='running'",
                    (timestamp, timestamp, category, job_id),
                )
        finally:
            with self._threads_lock:
                self._threads.pop(job_id, None)

    def prepare_demo_run(self, *, agent_count: int = 10, rounds: int = 3,
                         concurrency: int = 4) -> dict:
        """Create the stable fictional demo evidence and return a fresh created run."""
        case = CaseImport.model_validate({
            "case_id": DEMO_CASE_ID,
            "title": "Fictional Harborlight Insurance service exercise",
            "scope": "Synthetic Day 4 local demonstration only",
            "cutoff": "2026-10-07T09:30:00+08:00",
            "cutoff_basis": "Fictional notice visible at 09:00 plus 30 minutes.",
            "data_mode": "synthetic",
            "version": "day4-demo-synthetic-v1",
            "records": [{
                "record_id": DEMO_RECORD_ID,
                "channel": "official_status",
                "source_url": "https://example.com/fictional-harborlight-notice",
                "publisher": "Fictional Harborlight Insurance",
                "title": "Fictional exercise reports temporary service disruption",
                "summary": ("In a fictional local exercise, Harborlight Insurance reports a "
                            "temporary test-service disruption and an ongoing investigation."),
                "content_kind": "human_summary",
                "data_mode": "synthetic",
                "published_at": "2026-10-07T09:00:00+08:00",
                "available_at": "2026-10-07T09:00:00+08:00",
                "collected_at": "2026-10-07T09:01:00+08:00",
                "availability_basis": "Synthetic fixture created for the local Day 4 demo.",
                "historical_integrity": "synthetic",
                "acquisition_method": "synthetic_fixture",
                "role": "input_candidate",
                "limitations": ["Fictional event; no real company, user, or complaint data."],
            }],
        })
        self.store.import_case(case)
        snapshot = self.store.snapshot(DEMO_CASE_ID)
        source = snapshot["records"][0]
        graph = {
            "case_id": DEMO_CASE_ID,
            "cutoff": snapshot["cutoff"],
            "source_version": snapshot["version"],
            "nodes": [
                {"id": "event:" + DEMO_CASE_ID, "type": "event", "label": case.title},
                {"id": "claim:" + DEMO_RECORD_ID, "type": "claim",
                 "label": source["summary"]},
                {"id": "source:" + DEMO_RECORD_ID, "type": "source",
                 "label": source["title"]},
                {"id": "publisher:fictional-harborlight", "type": "publisher",
                 "label": source["publisher"]},
            ],
            "edges": [
                {"from": "event:" + DEMO_CASE_ID, "to": "claim:" + DEMO_RECORD_ID,
                 "relation": "has_claim"},
                {"from": "claim:" + DEMO_RECORD_ID, "to": "source:" + DEMO_RECORD_ID,
                 "relation": "supported_by"},
                {"from": "source:" + DEMO_RECORD_ID,
                 "to": "publisher:fictional-harborlight", "relation": "published_by"},
            ],
            "claims": [{
                "claim_id": "claim:" + DEMO_RECORD_ID,
                "record_id": DEMO_RECORD_ID,
                "text": source["summary"],
                "title": source["title"],
                "source_url": source["source_url"],
                "available_at": source["available_at"],
                "source_node_id": "source:" + DEMO_RECORD_ID,
                "publisher_node_id": "publisher:fictional-harborlight",
                "observation_ids": [],
                "observation_node_ids": [],
                "evidence_adapters": ["synthetic_fixture"],
            }],
            "excluded_counts": snapshot["excluded_counts"],
        }
        with self.store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO graphs VALUES (?, ?, ?, ?, ?, ?)",
                (DEMO_GRAPH_ID, DEMO_CASE_ID, snapshot["cutoff"], snapshot["version"],
                 _now(), _dump(graph)),
            )
            saved = db.execute(
                "SELECT case_id, cutoff, version, body FROM graphs WHERE graph_id=?",
                (DEMO_GRAPH_ID,),
            ).fetchone()
        if (saved is None or saved["case_id"] != DEMO_CASE_ID
                or saved["cutoff"] != snapshot["cutoff"]
                or saved["version"] != snapshot["version"]
                or json.loads(saved["body"]) != graph):
            raise TaskError("stable Day 4 demo graph id has conflicting content")
        run_id = self.simulation.create_run(
            DEMO_CASE_ID, DEMO_GRAPH_ID,
            agent_count=agent_count, rounds=rounds, concurrency=concurrency,
            seed=20261007,
        )
        return self.simulation.summary(run_id)
