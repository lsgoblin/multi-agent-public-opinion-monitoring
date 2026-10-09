"""Offline context benchmark against the frozen synthetic 500x30 run ledger."""

import hashlib
import json
import shutil
import statistics
import sqlite3
import tempfile
import time
from pathlib import Path
from decimal import Decimal

from riskshield.simulation import Day3Simulation
from riskshield.store import Store


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "data/research/day3_synthetic_bailian_500x30_20261007.sqlite3"
SOURCE_JSON = ROOT / "data/research/day3_synthetic_bailian_500x30_20261007.json"
RUN_ID = "run_50789c8d2dfe4260b682"


def review_saved_ledger():
    """Compare the frozen JSON and SQLite run ledgers without exposing content."""
    data = json.loads(SOURCE_JSON.read_text(encoding="utf-8"))
    connection = sqlite3.connect(SOURCE_DB)
    connection.row_factory = sqlite3.Row
    try:
        run = dict(connection.execute(
            "SELECT * FROM simulation_runs WHERE run_id=?", (RUN_ID,)).fetchone())
        config = json.loads(run["config"])
        db_rounds = [dict(row) for row in connection.execute(
            "SELECT * FROM simulation_rounds WHERE run_id=? ORDER BY round_number", (RUN_ID,))]
        db_actions = connection.execute(
            "SELECT * FROM simulation_actions WHERE run_id=? ORDER BY round_number, agent_id",
            (RUN_ID,))
        round_stats_match = True
        for item, row in zip(data["rounds"], db_rounds):
            stats = json.loads(row["stats"])
            round_stats_match &= (item["round"] == row["round_number"] and
                                  all(item[key] == value for key, value in stats.items()))

        trajectory_match = True
        actions_compared = 0
        valid_decisions = failed_decisions = 0
        for item, row in zip(data["trajectory"], db_actions):
            actions_compared += 1
            decision = json.loads(row["decision"]) if row["decision"] else None
            usage = json.loads(row["usage"]) if row["usage"] else None
            refs = json.loads(row["observation_refs"])
            diagnostics = (json.loads(row["schema_diagnostics"])
                           if row["schema_diagnostics"] else None)
            trajectory_match &= (
                item["round"] == row["round_number"] and
                item["agent_id"] == row["agent_id"] and
                item["status"] == row["status"] and
                item["decision"] == decision and item["usage"] == usage and
                item["requested_model"] == row["requested_model"] and
                item["actual_model"] == row["model"] and
                item["cost_cny"] == row["cost_cny"] and
                item["reserved_cny"] == row["reserved_cny"] and
                item["error"] == row["error"] and
                item["observation_refs"] == refs and
                item["schema_diagnostics"] == diagnostics)
            valid_decisions += row["status"] == "valid"
            failed_decisions += row["status"] == "failed"

        json_messages = {item["message_id"]: item for item in data["messages"]}
        db_messages = {row["message_id"]: dict(row) for row in connection.execute(
            "SELECT * FROM simulation_messages WHERE run_id=?", (RUN_ID,))}
        message_match = (len(json_messages) == len(db_messages) and all(
            message_id in db_messages and
            item["round"] == db_messages[message_id]["round_number"] and
            item["sender_id"] == db_messages[message_id]["sender_id"] and
            item["recipient_id"] == db_messages[message_id]["recipient_id"] and
            item["action"] == db_messages[message_id]["action"] and
            item["evidence_ids"] == json.loads(db_messages[message_id]["evidence_ids"]) and
            item["content_sha256"] == hashlib.sha256(
                db_messages[message_id]["content"].encode()).hexdigest()
            for message_id, item in json_messages.items()))
        memory_count = connection.execute(
            "SELECT COUNT(*) FROM simulation_memories WHERE run_id=?", (RUN_ID,)).fetchone()[0]
        participants = connection.execute(
            "SELECT COUNT(DISTINCT agent_id) FROM simulation_actions "
            "WHERE run_id=? AND status='valid'", (RUN_ID,)).fetchone()[0]
    finally:
        connection.close()

    elapsed = sum(row["elapsed_seconds"] for row in data["rounds"])
    routes = config["backend"]["routes"]
    reserve = (Decimal(routes["qwen-turbo"]["reservation_cny"]) * 6000 +
               Decimal(routes["qwen-flash"]["reservation_cny"]) * 9000)
    return {
        "run_id": RUN_ID,
        "input": {"case_id": run["case_id"], "graph_id": run["graph_id"],
                  "source_version": config["source_version"], "cutoff": config["cutoff"],
                  "data_mode": config["data_mode"]},
        "source_files": {
            "json": {"sha256": hashlib.sha256(SOURCE_JSON.read_bytes()).hexdigest(),
                     "bytes": SOURCE_JSON.stat().st_size},
            "sqlite": {"sha256": hashlib.sha256(SOURCE_DB.read_bytes()).hexdigest(),
                       "bytes": SOURCE_DB.stat().st_size},
        },
        "run_config": {"mode": config["backend_mode"], "agents": config["agent_count"],
                       "rounds": config["rounds"], "concurrency": config["concurrency"],
                       "max_calls": data["max_calls"], "seed": config["seed"],
                       "budget_cny": config["budget_cny"],
                       "route_calls": {name: data["usage"]["by_model"][name]["calls"]
                                       for name in ("qwen-turbo", "qwen-flash")},
                       "max_call_reservation_cny": str(reserve),
                       "reservation_headroom_cny": str(
                           Decimal(config["budget_cny"]) - reserve)},
        "outcome": {"run_status": run["status"], "completed_rounds": run["completed_rounds"],
                    "decision_requests": len(data["trajectory"]),
                    "valid_decisions": valid_decisions, "failed_decisions": failed_decisions,
                    "actual_participants": participants, "runtime_messages": len(db_messages),
                    "memory_rows": memory_count, "estimated_cost_cny": data["estimated_cost_cny"],
                    "usage": data["usage"]},
        "per_round_timer": {
            "seconds_sum": round(elapsed, 3), "minutes_sum": round(elapsed / 60, 3),
            "per_round_seconds": [row["elapsed_seconds"] for row in data["rounds"]],
            "interval_definition": (
                "Starts before thread-pool dispatch; stops after call/decision validation, usage "
                "aggregation and in-memory state/message/memory staging. Excludes context, "
                "budget preflight, SQLite transaction/persistence, refresh and report generation."),
            "source_provenance_limit": (
                "Run record reports d431a10 as the pre-run Git base and also notes preserved "
                "uncommitted changes. Run JSON/SQLite do not pin the executed day3.py SHA."),
        },
        "ledger_consistency": {
            "run_config_matches": (run["status"] == data["run_status"] and
                                    run["completed_rounds"] == data["completed_rounds"] and
                                    run["cost_cny"] == data["estimated_cost_cny"]),
            "round_stats_match_json_sqlite": round_stats_match,
            "trajectory_match_json_sqlite": (trajectory_match and
                                              actions_compared == len(data["trajectory"])),
            "message_hash_and_metadata_match_json_sqlite": message_match,
        },
    }


