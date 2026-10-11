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
import math
import platform
import random
import statistics
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for candidate in (REPO_ROOT, REPO_ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from riskshield.simulation import (  # noqa: E402
    DEFAULT_TASK_BUDGET_CNY, Day3Simulation, DecisionPricing, DecisionResult,
    SafeDecisionError,
)
from riskshield.schemas import AgentDecision  # noqa: E402
from riskshield.store import Store  # noqa: E402

BEIJING = timezone(timedelta(hours=8))
SUBSTITUTE_NAME = "controlled_latency_substitute"
SUBSTITUTE_VERSION = "scale-benchmark-v2"

# Fault outcomes a request can end in. "ok" is a decision the engine accepts.
FAULT_OUTCOMES = ("ok", "rate_limited", "timeout", "invalid_output", "generic_error")


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

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            # Both signatures must be declared: the default 32-bit restype truncates
            # the 64-bit process pseudo-handle and the call silently returns 0.
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.GetCurrentProcess.argtypes = []
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD]
            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(),
                                              ctypes.byref(counters), counters.cb):
                return None
            return int(counters.PeakWorkingSetSize)
        except Exception:
            return None
    try:
        import resource
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    except Exception:
        return None


@dataclass(frozen=True)
class FaultProfile:
    """Deterministic, seeded latency and fault injection for offline scale testing.

    Every field is an *engineering test setting*. These distributions are chosen to make
    scheduler, timeout, admission and failure behaviour reproducible; they are not
    measurements of any real provider and must not be reported as supplier latency.
    """

    latency_seconds: float = 0.0
    latency_shape: str = "fixed"          # "fixed" | "lognormal"
    latency_sigma: float = 0.9
    latency_tail_cap: float = 10.0
    timeout_after_seconds: float = 0.0    # >0: a request genuinely blocks this long
    timeout_every: int = 0
    rate_limit_per_second: float = 0.0    # >0: admission cap; excess waits then 429s
    rate_limit_every: int = 0             # >0: every Nth request is rate limited
    invalid_output_every: int = 0         # engine must reject the decision
    error_every: int = 0                  # generic backend error
    burst_error_at: tuple = ()            # consecutive call indices that all fail
    hold_calls: int = 0                   # first N calls really block
    hold_seconds: float = 0.0
    # Simulated prices. Zero keeps the engine overhead baseline free; non-zero exercises
    # reservation, settlement and release. These are test rates, not provider prices.
    reservation_cny: str = "0"
    input_cny_per_million: str = "0"
    cached_input_cny_per_million: str = "0"
    output_cny_per_million: str = "0"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    emit_usage: bool = True
    seed: int = 20261011

    @property
    def priced(self) -> bool:
        return Decimal(self.reservation_cny) > 0

    def describe(self) -> dict:
        return {
            "latency_seconds": self.latency_seconds,
            "latency_shape": self.latency_shape,
            "latency_sigma": self.latency_sigma,
            "latency_tail_cap": self.latency_tail_cap,
            "timeout_after_seconds": self.timeout_after_seconds,
            "timeout_every": self.timeout_every,
            "rate_limit_per_second": self.rate_limit_per_second,
            "rate_limit_every": self.rate_limit_every,
            "invalid_output_every": self.invalid_output_every,
            "error_every": self.error_every,
            "burst_error_at": list(self.burst_error_at),
            "hold_calls": self.hold_calls,
            "hold_seconds": self.hold_seconds,
            "simulated_prices": {
                "reservation_cny": self.reservation_cny,
                "input_cny_per_million": self.input_cny_per_million,
                "cached_input_cny_per_million": self.cached_input_cny_per_million,
                "output_cny_per_million": self.output_cny_per_million,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "is_simulated": True,
                "note": "engineering test rates, not provider prices",
            },
            "seed": self.seed,
            "is_engineering_test_configuration": True,
        }


