"""Provisional Day 4 alert classification and offline delivery previews.

No transport client is imported here. A preview is an audit record, never a send.
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone

from riskshield.evidence import EvidencePipeline
from riskshield.reporting import Day4Reports
from riskshield.simulation import Day3Simulation
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
        self.evidence = EvidencePipeline(store)
        # Used to find a round whose ledger reconciles before a risk level is produced.
        self.reports = Day4Reports(store)
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
        if run["status"] not in {"complete", "partial"}:
            raise AlertError("run must be complete or explicitly partial to assess")
        config = json.loads(run["config"])
        snapshot = self.store.snapshot(run["case_id"])
        graph = self.evidence.graph(run["graph_id"])
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

        # Which round may be assessed. The latest recorded round is the natural choice, but
        # it can hold no decision at all: a round that stopped on a timeout, a pool that was
        # spent, or a capacity refusal records requests without any usable decision. Reading
        # an empty round would produce a risk level with nothing behind it, so the assessment
        # falls back to the most recent round that actually decided something -- and only to
        # a round the report builder can also reconcile, so the alert never rests on a round
        # the report would reject.
        latest_recorded_round = run["completed_rounds"]
        valid_by_round = self.reports.valid_decisions_by_round(run_id)
        assessed_round = latest_recorded_round
        round_fallback = None
        if valid_by_round.get(assessed_round, 0) < 1:
            earlier = sorted((number for number, count in valid_by_round.items()
                              if count >= 1 and number < latest_recorded_round),
                             reverse=True)
            if not earlier:
                raise AlertError(
                    f"run has no round with a valid decision to assess "
                    f"({latest_recorded_round} recorded round(s)); no risk level is produced")
            assessed_round = earlier[0]
            round_fallback = {
                "reason": ("latest recorded round has no valid decision; fell back to the "
                           "most recent round with valid decisions and a reconciled ledger"),
                "latest_recorded_round": latest_recorded_round,
                "assessed_round": assessed_round,
            }

        round_number = assessed_round
        with self.store.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT agent_id, status, decision, observation_refs FROM simulation_actions "
                "WHERE run_id=? AND round_number=? ORDER BY agent_id", (run_id, round_number))]
            # Both sides of this integration needed the run's action ledger, so the
            # counts are taken once: ``action_counts`` covers every status for the whole
            # run, and ``valid_decision_count`` is the independent count the integrity
            # check below reconciles against it.
            action_counts = {row["status"]: row["n"] for row in db.execute(
                "SELECT status, COUNT(*) AS n FROM simulation_actions WHERE run_id=? "
                "GROUP BY status", (run_id,))}
            valid_decision_count = db.execute(
                "SELECT COUNT(*) AS n FROM simulation_actions WHERE run_id=? "
                "AND status='valid' AND decision IS NOT NULL", (run_id,)).fetchone()["n"]
            message_count = db.execute(
                "SELECT COUNT(*) AS n FROM simulation_messages WHERE run_id=? AND round_number=?",
                (run_id, round_number)).fetchone()["n"]
        valid = [row for row in rows if row["status"] == "valid" and row["decision"]]
        # ``deadline_exceeded`` is a request the engine stopped waiting for and
        # ``not_dispatched`` was never sent. Neither is a failed decision, so counting
        # "everything that is not valid" as a failure would report undecided requests as
        # our own errors and would also overstate ``decisions_all_valid`` failures.
        failed = [row for row in rows if row["status"] == "failed"]
        undecided = [row for row in rows
                     if row["status"] in {"deadline_exceeded", "not_dispatched"}]
        if not valid:
            raise AlertError("latest round has no valid decisions")
        total_failures = action_counts.get("failed", 0)
        undecided_statuses = {"deadline_exceeded", "not_dispatched"}
        run_undecided = sum(action_counts.get(status, 0) for status in undecided_statuses)
        run_complete = run["status"] == "complete" and round_number == config["rounds"]
        decisions_all_valid = (valid_decision_count == sum(action_counts.values()) ==
                               config["agent_count"] * round_number)
        integrity = {
            "run_complete": run_complete, "decisions_all_valid": decisions_all_valid,
            "alert_is_success_result": run_complete and decisions_all_valid,
            "run_action_counts": action_counts,
        }
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
        if not integrity["alert_is_success_result"]:
            limitations.append("Run is incomplete or contains failed decisions; classify valid latest-round actions only.")
        if run_undecided:
            limitations.append("Some requests were never decided (dispatched but not waited for, or never sent); they are undecided, not failed.")
        if round_fallback is not None:
            limitations.append(
                f"Latest recorded round {latest_recorded_round} has no valid decision; this "
                f"assessment is based on round {round_number} of an incomplete run and must "
                f"not be read as the current complete result.")
        execution_mode = (config.get("backend") or {}).get(
            "type", config.get("backend_mode"))
        if snapshot["data_mode"] == "real_historical" and execution_mode == "offline_dynamic_substitute":
            limitations.append("Real historical source material was processed by an offline substitute; this is not real-model reasoning evidence.")
        fingerprint = hashlib.sha256(json.dumps({
            "run_id": run_id, "rule_version": RULE_VERSION,
            "run_status": run["status"], "round": round_number,
            "rows": rows, "total_failures": total_failures,
            "integrity": integrity, "configured_rounds": config["rounds"],
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
            # ``**integrity`` carries run_complete, decisions_all_valid,
            # alert_is_success_result and run_action_counts. It must not silently
            # overwrite the two explicit flags, so both are expanded first and the
            # integrity block follows, sharing one definition.
            "run_complete": integrity["run_complete"],
            "decisions_all_valid": integrity["decisions_all_valid"],
            "alert_is_success_result": integrity["alert_is_success_result"],
            # Which round this assessment actually rests on, and whether that is the latest
            # one. A fallback is never presented as the current result.
            "assessed_round": round_number,
            "latest_recorded_round": latest_recorded_round,
            "round_fallback": round_fallback,
            "assessment_basis": ("latest_recorded_round" if round_fallback is None
                                 else "earlier_round_with_valid_decisions"),
            "assessment_basis_label": (
                "基于最新已记录轮次" if round_fallback is None
                else f"基于第 {round_number} 轮的不完整运行预警"),
            **integrity,
            "assessed_at": assessed_at,
            # Local rule detection time; it is not the historical event's first-public time.
            "discovered_at": assessed_at,
            "discovery_time_semantics": "local simulated-rule assessment time; not historical event discovery",
            "sent_at": None,
            "metrics": {
                "round": round_number, "run_status": run["status"],
                # The metrics below describe the round this assessment rests on, which is not
                # necessarily the latest one; ``latest_recorded_round`` above names the other.
                "assessed_round": round_number,
                "assessed_round_valid_decisions": len(valid),
                "assessed_round_failed_decisions": len(failed),
                "assessed_round_undecided_requests": len(undecided),
                "assessed_round_simulated_messages": message_count,
                # Kept because existing consumers read these names; after a fallback they
                # describe the assessed round, which ``round_fallback`` states explicitly.
                "latest_round_valid_decisions": len(valid),
                # ``failed`` and ``undecided`` are the assessed round's own lists, so these
                # two stay consistent with ``assessed_round_valid_decisions`` above.
                "latest_round_failed_decisions": len(failed),
                "latest_round_undecided_requests": len(undecided),
                "run_failed_decisions": total_failures,
                "run_undecided_requests": run_undecided,
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
        run_status = (alert.get("metrics") or {}).get("run_status", "unknown")
        success_result = alert.get("alert_is_success_result") is True
        qualifier = "" if success_result else "[INCOMPLETE OR UNVERIFIED RUN] "
        message = (f"OFFLINE PREVIEW ONLY: {qualifier}{alert['level'].upper()} provisional simulated "
                   f"alert for case {alert['case_id']} (alert {alert_id}). "
                   f"Run status: {run_status}; complete valid result: {success_result}. "
                   "No notification was sent. Review source and action references in the alert.")
        payload = ({"msgtype": "text", "text": {"content": message}}
                   if channel == "wecom" else
                   {"subject": f"[OFFLINE PREVIEW] {alert['level'].upper()} simulated alert {qualifier}".rstrip(),
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
            "receipt_status": "not_requested", "receipt_at": None, "delivered_at": None,
            "run_status": run_status, "run_complete": alert.get("run_complete"),
            "decisions_all_valid": alert.get("decisions_all_valid"),
            "alert_is_success_result": success_result,
            "recipient": None,
            "payload": payload,
            "limitation": "No notification was sent; delivery and 30-minute timing are unverified.",
        }
        with self.store.connect() as db:
            db.execute("INSERT INTO day4_delivery_previews VALUES (?, ?, ?, ?, ?)",
                       (preview["preview_id"], alert_id, channel,
                        preview["previewed_at"], _dump(preview)))
        return preview