def benchmark_saved_context(repeats=1):
    """Measure only frozen-ledger context construction; never invokes a backend."""
    if not SOURCE_DB.is_file():
        raise FileNotFoundError(SOURCE_DB)
    with tempfile.TemporaryDirectory() as temp_dir:
        copy = Path(temp_dir) / SOURCE_DB.name
        shutil.copyfile(SOURCE_DB, copy)
        engine = Day3Simulation(Store(copy))
        run = engine._run_row(RUN_ID)
        agents = engine._agents(RUN_ID)
        neighbors = {agent["agent_id"]: set(agent["neighbors"]) for agent in agents}
        with engine.store.connect() as db:
            actions = [dict(row) for row in db.execute(
                "SELECT * FROM simulation_actions WHERE run_id=? ORDER BY round_number, agent_id",
                (RUN_ID,))]
            message_rows = [dict(row) for row in db.execute(
                "SELECT * FROM simulation_messages WHERE run_id=? ORDER BY round_number, message_id",
                (RUN_ID,))]
            memory_rows = [dict(row) for row in db.execute(
                "SELECT * FROM simulation_memories WHERE run_id=?",
                (RUN_ID,))]
        action_by_key = {(row["round_number"], row["agent_id"]): row for row in actions}
        messages_by_round_and_recipient = {}
        for message in message_rows:
            messages_by_round_and_recipient.setdefault(
                (message["round_number"], message["recipient_id"]), set()).add(
                    message["message_id"])
            sender = action_by_key[(message["round_number"], message["sender_id"])]
            decision = json.loads(sender["decision"])
            assert sender["status"] == "valid"
            assert decision["action"] == message["action"]
            assert message["recipient_id"] in neighbors[message["sender_id"]]
        assert all(row["agent_id"] in row["memory_id"] for row in memory_rows)

        samples = []
        for repeat in range(repeats):
            started = time.perf_counter()
            observations_built = 0
            for round_number in range(1, 31):
                observations = engine._context(run, agents, round_number)
                observations_built += len(observations)
            samples.append({"repeat": repeat + 1,
                            "wall_seconds": round(time.perf_counter() - started, 6),
                            "observations": observations_built})

        valid_decisions = failed_decisions = requests_with_messages = 0
        requests_with_memories = citations_to_messages = citations_to_memories = 0
        decisions_citing_messages = decisions_citing_memories = 0
        visible_message_count = 0
        for round_number in range(1, 31):
            observations = engine._context(run, agents, round_number)
            assert len(observations) == 500
            for observation in observations:
                agent_id = observation["agent_id"]
                message_ids = {message["message_id"] for message in observation["messages"]}
                assert message_ids == messages_by_round_and_recipient.get(
                    (round_number - 1, agent_id), set())
                visible_message_count += len(message_ids)
                assert all(memory["round"] < round_number and
                           agent_id in memory["memory_id"] and
                           isinstance(memory["refs"], list)
                           for memory in observation["memories"])
                if observation["messages"]:
                    requests_with_messages += 1
                if observation["memories"]:
                    requests_with_memories += 1
                action = action_by_key[(round_number, agent_id)]
                recorded_refs = json.loads(action["observation_refs"])
                recorded_message_ids = set(recorded_refs["messages"])
                recorded_memory_ids = set(recorded_refs["memories"])
                assert message_ids == recorded_message_ids
                assert {memory["memory_id"] for memory in observation["memories"]} == recorded_memory_ids
                if action["status"] == "failed":
                    failed_decisions += 1
                    assert action["decision"] is None
                    continue
                valid_decisions += 1
                decision = json.loads(action["decision"])
                allowed = (set(recorded_refs["evidence"]) | recorded_message_ids |
                           recorded_memory_ids)
                citations = set(decision["evidence_ids"])
                assert citations <= allowed
                citations_to_messages += len(citations & recorded_message_ids)
                citations_to_memories += len(citations & recorded_memory_ids)
                decisions_citing_messages += bool(citations & recorded_message_ids)
                decisions_citing_memories += bool(citations & recorded_memory_ids)

        assert len(actions) == 15_000
        assert valid_decisions == 14_994
        assert failed_decisions == 6
        assert len(message_rows) == 20_622
        assert len(memory_rows) == 34_464
        assert all(sample["observations"] == 15_000 for sample in samples)

        return {
            "source": "data/research/day3_synthetic_bailian_500x30_20261007.sqlite3",
            "source_sha256": hashlib.sha256(SOURCE_DB.read_bytes()).hexdigest(),
            "run_id": RUN_ID,
            "rounds_per_repeat": 30,
            "agents_per_round": 500,
            "samples": samples,
            "median_wall_seconds": round(statistics.median(
                sample["wall_seconds"] for sample in samples), 6),
            "timed_region": "repeated Day3Simulation._context only",
            "backend_calls": 0,
            "network_calls": 0,
            "runtime_audit": {
                "decision_requests": len(actions),
                "valid_decisions": valid_decisions,
                "failed_decisions": failed_decisions,
                "rounds": 30,
                "agents_per_round": 500,
                "runtime_messages": len(message_rows),
                "visible_messages_checked": visible_message_count,
                "messages_in_final_round_without_next_round": sum(
                    message["round_number"] == 30 for message in message_rows),
                "memory_rows": len(memory_rows),
                "cross_agent_memory_rows": 0,
                "requests_with_messages": requests_with_messages,
                "requests_with_own_memory": requests_with_memories,
                "decisions_citing_messages": decisions_citing_messages,
                "decision_citations_to_messages": citations_to_messages,
                "decisions_citing_own_memory": decisions_citing_memories,
                "decision_citations_to_own_memory": citations_to_memories,
                "message_sender_action_and_neighbor_validated": True,
                "all_decision_citations_in_current_observation": True,
                "failed_decisions_not_filled": True,
            },
        }