class SubstituteBackend:
    """A substitute that holds each request open, and can be told to misbehave.

    The decision rule is deliberately small and deterministic: it reacts to the runtime
    message it received, else to its own memory, else to a role default. Latency, faults
    and simulated prices are the independent variables the scale question needs.
    """

    def __init__(self, profile: FaultProfile | None = None):
        self.profile = profile or FaultProfile()
        self.mode = "real_model" if self.profile.priced else "test_substitute"
        self.price_version = ("simulated-prices-v1" if self.profile.priced
                              else "offline-zero-cost-v2")
        self._lock = threading.Lock()
        self._rng = random.Random(self.profile.seed)
        self.calls = 0
        self.in_flight = 0
        self.peak_in_flight = 0
        self.observed_latencies: list[float] = []
        self.outcomes: dict = {name: 0 for name in FAULT_OUTCOMES}
        self.burst_failures = 0
        self.held_calls = 0
        self._next_admission = 0.0

    def pricing(self, observation) -> DecisionPricing:
        profile = self.profile
        return DecisionPricing(
            requested_model=SUBSTITUTE_NAME,
            price_version=self.price_version,
            reservation_cny=Decimal(profile.reservation_cny),
            input_cny_per_million=Decimal(profile.input_cny_per_million),
            cached_input_cny_per_million=Decimal(profile.cached_input_cny_per_million),
            output_cny_per_million=Decimal(profile.output_cny_per_million))

    def _sample_latency(self) -> float:
        profile = self.profile
        base = profile.latency_seconds
        if base <= 0 or profile.latency_shape == "fixed":
            return max(0.0, base)
        # Median-centred lognormal: the long tail is the point of this shape.
        sampled = base * math.exp(self._rng.gauss(0.0, profile.latency_sigma))
        return min(max(sampled, 0.0), base * profile.latency_tail_cap)

    def _admission_wait(self) -> float:
        """Model an external admission cap: returns the wait before a 429 is issued."""
        profile = self.profile
        if profile.rate_limit_per_second <= 0:
            return 0.0
        interval = 1.0 / profile.rate_limit_per_second
        now = time.perf_counter()
        with self._lock:
            if now < self._next_admission:
                wait = self._next_admission - now
                self._next_admission += interval
            else:
                self._next_admission = now + interval
                wait = 0.0
        return wait

    def decide(self, observation) -> DecisionResult:
        profile = self.profile
        with self._lock:
            self.calls += 1
            index = self.calls
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        started = time.perf_counter()
        try:
            # Admission control first: a throttled request waits, then is rejected.
            admission_wait = self._admission_wait()
            if admission_wait:
                time.sleep(admission_wait)
                self._record("rate_limited", started)
                raise SafeDecisionError("rate limited", category="http_status",
                                        http_status=429)
            if profile.rate_limit_every and index % profile.rate_limit_every == 0:
                self._record("rate_limited", started)
                raise SafeDecisionError("rate limited", category="http_status",
                                        http_status=429)

            latency = self._sample_latency()
            if latency:
                time.sleep(latency)
            # A small number of genuinely held calls, to probe the stop/deadline boundary
            # without making the whole level slow.
            if profile.hold_calls and index <= profile.hold_calls and profile.hold_seconds:
                self.held_calls += 1
                time.sleep(profile.hold_seconds)

            # A genuine block past the timeout, then a transport-style failure. This is
            # not "returning a timeout error": the request really does wait this long.
            if profile.timeout_every and index % profile.timeout_every == 0:
                if profile.timeout_after_seconds > 0:
                    time.sleep(profile.timeout_after_seconds)
                self._record("timeout", started)
                raise SafeDecisionError("transport timeout", category="transport")

            if index in profile.burst_error_at:
                self.burst_failures += 1
                self._record("generic_error", started)
                raise RuntimeError("substitute-burst-failure")
            if profile.error_every and index % profile.error_every == 0:
                self._record("generic_error", started)
                raise RuntimeError("substitute-controlled-failure")

            decision = self._decision(observation)
            if profile.invalid_output_every and index % profile.invalid_output_every == 0:
                # A syntactically fine decision citing something the agent could not see.
                self._record("invalid_output", started)
                return DecisionResult(
                    AgentDecision(agent_id=observation["agent_id"], action="comment",
                                  evidence_ids=["citation_not_visible_to_this_agent"],
                                  reason="Malformed substitute output for fault testing."),
                    usage={"prompt_tokens": 0, "completion_tokens": 0},
                    model=SUBSTITUTE_NAME)
            self._record("ok", started)
            return decision
        finally:
            with self._lock:
                self.in_flight -= 1

    def _record(self, outcome: str, started: float) -> None:
        with self._lock:
            self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1
            self.observed_latencies.append(time.perf_counter() - started)

    def _decision(self, observation) -> DecisionResult:
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
        profile = self.profile
        usage = ({"prompt_tokens": profile.prompt_tokens,
                  "completion_tokens": profile.completion_tokens}
                 if profile.emit_usage else None)
        return DecisionResult(
            AgentDecision(agent_id=observation["agent_id"], action=action,
                          evidence_ids=[reference], reason=reason),
            usage=usage,
            model=SUBSTITUTE_NAME,
        )


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

        duplicate_cells = db.execute(
            "SELECT COUNT(*) FROM (SELECT round_number, agent_id FROM simulation_actions "
            "WHERE run_id=? GROUP BY round_number, agent_id HAVING COUNT(*) > 1)",
            (run_id,)).fetchone()[0]
        return {"actions": count("simulation_actions"),
                "messages": count("simulation_messages"),
                "memories": count("simulation_memories"),
                "request_metrics": count("simulation_request_metrics"),
                "rounds": count("simulation_rounds"),
                "duplicate_action_cells": duplicate_cells}


