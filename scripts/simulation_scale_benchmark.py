"""Graded offline load harness for the dynamic simulation engine.

This is a local measurement tool. It never contacts a model or a network: the
decision backend below is a controlled-latency substitute whose only job is to
hold a request open for a fixed time so that scheduler, queueing and
persistence behaviour become observable at scale.

What it produces is a *throughput ledger*, not evidence of model quality. A run
recorded here exercises the engine with a substitute and must never be reported
as a real-model dynamic simulation.

Usage (from the repository root)::

    uv run --locked --offline python scripts/simulation_scale_benchmark.py \
        --agents 500 --rounds 30 --concurrency 64 --latency-ms 20 \
        --out path/to/ledger.json

Exits non-zero only on an operational failure. Every measurement is written to
the ledger, including runs that stop early.
"""

import argparse
import hashlib
import json
import platform
import statistics
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for candidate in (REPO_ROOT, REPO_ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from riskshield.simulation import (  # noqa: E402
    DEFAULT_TASK_BUDGET_CNY, Day3Simulation, DecisionPricing, DecisionResult,
)
from riskshield.schemas import AgentDecision  # noqa: E402
from riskshield.store import Store  # noqa: E402

BEIJING = timezone(timedelta(hours=8))
SUBSTITUTE_NAME = "controlled_latency_substitute"
SUBSTITUTE_VERSION = "scale-benchmark-v1"


def beijing_now() -> str:
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def peak_rss_bytes() -> int | None:
    """Best-effort process peak working set; None when the platform cannot report it."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            process = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(
                    process, ctypes.byref(counters), counters.cb):
                return int(counters.PeakWorkingSetSize)
        except Exception:
            return None
        return None
    try:
        import resource
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    except Exception:
        return None


class ControlledLatencySubstitute:
    """A substitute backend that holds each request open for a fixed time.

    The decision rule is deliberately small and deterministic: it reacts to the
    runtime message it received, else to its own memory, else to a role default.
    Latency is the independent variable that the scale question needs.
    """

    mode = "test_substitute"
    reservation_cny = Decimal("0")
    input_cny_per_million = Decimal("0")
    cached_input_cny_per_million = Decimal("0")
    output_cny_per_million = Decimal("0")
    price_version = "offline-zero-cost-v1"

    def __init__(self, latency_seconds: float, *, fail_every: int = 0):
        self.latency_seconds = float(latency_seconds)
        self.fail_every = int(fail_every)
        self._lock = threading.Lock()
        self.calls = 0
        self.in_flight = 0
        self.peak_in_flight = 0
        self.latencies: list[float] = []
        self.sender_actions = 0

    def pricing(self, observation) -> DecisionPricing:
        return DecisionPricing(requested_model=SUBSTITUTE_NAME,
                               price_version=SUBSTITUTE_VERSION,
                               reservation_cny=Decimal("0"),
                               input_cny_per_million=Decimal("0"),
                               cached_input_cny_per_million=Decimal("0"),
                               output_cny_per_million=Decimal("0"))

    def decide(self, observation) -> DecisionResult:
        with self._lock:
            self.calls += 1
            index = self.calls
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        started = time.perf_counter()
        try:
            if self.latency_seconds:
                time.sleep(self.latency_seconds)
            if self.fail_every and index % self.fail_every == 0:
                raise RuntimeError("benchmark-controlled-decision-failure")
            messages = observation["messages"]
            memories = observation["memories"]
            if messages:
                action = "comment"
                reference = messages[0]["message_id"]
                reason = "React to the runtime message received in this round."
            elif memories:
                action = "seek_clarification"
                reference = memories[0]["memory_id"]
                reason = "React to this agent's own persisted memory."
            else:
                role = observation["persona"]["role"]
                action = "share" if role in {"netizen", "media", "kol"} else "observe"
                reference = observation["evidence"][0]["evidence_id"]
                reason = "First-round reaction to visible cutoff evidence."
            return DecisionResult(
                AgentDecision(agent_id=observation["agent_id"], action=action,
                              evidence_ids=[reference], reason=reason),
                usage={"prompt_tokens": 0, "completion_tokens": 0},
                model=SUBSTITUTE_NAME,
            )
        finally:
            with self._lock:
                self.in_flight -= 1
                self.latencies.append(time.perf_counter() - started)


def prepare_benchmark_store(root: Path):
    """Purely fictional synthetic case; no real, private or historical material."""
    from tests.fixtures.day3_synthetic_case import (  # imported lazily: test fixture
        CASE_ID, GRAPH_ID, prepare_synthetic_case,
    )
    store = Store(root / "benchmark.db")
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    return store, engine, CASE_ID, GRAPH_ID


def line_counts(store: Store, run_id: str) -> dict:
    with store.connect() as db:
        def count(table):
            return db.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id=?",
                              (run_id,)).fetchone()[0]
        return {"actions": count("simulation_actions"),
                "messages": count("simulation_messages"),
                "memories": count("simulation_memories"),
                "request_metrics": count("simulation_request_metrics")}


def round_ledger(result: dict, store: Store) -> list[dict]:
    rows = []
    for row in result["rounds"]:
        performance = row.get("performance", {})
        rows.append({
            "round": row["round"],
            "target_decisions": row["decision_requests"],
            "configured_agents": row["configured_agents"],
            "dispatched_requests": row["dispatched_requests"],
            "valid_decisions": row["valid_decisions"],
            "failed_decisions": row["failed_decisions"],
            "not_dispatched_requests": row["not_dispatched_requests"],
            "active_decisions": row["active_decisions"],
            "messages_emitted": row["messages"],
            "elapsed_seconds": row.get("elapsed_seconds"),
            "context_seconds": performance.get("observation_build_seconds"),
            "scheduler_and_drain_seconds": performance.get(
                "scheduler_and_drain_wall_seconds"),
            "sqlite_persist_seconds": performance.get("sqlite_persist_seconds"),
            "request_ledger_persist_seconds": performance.get(
                "request_ledger_persist_cumulative_seconds"),
            "backend_call_seconds": performance.get("backend_call_cumulative_seconds"),
            "observed_max_in_flight": performance.get("observed_max_in_flight"),
            "failure_policy": row.get("failure_policy"),
        })
    return rows


def run_load_level(*, agents: int, rounds: int, concurrency: int, latency_ms: float,
                   seed: int = 20261011, fail_every: int = 0,
                   failure_policy: str | None = None,
                   budget_cny: Decimal = DEFAULT_TASK_BUDGET_CNY) -> dict:
    """Run one graded load level on a fresh local database and return its ledger."""
    with tempfile.TemporaryDirectory(prefix="scale-benchmark-") as temp:
        root = Path(temp)
        store, engine, case_id, graph_id = prepare_benchmark_store(root)
        run_id = engine.create_run(case_id, graph_id, agent_count=agents, rounds=rounds,
                                   concurrency=concurrency, seed=seed,
                                   budget_cny=budget_cny)
        config = engine.summary(run_id)["config"]
        backend = ControlledLatencySubstitute(latency_ms / 1000.0, fail_every=fail_every)
        started = time.perf_counter()
        result = engine.advance(run_id, backend, collect_performance=True,
                                **({"failure_policy": failure_policy}
                                   if failure_policy else {}))
        wall_seconds = time.perf_counter() - started
        requests = agents * rounds
        performance = result.get("performance") or {}
        ledger_rows = round_ledger(result, store)
        counts = line_counts(store, run_id)
        db_bytes = Path(store.path).stat().st_size
        rss = peak_rss_bytes()
        # Report the policy the engine actually applied, not the requested one.
        effective_policy = (ledger_rows[0]["failure_policy"]["mode"]
                            if ledger_rows else (failure_policy or "continue"))

    return {
        "load": {"agents": agents, "rounds": rounds, "concurrency": concurrency,
                 "latency_ms": latency_ms, "seed": seed, "fail_every": fail_every,
                 "failure_policy": failure_policy or "continue",
                 "failure_policy_applied": effective_policy,
                 "decision_opportunities": requests},
        "outcome": {
            "status": result["status"],
            "completed_rounds": result["completed_rounds"],
            "all_decisions_valid": result["all_decisions_valid"],
            "action_counts": result["action_counts"],
            "actual_participants": result["actual_participants"],
            "wall_seconds": round(wall_seconds, 4),
            "decision_opportunities": requests,
            "completed_round_opportunities": result["completed_rounds"] * agents,
            "effective_decisions_per_second": (
                round(result["action_counts"].get("valid", 0) / wall_seconds, 3)
                if wall_seconds else None),
            "opportunities_per_second": round(requests / wall_seconds, 3),
            "cost_cny": result["cost_cny"],
            "estimated_cost_cny": result["cost_cny"],
        },
        "resources": {
            "peak_process_rss_bytes": rss,
            "sqlite_bytes": db_bytes,
            "rows": counts,
            "substitute_peak_in_flight": backend.peak_in_flight,
            "engine_observed_max_in_flight": performance.get("maximum_observed_in_flight"),
            "substitute_call_count": backend.calls,
            "substitute_latency_seconds": {
                "configured": latency_ms / 1000.0,
                "p50_observed": (round(statistics.median(backend.latencies), 6)
                                 if backend.latencies else None),
                "mean_observed": (round(statistics.fmean(backend.latencies), 6)
                                  if backend.latencies else None),
            },
        },
        "performance_totals": {
            "invocation_wall_seconds_sum": performance.get("invocation_wall_seconds_sum"),
            "completed_request_throughput_per_second": performance.get(
                "completed_request_throughput_per_second"),
            "effective_decision_throughput_per_second": performance.get(
                "effective_decision_throughput_per_second"),
            "request_elapsed_seconds": performance.get("request_elapsed_seconds"),
            "queue_seconds": performance.get("queue_seconds"),
            "backend_elapsed_seconds": performance.get("backend_elapsed_seconds"),
        },
        "per_round": ledger_rows,
        "run_config_frozen": config,
    }


def build_report(levels: list[dict], *, label: str) -> dict:
    simulation_path = REPO_ROOT / "src" / "riskshield" / "simulation.py"
    sources = {}
    for relative in ("src/riskshield/simulation.py", "src/riskshield/store.py",
                     "src/riskshield/schemas.py", "tests/fixtures/day3_synthetic_case.py",
                     "pyproject.toml", "uv.lock"):
        path = REPO_ROOT / relative
        sources[relative] = sha256_file(path) if path.is_file() else None
    return {
        "schema": "simulation-scale-load-ledger-v1",
        "label": label,
        "created_at": beijing_now(),
        "binding": {
            "git_commit": git_commit(),
            "git_dirty": git_dirty(),
            "simulation_py_sha256": sha256_file(simulation_path)
            if simulation_path.is_file() else None,
            "source_sha256": sources,
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "backend": {
            "name": SUBSTITUTE_NAME,
            "version": SUBSTITUTE_VERSION,
            "is_real_model": False,
            "model_calls": 0,
            "network_requests": 0,
            "note": ("Controlled-latency substitute. These numbers measure engine "
                     "scheduling, queueing, persistence and accounting only. They are "
                     "not model quality, not a real-model dynamic simulation and must "
                     "not be extrapolated to the >=500x30 model target."),
        },
        "environment": {
            "cpu_count": __import__("os").cpu_count(),
            "peak_process_rss_bytes_after": peak_rss_bytes(),
        },
        "levels": levels,
    }


def git_commit() -> str | None:
    import subprocess
    try:
        return subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=30,
                              check=True).stdout.strip()
    except Exception:
        return None


def git_dirty() -> bool | None:
    import subprocess
    try:
        output = subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
                                capture_output=True, text=True, timeout=30,
                                check=True).stdout
        return bool(output.strip())
    except Exception:
        return None


DEFAULT_LEVELS = [
    (10, 3, 4, 50),
    (50, 5, 8, 50),
    (100, 10, 16, 50),
    (500, 30, 64, 0),
]


def parse_levels(raw: list[str]) -> list[tuple[int, int, int, float]]:
    levels = []
    for item in raw:
        agents, rounds, concurrency, latency = item.split(":")
        levels.append((int(agents), int(rounds), int(concurrency), float(latency)))
    return levels


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--level", action="append", default=None,
                        help="agents:rounds:concurrency:latency_ms (repeatable)")
    parser.add_argument("--label", default="simulation-scale-load")
    parser.add_argument("--seed", type=int, default=20261011)
    parser.add_argument("--fail-every", type=int, default=0,
                        help="fail every Nth decision (0 disables)")
    parser.add_argument("--failure-policy", choices=["stop", "continue"], default=None)
    parser.add_argument("--out", default=None, help="ledger JSON path; stdout when omitted")
    arguments = parser.parse_args(argv)
    levels = parse_levels(arguments.level) if arguments.level else DEFAULT_LEVELS

    measured = []
    for agents, rounds, concurrency, latency_ms in levels:
        print(f"running agents={agents} rounds={rounds} concurrency={concurrency} "
              f"latency_ms={latency_ms}", flush=True)
        level = run_load_level(agents=agents, rounds=rounds, concurrency=concurrency,
                               latency_ms=latency_ms, seed=arguments.seed,
                               fail_every=arguments.fail_every,
                               failure_policy=arguments.failure_policy)
        measured.append(level)
        print(json.dumps({"load": level["load"], "outcome": level["outcome"]},
                         ensure_ascii=False), flush=True)

    report = build_report(measured, label=arguments.label)
    payload = json.dumps(report, ensure_ascii=False, indent=1)
    if arguments.out:
        target = Path(arguments.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        print(f"ledger written to {target}", flush=True)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