def test_saved_500x30_context_history_contract():
    result = benchmark_saved_context()
    audit = result["runtime_audit"]
    assert audit["decision_requests"] == 15_000
    assert audit["valid_decisions"] == 14_994
    assert audit["failed_decisions"] == 6
    assert audit["runtime_messages"] == 20_622
    assert audit["memory_rows"] == 34_464
    assert audit["cross_agent_memory_rows"] == 0
    assert audit["message_sender_action_and_neighbor_validated"]
    assert audit["all_decision_citations_in_current_observation"]
    assert audit["failed_decisions_not_filled"]
    assert result["backend_calls"] == result["network_calls"] == 0


def test_saved_500x30_json_sqlite_accounting():
    review = review_saved_ledger()
    assert review["per_round_timer"]["seconds_sum"] == 4898.446
    assert review["outcome"]["decision_requests"] == 15_000
    assert review["outcome"]["valid_decisions"] == 14_994
    assert review["outcome"]["failed_decisions"] == 6
    assert review["outcome"]["actual_participants"] == 500
    assert all(review["ledger_consistency"].values())


if __name__ == "__main__":
    result = benchmark_saved_context(repeats=5)
    output_dir = ROOT / "artifacts/final-simulation-review"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "runtime-audit-after.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "scale-run-review.json").write_text(
        json.dumps(review_saved_ledger(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
