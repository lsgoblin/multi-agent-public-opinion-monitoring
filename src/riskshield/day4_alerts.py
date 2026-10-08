"""Provisional Day 4 alert classification and offline delivery previews.

No transport client is imported here. A preview is an audit record, never a send.
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone

from riskshield.day2 import Day2Pipeline
from riskshield.day3 import Day3Simulation
from riskshield.store import Store


RULE_VERSION = "day4-simulated-complaint-share-v1-provisional"
CHANNELS = frozenset({"wecom", "email"})


class AlertError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _elapsed_ms(start: str | None, end: str) -> float | None:
    if not start:
        return None
    try:
        start_at, end_at = datetime.fromisoformat(start), datetime.fromisoformat(end)
        if (start_at.tzinfo is None or start_at.utcoffset() is None
                or end_at.tzinfo is None or end_at.utcoffset() is None):
            return None
    except (TypeError, ValueError):
        return None
    return round(max(0.0, (end_at - start_at).total_seconds() * 1000), 3)


class Day4Alerts:
    """Classify a completed simulation round, then optionally record dry-run previews."""

    def __init__(self, store: Store):
        self.store = store
        self.simulation = Day3Simulation(store)
        self.day2 = Day2Pipeline(store)
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS day4_alerts (
                    alert_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    assessed_at TEXT NOT NULL, body TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS day4_alerts_run ON day4_alerts(run_id);
                CREATE TABLE IF NOT EXISTS day4_delivery_previews (
                    preview_id TEXT PRIMARY KEY, alert_id TEXT NOT NULL
                        REFERENCES day4_alerts(alert_id),
                    channel TEXT NOT NULL, previewed_at TEXT NOT NULL,
                    body TEXT NOT NULL
                );
            """)

    def assess(self, run_id: str) -> dict:
        run = self.simulation._run_row(run_id)
        if run["completed_rounds"] < 1:
            raise AlertError("run has no completed round to assess")
        config = json.loads(run["config"])
        snapshot = self.store.snapshot(run["case_id"])
        graph = self.day2.graph(run["graph_id"])
        if (graph["case_id"] != run["case_id"] or
                graph["cutoff"] != snapshot["cutoff"] or
                graph["source_version"] != snapshot["version"] or
                config.get("cutoff") != snapshot["cutoff"] or
                config.get("source_version") != snapshot["version"] or
                config.get("data_mode") != snapshot["data_mode"]):
            raise AlertError("run, graph and case snapshot do not match")
        eligible = {item["record_id"]: item for item in snapshot["records"]}
        graph_sources = {item["record_id"] for item in graph["claims"]}
        cutoff = datetime.fromisoformat(snapshot["cutoff"])
        if (not graph_sources or not graph_sources <= eligible.keys() or
                any(datetime.fromisoformat(item["available_at"]) > cutoff or
                    item["available_at"] != eligible[item["record_id"]]["available_at"]
                    for item in graph["claims"])):
            raise AlertError("graph has no eligible cutoff sources")

        round_number = run["completed_rounds"]
        with self.store.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT agent_id, status, decision, observation_refs FROM simulation_actions "
                "WHERE run_id=? AND round_number=? ORDER BY agent_id", (run_id, round_number))]
            total_failures = db.execute(
                "SELECT COUNT(*) AS n FROM simulation_actions WHERE run_id=? AND status!='valid'",
                (run_id,)).fetchone()["n"]
            message_count = db.execute(
                "SELECT COUNT(*) AS n FROM simulation_messages WHERE run_id=? AND round_number=?",
                (run_id, round_number)).fetchone()["n"]
        valid = [row for row in rows if row["status"] == "valid" and row["decision"]]
        if not valid:
            raise AlertError("latest round has no valid decisions")
        complaint = [row for row in valid if json.loads(row["decision"])["action"] ==
                     "express_complaint_intent"]
        share = len(complaint) / len(valid)
        level = ("red" if share >= 0.40 else "orange" if share >= 0.20 else
                 "yellow" if share >= 0.05 else "blue")

        # The compact references are traceable without copying private memory or reasons.
        selected = complaint[:20] if complaint else valid[:20]
        action_refs = []
        observed_sources = set()
        for row in valid:
            refs = json.loads(row["observation_refs"])
            observed = set(refs.get("evidence", []))
            if not observed or not observed <= graph_sources:
                raise AlertError("decision evidence is outside this run's eligible graph")
            observed_sources.update(observed)
        for row in selected:
            refs = json.loads(row["observation_refs"])
            action_refs.append({
                "run_id": run_id, "round": round_number, "agent_id": row["agent_id"],
                "evidence_ids": sorted(set(refs.get("evidence", [])) & graph_sources),
                "message_ids": refs.get("messages", [])[:20],
            })
        limitations = [
            "Provisional thresholds on simulated complaint-intent decisions; not calibrated to real incidents.",
            "Simulation actions and messages are not real platform activity or actual complaint volume.",
            "Historical-source discovery and actual delivery timestamps are unavailable; the 30-minute red alert SLA is unverified.",
            "Offline preview only; no WeCom or email notification has been sent.",
        ]
        if snapshot["data_mode"] == "synthetic":
            limitations.append("Synthetic run must never trigger an external notification.")
        if run["status"] != "complete" or total_failures:
            limitations.append("Run is incomplete or contains failed decisions; classify valid latest-round actions only.")
        execution_mode = (config.get("backend") or {}).get(
            "type", config.get("backend_mode"))
        if snapshot["data_mode"] == "real_historical" and execution_mode == "offline_dynamic_substitute":
            limitations.append("Real historical source material was processed by an offline substitute; this is not real-model reasoning evidence.")
        fingerprint = hashlib.sha256(json.dumps({
            "run_id": run_id, "rule_version": RULE_VERSION,
            "run_status": run["status"], "round": round_number,
            "rows": rows, "total_failures": total_failures,
            "message_count": message_count,
            "graph_claims": graph["claims"],
            "source_version": snapshot["version"], "cutoff": snapshot["cutoff"],
        }, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        assessed_at = _now()
        alert = {
            "alert_id": "alert_" + fingerprint[:32],
            "run_id": run_id, "case_id": run["case_id"], "graph_id": run["graph_id"],
            "level": level, "rule_version": RULE_VERSION,
            "assessment_fingerprint_sha256": fingerprint,
            "data_mode": snapshot["data_mode"], "execution_mode": execution_mode,
            "status": "provisional_offline_assessment",
            "assessed_at": assessed_at,
            # Local rule detection time; it is not the historical event's first-public time.
            "discovered_at": assessed_at,
            "discovery_time_semantics": "local simulated-rule assessment time; not historical event discovery",
            "sent_at": None,
            "metrics": {
                "round": round_number, "run_status": run["status"],
                "latest_round_valid_decisions": len(valid),
                "latest_round_failed_decisions": len(rows) - len(valid),
                "run_failed_decisions": total_failures,
                "simulated_complaint_intent_decisions": len(complaint),
                "simulated_complaint_intent_share": round(share, 6),
                "latest_round_simulated_messages": message_count,
                "thresholds": {"red": 0.40, "orange": 0.20, "yellow": 0.05},
            },
            "evidence_refs": {
                "case_id": run["case_id"], "graph_id": run["graph_id"],
                "cutoff": snapshot["cutoff"], "source_version": snapshot["version"],
                "source_record_ids": sorted(observed_sources),
                "action_refs": action_refs,
                "action_ref_total": len(complaint) if complaint else len(valid),
                "action_refs_truncated": len(selected) < (len(complaint) if complaint else len(valid)),
            },
            "limitations": limitations,
        }
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO day4_alerts VALUES (?, ?, ?, ?)",
                       (alert["alert_id"], run_id, alert["assessed_at"], _dump(alert)))
            saved = db.execute("SELECT body FROM day4_alerts WHERE alert_id=?",
                               (alert["alert_id"],)).fetchone()
        return json.loads(saved["body"])

    def get(self, alert_id: str) -> dict:
        with self.store.connect() as db:
            row = db.execute("SELECT body FROM day4_alerts WHERE alert_id=?", (alert_id,)).fetchone()
        if row is None:
            raise KeyError(alert_id)
        return json.loads(row["body"])

    def preview_delivery(self, alert_id: str, channel: str) -> dict:
        if channel not in CHANNELS:
            raise AlertError("channel must be wecom or email")
        alert = self.get(alert_id)
        message = (f"OFFLINE PREVIEW ONLY: {alert['level'].upper()} provisional simulated "
                   f"alert for case {alert['case_id']} (alert {alert_id}). "
                   "No notification was sent. Review source and action references in the alert.")
        payload = ({"msgtype": "text", "text": {"content": message}}
                   if channel == "wecom" else
                   {"subject": f"[OFFLINE PREVIEW] {alert['level'].upper()} simulated alert",
                    "body_text": message, "to_alias": None})
        previewed_at = _now()
        preview = {
            "preview_id": "preview_" + uuid.uuid4().hex[:20],
            "alert_id": alert_id, "run_id": alert["run_id"], "channel": channel,
            "delivery_status": "dry_run", "previewed_at": previewed_at,
            "discovered_at": alert.get("discovered_at"),
            "discovery_to_preview_ms": _elapsed_ms(alert.get("discovered_at"), previewed_at),
            "latency_basis": "local_rule_assessment_to_preview_generation",
            "sent_at": None, "network_requests": 0,
            "recipient": None,
            "payload": payload,
            "limitation": "No notification was sent; delivery and 30-minute timing are unverified.",
        }
        with self.store.connect() as db:
            db.execute("INSERT INTO day4_delivery_previews VALUES (?, ?, ?, ?, ?)",
                       (preview["preview_id"], alert_id, channel,
                        preview["previewed_at"], _dump(preview)))
        return preview
