"""Deterministic, evidence-bound offline reports for a persisted simulation run."""

import hashlib
import json
import random
from collections import Counter, defaultdict
from datetime import datetime

from riskshield.store import Store


class ReportError(ValueError):
    """The available evidence cannot support the requested report."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _time(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result
    except (TypeError, ValueError):
        raise ReportError("Evidence time must be timezone-aware") from None


def _action_id(run_id, round_number, agent_id):
    return f"action:{run_id}:{round_number}:{agent_id}"


_STATE_RULE_VERSION = "day3_state_transition_v1"
_ROLES = ("netizen", "customer", "media", "kol", "regulator")
_SIGNALS = {"observe": 0.0, "share": -0.06, "comment": -0.04,
            "seek_clarification": 0.03, "express_complaint_intent": -0.12}


def _clip(value, lo, hi):
    return max(lo, min(hi, round(value, 4)))


def _emotion_module(config, agents, actions, messages, rounds, distributions):
    """Replay the frozen Day 3 v1 state rule, accepting it only if final state agrees."""
    fallback = {"kind": "round_action_distribution_proxy",
                "reconstruction_status": "unavailable", "series": [],
                "rounds": distributions, "emotion_state_available": False,
                "note": "Action counts are not emotion scores or observed public sentiment."}
    try:
        count = config["agent_count"]
        seed = config["seed"]
        offset = config["role_offset"]
        if (type(count) is not int or count < 1 or type(seed) is not int
                or type(offset) is not int or len(agents) != count):
            raise ValueError("initialization fields or agent rows are unavailable")
        rng = random.Random(seed)
        states = {}
        for index in range(count):
            agent_id = f"agent_{index:04d}"
            agent = agents[index]
            susceptibility = round(rng.uniform(0.25, 0.9), 3)
            influence = round(rng.uniform(0.2, 0.9), 3)
            emotion = round(rng.uniform(-0.2, 0.2), 3)
            trust = round(rng.uniform(0.35, 0.8), 3)
            persona = json.loads(agent["persona"])
            if (agent["agent_id"] != agent_id
                    or persona["role"] != _ROLES[(index + offset) % len(_ROLES)]
                    or persona["susceptibility"] != susceptibility
                    or persona["influence"] != influence):
                raise ValueError("agent initialization cannot be reproduced")
            states[agent_id] = {"emotion": emotion, "trust": trust}

        actions_by_round = defaultdict(list)
        messages_by_round = defaultdict(list)
        for action in actions:
            actions_by_round[action["round_number"]].append(action)
        for message in messages:
            messages_by_round[message["round_number"]].append(message)
        series = []
        for row in rounds:
            number = row["round_number"]
            if len(actions_by_round[number]) != count:
                raise ValueError("a round lacks agent decisions")
            for action in actions_by_round[number]:
                if action["agent_id"] not in states:
                    raise ValueError("a decision has no initialized agent")
                if action["status"] != "valid":
                    continue
                decision = json.loads(action["decision"])
                signal = _SIGNALS[decision["action"]]
                state = states[action["agent_id"]]
                persona = json.loads(agents[int(action["agent_id"].split("_")[1])]["persona"])
                state["emotion"] = _clip(state["emotion"] + signal * persona["susceptibility"], -1, 1)
                state["trust"] = _clip(state["trust"] +
                                        (0.015 if decision["action"] == "seek_clarification" else -0.01),
                                        0, 1)
            for message in messages_by_round[number]:
                if message["recipient_id"] not in states:
                    raise ValueError("a message has no initialized recipient")
                state = states[message["recipient_id"]]
                state["emotion"] = _clip(state["emotion"] + _SIGNALS[message["action"]] * 0.25,
                                         -1, 1)
            values = [state["emotion"] for state in states.values()]
            series.append({"round": number, "mean_emotion": round(sum(values) / count, 6),
                           "min_emotion": min(values), "max_emotion": max(values),
                           "agent_count": count})
        for agent in agents:
            saved = json.loads(agent["state"])
            expected = states[agent["agent_id"]]
            if saved["emotion"] != expected["emotion"] or saved["trust"] != expected["trust"]:
                raise ValueError("reconstructed final agent state differs from persisted state")
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        return {**fallback, "reason": str(exc)}
    return {"kind": "reconstructed_simulated_emotion_v1",
            "rule_version": _STATE_RULE_VERSION,
            "reconstruction_status": "validated_against_final_agent_state",
            "series": series, "rounds": distributions, "emotion_state_available": True,
            "note": "Derived simulation state under the stated rule; not persisted round snapshots or observed public sentiment."}


class Day4Reports:
    def __init__(self, store: Store):
        self.store = store
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS day4_reports (
                report_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, body TEXT NOT NULL
            )""")

    def build(self, run_id: str, risk_assessment: dict | None = None) -> dict:
        if risk_assessment is not None and not isinstance(risk_assessment, dict):
            raise ReportError("risk_assessment must be a JSON object")
        with self.store.connect() as db:
            run = db.execute("SELECT * FROM simulation_runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            run = dict(run)
            if run["status"] not in {"complete", "partial"}:
                raise ReportError("Report requires a completed or explicitly partial run")
            if run["completed_rounds"] < 1:
                raise ReportError("Report requires at least one completed round")
            graph_row = db.execute("SELECT * FROM graphs WHERE graph_id=?", (run["graph_id"],)).fetchone()
            if graph_row is None:
                raise ReportError("Run graph is missing")
            graph_row = dict(graph_row)
            records = {r["record_id"]: json.loads(r["body"]) for r in db.execute(
                "SELECT record_id, body FROM records WHERE case_id=?", (run["case_id"],))}
            actions = [dict(r) for r in db.execute(
                "SELECT * FROM simulation_actions WHERE run_id=? ORDER BY round_number, agent_id", (run_id,))]
            messages = [dict(r) for r in db.execute(
                "SELECT * FROM simulation_messages WHERE run_id=? ORDER BY round_number, message_id", (run_id,))]
            rounds = [dict(r) for r in db.execute(
                "SELECT * FROM simulation_rounds WHERE run_id=? ORDER BY round_number", (run_id,))]
            memories = {r["memory_id"]: dict(r) for r in db.execute(
                "SELECT * FROM simulation_memories WHERE run_id=?", (run_id,))}
            agents = [dict(r) for r in db.execute(
                "SELECT agent_id, persona, state FROM simulation_agents WHERE run_id=? ORDER BY agent_id",
                (run_id,))]

        try:
            case = self.store.snapshot(run["case_id"])
        except KeyError:
            raise ReportError("Run case is missing") from None
        config = json.loads(run["config"])
        graph = json.loads(graph_row["body"])
        if (graph_row["case_id"] != run["case_id"] or graph["case_id"] != run["case_id"]
                or graph_row["cutoff"] != case["cutoff"] or graph["cutoff"] != case["cutoff"]
                or config.get("cutoff") != case["cutoff"]
                or graph_row["version"] != case["version"]
                or graph["source_version"] != case["version"]
                or config.get("source_version") != case["version"]
                or config.get("data_mode") != case["data_mode"]):
            raise ReportError("Run, graph, and case identity or version do not match")
        cutoff = _time(case["cutoff"])
        if run["status"] == "complete" and run["completed_rounds"] != config["rounds"]:
            raise ReportError("Complete run does not contain every configured round")
        eligible = {}
        for claim in graph["claims"]:
            record = records.get(claim["record_id"])
            if (record is None or record["role"] != "input_candidate"
                    or record["available_at"] is None
                    or record["data_mode"] != case["data_mode"]
                    or _time(record["available_at"]) > cutoff
                    or _time(record["published_at"]) > cutoff
                    or _time(claim["available_at"]) != _time(record["available_at"])
                    or claim["text"] != record["summary"]
                    or claim["title"] != record["title"]
                    or claim["source_url"] != record["source_url"]):
                raise ReportError("Graph contains unknown, future, or ineligible evidence")
            eligible[claim["record_id"]] = claim
        if not eligible:
            raise ReportError("Graph has no eligible evidence")

        if risk_assessment is not None:
            for key, expected in (("run_id", run_id), ("case_id", run["case_id"]),
                                  ("graph_id", run["graph_id"]),
                                  ("data_mode", case["data_mode"])):
                if key in risk_assessment and risk_assessment[key] != expected:
                    raise ReportError(f"risk_assessment {key} does not match run")
            if risk_assessment.get("level") not in {"red", "orange", "yellow", "blue"}:
                raise ReportError("risk_assessment needs a four-level classification")
            if risk_assessment.get("status") != "provisional_offline_assessment":
                raise ReportError("risk_assessment must be marked provisional")
            try:
                risk_assessment = json.loads(_json(risk_assessment))
            except (TypeError, ValueError):
                raise ReportError("risk_assessment must be JSON-compatible") from None

        action_map = {(a["round_number"], a["agent_id"]): a for a in actions}
        message_map = {m["message_id"]: m for m in messages}
        if (len(action_map) != len(actions) or len(message_map) != len(messages)
                or len(rounds) != run["completed_rounds"]
                or [r["round_number"] for r in rounds] != list(range(1, len(rounds) + 1))):
            raise ReportError("Run ledger is incomplete or inconsistent")
        if risk_assessment is not None:
            evidence_refs = risk_assessment.get("evidence_refs", {})
            if not isinstance(evidence_refs, dict):
                raise ReportError("risk_assessment evidence_refs must be an object")
            for key, expected in (("case_id", run["case_id"]), ("graph_id", run["graph_id"]),
                                  ("cutoff", case["cutoff"]),
                                  ("source_version", case["version"])):
                if key in evidence_refs and evidence_refs[key] != expected:
                    raise ReportError(f"risk_assessment evidence_refs {key} does not match")
            if any(ref not in eligible for ref in evidence_refs.get("source_record_ids", [])):
                raise ReportError("risk_assessment cites ineligible source evidence")
            for ref in evidence_refs.get("action_refs", []):
                if (ref.get("run_id") != run_id
                        or (ref.get("round"), ref.get("agent_id")) not in action_map):
                    raise ReportError("risk_assessment cites an action outside this run")

        distributions = []
        failed_ids = []
        cited_source_ids = set()
        for row in rounds:
            number = row["round_number"]
            round_actions = [a for a in actions if a["round_number"] == number]
            counts = Counter()
            action_ids = []
            for action in round_actions:
                aid = _action_id(run_id, number, action["agent_id"])
                action_ids.append(aid)
                refs = json.loads(action["observation_refs"])
                if any(ref not in eligible for ref in refs.get("evidence", [])):
                    raise ReportError("Action observed ineligible source evidence")
                if any(ref not in message_map or message_map[ref]["recipient_id"] != action["agent_id"]
                       or message_map[ref]["round_number"] != number - 1
                       for ref in refs.get("messages", [])):
                    raise ReportError("Action observed foreign or future messages")
                if any(ref not in memories or memories[ref]["agent_id"] != action["agent_id"]
                       or memories[ref]["round_number"] >= number
                       for ref in refs.get("memories", [])):
                    raise ReportError("Action observed foreign or future memory")
                decision = json.loads(action["decision"]) if action["decision"] else None
                if action["status"] == "valid" and decision is not None:
                    if not set(decision["evidence_ids"]) <= (set(refs.get("evidence", [])) |
                            set(refs.get("messages", [])) | set(refs.get("memories", []))):
                        raise ReportError("Decision cites evidence outside its observation")
                    cited_source_ids.update(set(decision["evidence_ids"]) & set(eligible))
                    counts[decision["action"]] += 1
                else:
                    counts["failed_or_invalid"] += 1
                    failed_ids.append(aid)
            stats = json.loads(row["stats"])
            if (len(round_actions) != stats["decision_requests"]
                    or counts["failed_or_invalid"] != stats["failed_decisions"]):
                raise ReportError("Round statistics disagree with action ledger")
            distributions.append({"round": number, "action_counts": dict(sorted(counts.items())),
                                  "decision_count": len(round_actions), "action_ids": action_ids})

        edges = defaultdict(list)
        outgoing = Counter()
        incoming = Counter()
        for message in messages:
            sender_action = action_map.get((message["round_number"], message["sender_id"]))
            if (sender_action is None or sender_action["status"] != "valid"
                    or json.loads(sender_action["decision"])["action"] != message["action"]
                    or any(ref not in eligible for ref in json.loads(message["evidence_ids"]))):
                raise ReportError("Message lacks a valid same-run sending action")
            edges[(message["sender_id"], message["recipient_id"])].append(message["message_id"])
            outgoing[message["sender_id"]] += 1
            incoming[message["recipient_id"]] += 1
        if any(sum(m["round_number"] == r["round_number"] for m in messages)
               != json.loads(r["stats"])["messages"] for r in rounds):
            raise ReportError("Round statistics disagree with message ledger")
        propagation = [{"sender_id": sender, "recipient_id": recipient,
                        "message_count": len(ids), "message_ids": ids,
                        "action_ids": sorted({_action_id(run_id, message_map[mid]["round_number"], sender)
                                              for mid in ids})}
                       for (sender, recipient), ids in sorted(edges.items())]
        nodes = sorted(set(outgoing) | set(incoming),
                       key=lambda agent: (-outgoing[agent], agent))[:10]
        key_nodes = [{"agent_id": agent, "outgoing_message_count": outgoing[agent],
                      "incoming_message_count": incoming[agent],
                      "message_ids": [m["message_id"] for m in messages
                                      if m["sender_id"] == agent]}
                     for agent in nodes]

        source_ids = sorted(eligible)
        suggestions = [{"text": "Review cutoff-eligible source material before acting on this simulation.",
                        "source_record_ids": source_ids, "action_ids": [], "message_ids": []}]
        if failed_ids:
            suggestions.append({"text": "Review invalid decisions before interpreting aggregate behavior.",
                                "source_record_ids": [], "action_ids": failed_ids,
                                "message_ids": []})
        if messages:
            suggestions.append({"text": "Inspect simulated message paths; message volume alone does not prove influence.",
                                "source_record_ids": [], "action_ids": [],
                                "message_ids": [m["message_id"] for m in messages[:10]]})
        report = {
            "run_id": run_id, "case_id": run["case_id"], "graph_id": run["graph_id"],
            "cutoff": case["cutoff"], "source_version": case["version"],
            "data_mode": case["data_mode"], "run_status": run["status"],
            "completed_rounds": run["completed_rounds"], "configured_rounds": config["rounds"],
            "modules": {
                "propagation": {"kind": "simulated_runtime_messages", "edges": propagation,
                                "total_messages": len(messages),
                                "source_record_ids": source_ids},
                "emotion_evolution": _emotion_module(config, agents, actions, messages,
                                                      rounds, distributions),
                "key_nodes": {"metric": "outgoing_runtime_message_count",
                              "interpretation": "Simulated activity, not causal influence.",
                              "nodes": key_nodes},
                "risk": risk_assessment if risk_assessment is not None else
                        {"status": "unassessed", "level": None,
                         "note": "No four-level risk assessment was supplied."},
                "recommendations": {"items": suggestions},
            },
            "evidence": {"source_record_ids": source_ids,
                         "decision_cited_source_record_ids": sorted(cited_source_ids),
                         "failed_action_ids": failed_ids},
            "limitations": ["Simulated messages are not observed platform propagation.",
                            "Message counts and references do not prove message-to-action causality.",
                            "This report does not determine G3 or G4 gate status."],
        }
        digest = hashlib.sha256(_json(report).encode()).hexdigest()[:24]
        report["report_id"] = "report_" + digest
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO day4_reports VALUES (?, ?, ?)",
                       (report["report_id"], run_id, _json(report)))
        return report

    def get(self, report_id: str) -> dict:
        with self.store.connect() as db:
            row = db.execute("SELECT body FROM day4_reports WHERE report_id=?", (report_id,)).fetchone()
        if row is None:
            raise KeyError(report_id)
        return json.loads(row["body"])
