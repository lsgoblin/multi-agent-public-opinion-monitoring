"""Offline scheduler comparison for bounded Day 3 execution (no network calls)."""

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from riskshield.simulation import Day3Simulation, DecisionResult
from riskshield.schemas import AgentDecision
from riskshield.store import Store
from tests.fixtures.day3_synthetic_case import CASE_ID, GRAPH_ID, prepare_synthetic_case


class ControlledDelayBackend:
    mode = "test_substitute"
    model = "controlled-delay-test"
    price_version = "controlled-delay-v1"
    reservation_cny = 0
    input_cny_per_million = 0
    output_cny_per_million = 0

    def __init__(self, delay_seconds, fail_agent, fail_round):
        self.delay_seconds = delay_seconds
        self.fail_agent = fail_agent
        self.fail_round = fail_round
        self._lock = threading.Lock()
        self._in_flight = 0
        self.max_in_flight = 0
        self.calls = Counter()

    def decide(self, observation):
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            self.calls[(observation["round"], observation["agent_id"])] += 1
        try:
            time.sleep(self.delay_seconds)
            if (observation["round"], observation["agent_id"]) == (
                    self.fail_round, self.fail_agent):
                raise RuntimeError("controlled probe failure; response suppressed")
            if observation["messages"]:
                action, ref = "comment", observation["messages"][0]["message_id"]
                reason = "Respond to a visible simulated message."
            elif observation["memories"]:
                action, ref = "seek_clarification", observation["memories"][0]["memory_id"]
                reason = "Use this agent's own prior simulated memory."
            elif observation["persona"]["role"] == "netizen":
                action, ref = "share", observation["evidence"][0]["evidence_id"]
                reason = "Share visible fictional evidence."
            else:
                action, ref = "observe", observation["evidence"][0]["evidence_id"]
                reason = "Observe visible fictional evidence."
            return DecisionResult(AgentDecision(
                agent_id=observation["agent_id"], action=action,
                evidence_ids=[ref], reason=reason,
            ), usage={"prompt_tokens": 3, "completion_tokens": 2}, model=self.model)
        finally:
            with self._lock:
                self._in_flight -= 1


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def run_candidate(root, concurrency, agents, rounds, delay_seconds):
    store_path = root / f"concurrency-{concurrency}.sqlite3"
    store = Store(store_path)
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    snapshot = store.snapshot(CASE_ID)
    with store.connect() as db:
        graph_row = db.execute("SELECT body FROM graphs WHERE graph_id=?", (GRAPH_ID,)).fetchone()
        graph_hash = sha256_bytes(graph_row["body"].encode("utf-8"))
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=agents, rounds=rounds,
                               seed=47, concurrency=concurrency)
    fail_agent, fail_round = "agent_0016", 2
    backend = ControlledDelayBackend(delay_seconds, fail_agent, fail_round)
    wall_start = datetime.now().astimezone().isoformat()
    started = time.perf_counter()
    summary = engine.advance(run_id, backend, collect_performance=True)
    wall_seconds = time.perf_counter() - started
    wall_end = datetime.now().astimezone().isoformat()
    with store.connect() as db:
        requests = [dict(row) for row in db.execute(
            "SELECT round_number, agent_id, requested_model, queued_at, started_at, ended_at, "
            "queue_seconds, backend_seconds, validation_seconds, request_elapsed_seconds, "
            "status, failure_category, dispatch_evidence, usage_json, cost_cny, usage_unknown "
            "FROM simulation_request_metrics WHERE run_id=? ORDER BY round_number, agent_id",
            (run_id,))]
    assert all(count == 1 for count in backend.calls.values())
    assert len(backend.calls) == agents * rounds
    return {
        "run_id": run_id,
        "config": {"agents": agents, "rounds": rounds, "concurrency": concurrency,
                   "seed": 47, "delay_seconds": delay_seconds,
                   "failure": {"round": fail_round, "agent_id": fail_agent},
                   "backend": "offline controlled delay/failure substitute"},
        "input": {"case_id": CASE_ID, "graph_id": GRAPH_ID,
                  "case_version": snapshot["version"], "cutoff": snapshot["cutoff"],
                  "case_data_mode": snapshot["data_mode"], "graph_body_sha256": graph_hash},
        "wall": {"started_at": wall_start, "ended_at": wall_end,
                 "advance_seconds": round(wall_seconds, 6)},
        "observed_backend_max_in_flight": backend.max_in_flight,
        "backend_calls_by_round_agent": [
            {"round": round_number, "agent_id": agent_id, "count": count}
            for (round_number, agent_id), count in sorted(backend.calls.items())],
        "summary": summary,
        "request_metrics": requests,
        "evidence_limit": (
            "Measures local scheduling with deterministic sleep and one controlled failure; "
            "not cloud model latency, provider inference, rate limits, or full input-to-report."),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents", type=int, default=64)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--delay-seconds", type=float, default=0.02)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not (1 <= args.agents <= 500 and 1 <= args.rounds <= 30 and
            0 < args.delay_seconds <= 5):
        parser.error("agents, rounds or delay outside supported probe bounds")

    with tempfile.TemporaryDirectory(prefix="day3-performance-probe-") as temp_dir:
        temp_root = Path(temp_dir)
        candidates = [run_candidate(temp_root, concurrency, args.agents,
                                    args.rounds, args.delay_seconds)
                      for concurrency in (4, 8)]
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                  check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        revision = "unavailable"
    source_hash = sha256_bytes((ROOT / "src/riskshield/simulation.py").read_bytes())
    artifact = {
        "schema_version": "day3-scheduler-probe-v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "source": {"git_revision": revision, "working_day3_sha256": source_hash},
        "runtime": {"python": platform.python_version(), "platform": platform.platform()},
        "comparison_method": "same synthetic case/version, seed, role routing, backend and delay; only concurrency differs",
        "candidates": candidates,
    }
    output = args.output or (ROOT / "artifacts/final-simulation-review" /
                             f"scheduler-probe-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print(json.dumps({"artifact": str(output), "source_sha256": source_hash,
                      "candidates": [{
                          "concurrency": item["config"]["concurrency"],
                          "advance_seconds": item["wall"]["advance_seconds"],
                          "valid": item["summary"]["performance"]["valid_decisions"],
                          "failed": item["summary"]["performance"]["failed_dispatched_requests"],
                          "max_in_flight": item["observed_backend_max_in_flight"],
                      } for item in candidates]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
