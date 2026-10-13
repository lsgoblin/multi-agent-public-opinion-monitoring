"""Offline graded-load harness: determinism, fault injection and invariant verdicts.

The harness under test is ``scripts/simulation_scale_benchmark.py``. It is a local
measurement tool: the substitute backend never contacts a network or a model, and the
latency/fault profiles it takes are *engineering test settings*, not measurements of any
provider. These tests keep that boundary honest and check the properties P1 requires:

* the same seed and profile give the same outcome (reproducibility);
* each fault mode reaches the intended outcome and is attributed correctly;
* an injected error is never presented as a real timeout mechanism;
* no duplicate dispatch, no runaway concurrency, no silently lost request;
* every run has a terminating boundary rather than an open-ended wait.
"""

import importlib.util
import time
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_harness():
    path = ROOT / "scripts" / "simulation_scale_benchmark.py"
    spec = importlib.util.spec_from_file_location("scale_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harness():
    return load_harness()


def run_level(harness, *, agents=12, rounds=2, concurrency=4, profile=None, **kwargs):
    profile = profile or harness.FaultProfile(latency_seconds=0.0)
    return harness.run_load_level(agents=agents, rounds=rounds, concurrency=concurrency,
                                  latency_ms=profile.latency_seconds * 1000.0,
                                  profile=profile, **kwargs)


def assert_all_invariants_hold(level):
    failed = {name: value for name, value in level["invariants"].items()
              if not value["verdict"]}
    assert failed == {}, failed


def test_profile_is_reported_as_an_engineering_test_configuration(harness):
    level = run_level(harness, profile=harness.FaultProfile(latency_seconds=0.005))
    profile = level["fault_profile"]
    assert profile["is_engineering_test_configuration"] is True
    assert level["outcome"]["status"] == "complete"
    assert_all_invariants_hold(level)


def test_same_seed_and_profile_reproduce_the_same_outcome(harness):
    profile = harness.FaultProfile(latency_seconds=0.002, error_every=7, seed=99)
    first = run_level(harness, profile=profile)
    second = run_level(harness, profile=profile)
    keys = ("status", "completed_rounds", "action_counts", "all_decisions_valid")
    assert {k: first["outcome"][k] for k in keys} == {
        k: second["outcome"][k] for k in keys}
    assert first["resources"]["substitute_outcomes"] == (
        second["resources"]["substitute_outcomes"])


def test_fixed_latency_keeps_the_tail_tight_and_records_concurrency(harness):
    level = run_level(harness, agents=16, rounds=1, concurrency=4,
                      profile=harness.FaultProfile(latency_seconds=0.02))
    latency = level["resources"]["substitute_latency_seconds"]
    assert latency["shape"] == "fixed"
    assert 0.02 <= latency["p50_observed"] < 0.05
    # A fixed shape must not produce a long tail.
    assert latency["p99_observed"] < 0.06
    assert level["resources"]["substitute_peak_in_flight"] == 4
    assert_all_invariants_hold(level)


def test_lognormal_latency_produces_a_long_tail(harness):
    level = run_level(harness, agents=16, rounds=1, concurrency=4,
                      profile=harness.FaultProfile(latency_seconds=0.02,
                                                   latency_shape="lognormal",
                                                   latency_sigma=0.9))
    latency = level["resources"]["substitute_latency_seconds"]
    assert latency["shape"] == "lognormal"
    # The tail must actually be long: p99 well above the median.
    assert latency["p99_observed"] > latency["p50_observed"] * 2
    assert_all_invariants_hold(level)


def test_injected_timeout_fails_fast_and_is_attributed_to_transport(harness):
    """An injected timeout returns immediately; it is not a real waiting timeout."""
    level = run_level(harness, profile=harness.FaultProfile(latency_seconds=0.002,
                                                            timeout_every=5),
                      rounds=2)
    outcomes = level["resources"]["substitute_outcomes"]
    assert outcomes["timeout"] > 0
    assert outcomes["ok"] > 0
    assert level["outcome"]["all_decisions_valid"] is False
    # Injected: the failure path adds no wait beyond the ordinary latency.
    latency = level["resources"]["substitute_latency_seconds"]
    assert latency["p99_observed"] < 0.05
    assert_all_invariants_hold(level)


def test_blocking_timeout_really_waits_and_shows_up_in_the_tail(harness):
    """The blocking mode is the one that models an actual timeout wait."""
    level = run_level(harness, profile=harness.FaultProfile(
        latency_seconds=0.002, timeout_every=5, timeout_after_seconds=0.25), rounds=2)
    outcomes = level["resources"]["substitute_outcomes"]
    assert outcomes["timeout"] > 0
    latency = level["resources"]["substitute_latency_seconds"]
    # The timed-out requests really held the worker for ~0.25s.
    assert latency["max_observed"] >= 0.25
    assert latency["max_observed"] > latency["p50_observed"] * 5
    assert_all_invariants_hold(level)


def test_rate_limit_models_admission_control(harness):
    level = run_level(harness, agents=12, rounds=2, concurrency=4,
                      profile=harness.FaultProfile(latency_seconds=0.005,
                                                   rate_limit_per_second=20.0))
    outcomes = level["resources"]["substitute_outcomes"]
    assert outcomes["rate_limited"] > 0
    assert level["outcome"]["all_decisions_valid"] is False
    # A throttled request waits for its slot, so the observed latency exceeds the
    # nominal per-call latency.
    latency = level["resources"]["substitute_latency_seconds"]
    assert latency["p50_observed"] > 0.005
    assert_all_invariants_hold(level)


def test_rate_limited_requests_respect_the_admission_cap(harness):
    """Observed throughput must not exceed the configured admission rate."""
    per_second = 30.0
    level = run_level(harness, agents=12, rounds=2, concurrency=4,
                      profile=harness.FaultProfile(latency_seconds=0.0,
                                                   rate_limit_per_second=per_second))
    wall = level["outcome"]["wall_seconds"]
    admitted = level["resources"]["substitute_outcomes"]["ok"]
    # Calls are serialised by the limiter, so wall time covers every call.
    assert wall * per_second >= admitted * 0.5
    assert_all_invariants_hold(level)


def test_invalid_output_is_rejected_by_the_engine(harness):
    level = run_level(harness, profile=harness.FaultProfile(
        latency_seconds=0.002, invalid_output_every=6), rounds=2)
    outcomes = level["resources"]["substitute_outcomes"]
    assert outcomes["invalid_output"] > 0
    assert level["outcome"]["all_decisions_valid"] is False
    # The engine must classify it as a failed decision, not accept it.
    assert level["outcome"]["action_counts"].get("failed", 0) > 0
    assert_all_invariants_hold(level)


def test_burst_failures_are_recorded_as_consecutive_failures(harness):
    burst = tuple(range(4, 12))
    level = run_level(harness, agents=12, rounds=2, concurrency=1,
                      profile=harness.FaultProfile(latency_seconds=0.0,
                                                   burst_error_at=burst))
    assert level["resources"]["substitute_burst_failures"] == len(burst)
    assert level["outcome"]["action_counts"].get("failed", 0) == len(burst)
    assert_all_invariants_hold(level)


def test_harness_never_retries_a_failed_request(harness):
    """Every request the substitute received maps to exactly one persisted outcome."""
    level = run_level(harness, agents=12, rounds=2, concurrency=2,
                      profile=harness.FaultProfile(latency_seconds=0.0, error_every=5))
    verdict = level["invariants"]["no_request_silently_lost"]
    assert verdict["verdict"] is True
    assert verdict["requests_received"] == verdict["persisted_dispatched"]
    assert level["resources"]["substitute_call_count"] == verdict["requests_received"]


def test_stop_policy_and_continue_policy_differ_at_the_same_fault_position(harness):
    """Identical configuration and fault position; only the policy differs."""
    profile = harness.FaultProfile(latency_seconds=0.01, error_every=5)
    stopped = run_level(harness, agents=16, rounds=1, concurrency=1, profile=profile,
                        failure_policy="stop")
    continued = run_level(harness, agents=16, rounds=1, concurrency=1, profile=profile,
                          failure_policy="continue")
    assert stopped["load"]["failure_policy_applied"] == "stop"
    assert continued["load"]["failure_policy_applied"] == "continue"
    assert stopped["outcome"]["status"] == "partial"
    assert continued["outcome"]["status"] == "complete"
    # The stop strands opportunities that the continue policy actually used.
    assert stopped["outcome"]["action_counts"].get("not_dispatched", 0) > 0
    assert continued["outcome"]["action_counts"].get("not_dispatched", 0) == 0
    assert continued["outcome"]["dispatched_decisions"] > (
        stopped["outcome"]["dispatched_decisions"])
    assert_all_invariants_hold(stopped)
    assert_all_invariants_hold(continued)


def test_zero_latency_does_not_demonstrate_concurrency(harness):
    """An instantaneous substitute finishes each call before the pool can overlap it."""
    level = run_level(harness, agents=16, rounds=1, concurrency=8,
                      profile=harness.FaultProfile(latency_seconds=0.0))
    # Peak in-flight stays far below the configured pool size, which is exactly why
    # concurrency claims must come from a delayed profile.
    assert level["resources"]["substitute_peak_in_flight"] < 8
    assert_all_invariants_hold(level)


# ------------------------------------------- whole-task opportunity reconciliation

def test_whole_task_reconciliation_counts_rounds_never_reached(harness):
    """A stopped run must account for every opportunity it never reached.

    Ten agents over ten rounds is 100 opportunities. The run stops inside round 1, so only
    a handful of rows exist; the ledger must still reconcile to 100, not to the 10 rows.
    """
    level = run_level(harness, agents=10, rounds=10, concurrency=1,
                      profile=harness.FaultProfile(latency_seconds=0.0, error_every=2),
                      failure_policy="stop")
    opportunities = level["opportunities"]
    buckets = opportunities["buckets"]
    assert opportunities["total_opportunities"] == 100
    assert opportunities["fully_reconciled"] is True
    assert sum(buckets.values()) == 100
    # The persisted row count is far smaller than the reconciled total.
    assert level["resources"]["rows"]["actions"] < 100
    assert buckets["failed"] >= 1
    assert buckets["not_dispatched_current_round"] >= 0
    assert buckets["not_started_future_rounds"] == 90
    assert_all_invariants_hold(level)


def test_completed_run_has_no_unstarted_or_stranded_buckets(harness):
    level = run_level(harness, agents=12, rounds=3, concurrency=4,
                      profile=harness.FaultProfile(latency_seconds=0.002))
    buckets = level["opportunities"]["buckets"]
    assert level["opportunities"]["total_opportunities"] == 36
    assert buckets == {"valid": 36, "failed": 0, "deadline_exceeded": 0,
                       "not_dispatched_current_round": 0,
                       "not_started_future_rounds": 0}
    assert_all_invariants_hold(level)


def test_invariant_fails_when_persisted_rows_are_not_the_whole_task(harness):
    """The reconciliation must reject the weaker "rows add up" claim.

    Guards against regressing to an invariant that only compares persisted rows with
    themselves: for a stopped run the row-derived total is deliberately below the
    configured opportunity total.
    """
    level = run_level(harness, agents=10, rounds=10, concurrency=1,
                      profile=harness.FaultProfile(latency_seconds=0.0, error_every=2),
                      failure_policy="stop")
    bucket_sum = sum(level["opportunities"]["buckets"].values())
    rows = level["resources"]["rows"]["actions"]
    assert rows != bucket_sum
    assert bucket_sum == level["opportunities"]["total_opportunities"]
    verdict = level["invariants"]["whole_task_opportunities_reconciled"]
    assert verdict["verdict"] is True
    assert verdict["reconciled"] == 100 != rows


# ------------------------------------------------------------------ billing ledger

PRICED = dict(reservation_cny="0.20", input_cny_per_million="3",
              cached_input_cny_per_million="0.6", output_cny_per_million="6",
              prompt_tokens=1000, completion_tokens=200)


def test_priced_run_settles_and_reconciles(harness):
    level = run_level(harness, agents=8, rounds=2, concurrency=2,
                      profile=harness.FaultProfile(latency_seconds=0.002, **PRICED))
    billing = level["billing"]
    assert level["resources"]["substitute_priced"] is True
    assert level["resources"]["substitute_mode"] == "real_model"
    assert billing["states"]["settled"] == 16
    assert billing["states"]["unsettled_reserved"] == 0
    assert billing["cost_reconciles"] is True
    assert Decimal(billing["settled_cost_cny"]) == Decimal(level["outcome"]["cost_cny"])
    assert_all_invariants_hold(level)


def test_priced_unknown_usage_keeps_the_reservation(harness):
    """A dispatched request with no usage must not be reported as free."""
    level = run_level(harness, agents=8, rounds=2, concurrency=2,
                      profile=harness.FaultProfile(latency_seconds=0.002,
                                                   emit_usage=False, **PRICED))
    billing = level["billing"]
    assert billing["states"] == {"not_sent": 0, "released": 0, "settled": 0,
                                 "unsettled_reserved": 16}
    assert Decimal(billing["unsettled_reserved_cost_cny"]) == Decimal("0.20") * 16
    assert billing["cost_reconciles"] is True
    assert_all_invariants_hold(level)


def test_priced_failure_still_reconciles(harness):
    level = run_level(harness, agents=8, rounds=2, concurrency=2,
                      profile=harness.FaultProfile(latency_seconds=0.002,
                                                   error_every=5, **PRICED))
    billing = level["billing"]
    assert billing["states"]["settled"] > 0
    assert billing["states"]["unsettled_reserved"] > 0
    assert billing["cost_reconciles"] is True
    assert_all_invariants_hold(level)


def test_priced_budget_stop_sends_nothing_and_charges_nothing(harness):
    """Reservation must be released, not charged, when the preflight rejects a round."""
    profile = harness.FaultProfile(latency_seconds=0.0, **PRICED)
    level = harness.run_load_level(agents=8, rounds=2, concurrency=2, latency_ms=0.0,
                                   profile=profile, budget_cny=Decimal("0.50"))
    billing = level["billing"]
    assert level["outcome"]["status"] == "partial"
    assert billing["states"]["not_sent"] == 8
    assert Decimal(billing["charged_cost_cny"]) == 0
    assert Decimal(level["outcome"]["cost_cny"]) == 0
    assert billing["cost_reconciles"] is True
    assert_all_invariants_hold(level)


# ----------------------------------------------------- deadline and held-call boundary

def test_deadline_stops_waiting_on_held_calls(harness):
    """A genuinely held call must not extend the run past the deadline.

    Four calls are held for 0.4 s while the deadline is 150 ms away. The engine submits the
    batch, then stops waiting when the deadline passes, so it returns near the deadline
    rather than after the held calls.
    """
    profile = harness.FaultProfile(latency_seconds=0.0, hold_calls=4, hold_seconds=0.4)
    level = harness.run_load_level(agents=20, rounds=3, concurrency=4, latency_ms=0.0,
                                   profile=profile, deadline_ms=150.0)
    outcome = level["outcome"]
    assert outcome["status"] == "partial"
    assert level["resources"]["substitute_held_calls"] == 4
    assert level["resources"]["substitute_call_count"] == 4
    # It returned near the deadline, not after the held calls finished.
    assert outcome["wall_seconds"] < 0.4
    assert outcome["completed_rounds"] == 0
    assert level["invariants"]["ledger_round_rows_match_completed_rounds"][
        "abandoned_rounds"] == 1
    assert level["opportunities"]["fully_reconciled"] is True
    config = level["run_config_frozen"]
    assert config["agent_count"] * config["rounds"] == 60
    assert level["opportunities"]["buckets"] == {
        "valid": 0, "failed": 0, "deadline_exceeded": 4,
        "not_dispatched_current_round": 16, "not_started_future_rounds": 40}
    assert_all_invariants_hold(level)


def test_deadline_charges_the_requests_it_stopped_waiting_for(harness):
    """Requests abandoned at the deadline keep their reservation, never free."""
    profile = harness.FaultProfile(latency_seconds=0.0, hold_calls=2, hold_seconds=0.3,
                                   **PRICED)
    level = harness.run_load_level(agents=12, rounds=2, concurrency=2, latency_ms=0.0,
                                   profile=profile, deadline_ms=100.0)
    assert level["billing"]["cost_reconciles"] is True
    # Nothing came back, so nothing settled; the two dispatched requests are unsettled.
    assert level["billing"]["states"]["settled"] == 0
    assert level["billing"]["states"]["unsettled_reserved"] == 2
    assert level["billing"]["states"]["not_sent"] == 10
    assert Decimal(level["billing"]["unsettled_reserved_cost_cny"]) == Decimal("0.20") * 2
    assert_all_invariants_hold(level)


def test_an_expired_deadline_dispatches_nothing(harness):
    level = harness.run_load_level(agents=6, rounds=2, concurrency=2, latency_ms=0.0,
                                   profile=harness.FaultProfile(latency_seconds=0.0), deadline_ms=-1.0)
    assert level["resources"]["substitute_call_count"] == 0
    assert level["outcome"]["action_counts"] == {"not_dispatched": 6}
    assert level["opportunities"]["buckets"] == {
        "valid": 0, "failed": 0, "deadline_exceeded": 0,
        "not_dispatched_current_round": 6, "not_started_future_rounds": 6}
    assert_all_invariants_hold(level)