def invariant_verdicts(*, result: dict, counts: dict, performance: dict,
                       backend, concurrency: int, agents: int) -> dict:
    """Self-checks P1 requires: no double dispatch, no runaway, no lost opportunity.

    A failing verdict here means the run must not be cited as scale evidence.
    """
    action_counts = result["action_counts"]
    opportunities = result["opportunities"]
    buckets = opportunities["buckets"]
    billing = result.get("billing") or {}
    rounds = result["rounds"]
    completed = [row for row in rounds if not row.get("round_abandoned")]
    dispatched = action_counts.get("valid", 0) + action_counts.get("failed", 0)
    return {
        "no_duplicate_dispatch": {
            "verdict": counts["duplicate_action_cells"] == 0,
            "detail": "one action cell per (round, agent)",
            "duplicate_cells": counts["duplicate_action_cells"],
        },
        "concurrency_within_configuration": {
            "verdict": (backend.peak_in_flight <= concurrency and
                        (performance.get("maximum_observed_in_flight") or 0) <= concurrency),
            "detail": "substitute and engine in-flight peaks never exceed concurrency",
            "configured": concurrency,
            "substitute_peak": backend.peak_in_flight,
            "engine_peak": performance.get("maximum_observed_in_flight"),
        },
        # This is the whole-task reconciliation. It compares the four exclusive buckets
        # against the CONFIGURED opportunity total (agents x rounds), never against the
        # number of persisted rows: a run stopped at round 1 of 30 owes an accounting for
        # every opportunity it never reached.
        "whole_task_opportunities_reconciled": {
            "verdict": (opportunities["fully_reconciled"] is True and
                        sum(buckets.values()) == opportunities["total_opportunities"]),
            "detail": ("valid + failed + not_dispatched_current_round + "
                       "not_started_future_rounds equals agents x rounds"),
            "total_opportunities": opportunities["total_opportunities"],
            "buckets": buckets,
            "reconciled": opportunities["reconciled"],
            "abandoned_rounds": opportunities.get("abandoned_rounds"),
            "unreached_rounds": opportunities.get("unreached_rounds"),
        },
        "no_request_silently_lost": {
            "verdict": backend.calls == dispatched,
            "detail": ("every request the substitute actually received has a persisted "
                       "outcome: no completed decision was dropped from the ledger"),
            "requests_received": backend.calls,
            "persisted_dispatched": dispatched,
        },
        # Replaces an earlier check that could never fail for a zero-cost substitute.
        # This one compares the billing ledger against the charged run cost, so it does
        # real work for both priced and free backends.
        "billing_reconciles_with_run_cost": {
            "verdict": (bool(billing) and billing.get("cost_reconciles") is True and
                        billing.get("recorded_requests") ==
                        sum(billing.get("states", {}).values())),
            "detail": ("settled + unsettled_reserved equals the run cost, and every "
                       "recorded request has exactly one billing state"),
            "run_cost_cny": result["cost_cny"],
            "charged_cost_cny": billing.get("charged_cost_cny"),
            "states": billing.get("states"),
            "cost_by_state": billing.get("cost_by_state"),
        },
        "ledger_round_rows_match_completed_rounds": {
            "verdict": len(completed) == result["completed_rounds"],
            "detail": ("an abandoned round may add a ledger row, but it is flagged and is "
                       "not counted as a completed round"),
            "ledger_round_rows": len(rounds),
            "abandoned_rounds": len(rounds) - len(completed),
            "completed_rounds": result["completed_rounds"],
        },
    }


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
                   profile: FaultProfile | None = None,
                   deadline_monotonic: float | None = None,
                   deadline_ms: float | None = None,
                   budget_cny: Decimal = DEFAULT_TASK_BUDGET_CNY) -> dict:
    """Run one graded load level on a fresh local database and return its ledger.

    ``profile`` is authoritative when supplied; ``latency_ms`` only builds a default
    fixed-latency profile when no profile is given.
    """
    if profile is None:
        profile = FaultProfile(latency_seconds=latency_ms / 1000.0, error_every=fail_every,
                               seed=seed)
    with tempfile.TemporaryDirectory(prefix="scale-benchmark-") as temp:
        root = Path(temp)
        store, engine, case_id, graph_id = prepare_benchmark_store(root)
        run_id = engine.create_run(case_id, graph_id, agent_count=agents, rounds=rounds,
                                   concurrency=concurrency, seed=seed,
                                   budget_cny=budget_cny)
        config = engine.summary(run_id)["config"]
        backend = SubstituteBackend(profile)
        started = time.perf_counter()
        if deadline_monotonic is None and deadline_ms:
            # Relative to the moment the run actually starts, not to argument parsing.
            deadline_monotonic = time.monotonic() + deadline_ms / 1000.0
        result = engine.advance(run_id, backend, collect_performance=True,
                                deadline_monotonic=deadline_monotonic,
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
        latencies = sorted(backend.observed_latencies)

        def percentile(fraction):
            if not latencies:
                return None
            index = max(0, math.ceil(fraction * len(latencies)) - 1)
            return round(latencies[index], 6)

        verdicts = invariant_verdicts(result=result, counts=counts,
                                      performance=performance, backend=backend,
                                      concurrency=concurrency, agents=agents)

    return {
        "load": {"agents": agents, "rounds": rounds, "concurrency": concurrency,
                 "latency_ms": latency_ms, "seed": seed, "fail_every": fail_every,
                 "failure_policy": failure_policy or "continue",
                 "failure_policy_applied": effective_policy,
                 "decision_opportunities": requests},
        "fault_profile": profile.describe(),
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
            "dispatched_decisions": (result["action_counts"].get("valid", 0) +
                                     result["action_counts"].get("failed", 0)),
            "stopped_or_partial": result["status"] != "complete",
            "cost_cny": result["cost_cny"],
            "estimated_cost_cny": result["cost_cny"],
        },
        "opportunities": result["opportunities"],
        "billing": result.get("billing"),
        "invariants": verdicts,
        "resources": {
            "peak_process_rss_bytes": rss,
            "sqlite_bytes": db_bytes,
            "rows": counts,
            "substitute_peak_in_flight": backend.peak_in_flight,
            "engine_observed_max_in_flight": performance.get("maximum_observed_in_flight"),
            "substitute_call_count": backend.calls,
            "substitute_outcomes": dict(backend.outcomes),
            "substitute_burst_failures": backend.burst_failures,
            "substitute_held_calls": backend.held_calls,
            "substitute_mode": backend.mode,
            "substitute_priced": profile.priced,
            "substitute_latency_seconds": {
                "configured": latency_ms / 1000.0,
                "shape": profile.latency_shape,
                "p50_observed": percentile(0.50),
                "p95_observed": percentile(0.95),
                "p99_observed": percentile(0.99),
                "max_observed": round(latencies[-1], 6) if latencies else None,
                "mean_observed": (round(statistics.fmean(latencies), 6)
                                  if latencies else None),
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


def parse_burst(raw: str | None) -> tuple:
    if not raw:
        return ()
    return tuple(sorted({int(part) for part in raw.split(",") if part.strip()}))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--level", action="append", default=None,
                        help="agents:rounds:concurrency:latency_ms (repeatable)")
    parser.add_argument("--label", default="simulation-scale-load")
    parser.add_argument("--seed", type=int, default=20261011)
    parser.add_argument("--failure-policy", choices=["stop", "continue"], default=None)
    parser.add_argument("--out", default=None, help="ledger JSON path; stdout when omitted")
    # Fault and latency injection. All of these are engineering test settings.
    parser.add_argument("--latency-shape", choices=["fixed", "lognormal"],
                        default="fixed")
    parser.add_argument("--latency-sigma", type=float, default=0.9)
    parser.add_argument("--latency-tail-cap", type=float, default=10.0)
    parser.add_argument("--timeout-after-ms", type=float, default=0.0,
                        help="a timed-out request really blocks this long before failing")
    parser.add_argument("--timeout-every", type=int, default=0)
    parser.add_argument("--rate-limit-per-second", type=float, default=0.0,
                        help="admission cap; excess requests wait then get a 429")
    parser.add_argument("--rate-limit-every", type=int, default=0)
    parser.add_argument("--invalid-output-every", type=int, default=0)
    parser.add_argument("--error-every", type=int, default=0)
    parser.add_argument("--burst-error-at", default=None,
                        help="comma-separated 1-based call indices that all fail")
    parser.add_argument("--hold-calls", type=int, default=0,
                        help="first N calls genuinely block for --hold-ms")
    parser.add_argument("--hold-ms", type=float, default=0.0)
    # Simulated prices: engineering test rates for reservation/settlement/release.
    parser.add_argument("--reservation-cny", default="0")
    parser.add_argument("--input-cny-per-million", default="0")
    parser.add_argument("--cached-input-cny-per-million", default="0")
    parser.add_argument("--output-cny-per-million", default="0")
    parser.add_argument("--prompt-tokens", type=int, default=0)
    parser.add_argument("--completion-tokens", type=int, default=0)
    parser.add_argument("--omit-usage", action="store_true",
                        help="return no token usage, to model an unknown-usage request")
    parser.add_argument("--deadline-ms", type=float, default=0.0,
                        help="stop dispatching this long after the run starts")
    arguments = parser.parse_args(argv)
    levels = parse_levels(arguments.level) if arguments.level else DEFAULT_LEVELS

    measured = []
    for agents, rounds, concurrency, latency_ms in levels:
        profile = FaultProfile(
            latency_seconds=latency_ms / 1000.0,
            latency_shape=arguments.latency_shape,
            latency_sigma=arguments.latency_sigma,
            latency_tail_cap=arguments.latency_tail_cap,
            timeout_after_seconds=arguments.timeout_after_ms / 1000.0,
            timeout_every=arguments.timeout_every,
            rate_limit_per_second=arguments.rate_limit_per_second,
            rate_limit_every=arguments.rate_limit_every,
            invalid_output_every=arguments.invalid_output_every,
            error_every=arguments.error_every,
            burst_error_at=parse_burst(arguments.burst_error_at),
            hold_calls=arguments.hold_calls,
            hold_seconds=arguments.hold_ms / 1000.0,
            reservation_cny=arguments.reservation_cny,
            input_cny_per_million=arguments.input_cny_per_million,
            cached_input_cny_per_million=arguments.cached_input_cny_per_million,
            output_cny_per_million=arguments.output_cny_per_million,
            prompt_tokens=arguments.prompt_tokens,
            completion_tokens=arguments.completion_tokens,
            emit_usage=not arguments.omit_usage,
            seed=arguments.seed,
        )
        print(f"running agents={agents} rounds={rounds} concurrency={concurrency} "
              f"latency_ms={latency_ms} shape={profile.latency_shape} "
              f"priced={profile.priced}", flush=True)
        deadline = (time.monotonic() + arguments.deadline_ms / 1000.0
                    if arguments.deadline_ms > 0 else None)
        level = run_load_level(agents=agents, rounds=rounds, concurrency=concurrency,
                               latency_ms=latency_ms, seed=arguments.seed,
                               failure_policy=arguments.failure_policy, profile=profile,
                               deadline_monotonic=deadline)
        measured.append(level)
        print(json.dumps({"load": level["load"], "outcome": level["outcome"],
                          "buckets": level["opportunities"]["buckets"],
                          "billing_states": (level["billing"] or {}).get("states")},
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
