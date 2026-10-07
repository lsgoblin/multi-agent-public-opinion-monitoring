"""Persisted Day 4 offline task execution.

The task runner deliberately accepts synthetic simulation runs only.  Its
decision backend is a local dynamic substitute: decisions depend on each
agent's current observation, own memory, and runtime messages.  It performs
no model calls, network requests, notification delivery, or deployment.
"""

import json
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from riskshield.day2 import Day2Pipeline
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


def _failure_category(exc: Exception) -> str:
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
    """Local observation-driven backend used only for synthetic demonstrations."""

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

    def _queue(self, run_id: str) -> tuple[dict, bool]:
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
            if config.get("data_mode") != "synthetic" or metadata.get("data_mode") != "synthetic":
                raise TaskError("Day 4 offline task accepts synthetic runs only")
            job_id = "job_" + uuid.uuid4().hex[:20]
            db.execute(
                "INSERT INTO day4_tasks "
                "(job_id, run_id, status, mode, queued_at, updated_at, model_calls, "
                "network_requests) VALUES (?, ?, 'queued', ?, ?, ?, 0, 0)",
                (job_id, run_id, TASK_MODE, timestamp, timestamp),
            )
            row = db.execute("SELECT * FROM day4_tasks WHERE job_id=?", (job_id,)).fetchone()
        return dict(row), True

    def start(self, run_id: str) -> dict:
        """Idempotently queue ``run_id`` and start one daemon worker."""
        row, created = self._queue(run_id)
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

    def run_sync(self, run_id: str) -> dict:
        """Deterministic synchronous entry point for local validation and tests."""
        row, created = self._queue(run_id)
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
            row = db.execute("SELECT run_id FROM day4_tasks WHERE job_id=?", (job_id,)).fetchone()
        if not changed or row is None:
            return
        run_id = row["run_id"]
        try:
            result = self.simulation.advance(run_id, OfflineDynamicSubstitute())
            if result["status"] != "complete":
                raise SimulationError("offline synthetic run did not complete")
            alert = self.alerts.assess(run_id)
            report = self.reports.build(run_id, alert)
            timestamp = _now()
            with self.store.connect() as db:
                db.execute(
                    "UPDATE day4_tasks SET status='complete', completed_at=?, updated_at=?, "
                    "alert_id=?, report_id=?, failure_category=NULL "
                    "WHERE job_id=? AND status='running'",
                    (timestamp, timestamp, alert["alert_id"], report["report_id"], job_id),
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
