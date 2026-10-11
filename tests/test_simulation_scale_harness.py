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
