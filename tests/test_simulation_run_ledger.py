"""Tests for the run-ledger export the evaluation gate reconciles against.

The gate's conditions are asserted here from the producer's side, so a ledger
that cannot pass says so in the producer's own terms instead of failing later as
an opaque rejection reason from the consumer.
"""

from __future__ import annotations

import json

import pytest

from riskshield.simulation_run_ledger import (
    RUN_LEDGER_SCHEMA,
    build_run_ledger,
    canonical_digest,
    ledger_digests,
    reconciliation_report,
)


def _summary(**overrides) -> dict:
    summary = {
        "run_id": "run_test_0001", "case_id": "case_test",
        "graph_id": "graph_test",
        "config": {"agent_count": 2, "rounds": 2, "backend_mode": "real_model",
                   "data_mode": "real_historical", "cutoff": "2024-02-22T00:00:00Z",
                   "source_version": "v1"},
        "status": "complete", "completed_rounds": 2,
        "rounds": [{"round": 1, "round_abandoned": False},
                   {"round": 2, "round_abandoned": False}],
        "cost_cny": "0.01",
    }
    summary.update(overrides)
    return summary


def _action(round_number: int, agent_id: str, **overrides) -> dict:
    action = {
        "run_id": "run_test_0001", "round_number": round_number, "agent_id": agent_id,
        "status": "valid", "decision": {"action": "share", "evidence_ids": []},
        "observation_refs": {"evidence": [], "messages": [], "memories": []},
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cached_input_tokens": 0},
        "requested_model": "qwen-flash", "model": "qwen-flash",
        "price_version": "test", "cost_cny": "0.001",
    }
    action.update(overrides)
    return action


def _full_trajectory() -> list:
    return [_action(r, "agent_%04d" % i)
            for r in (1, 2) for i in range(1, 3)]


def test_the_ledger_carries_the_engine_exports_verbatim():
    summary, trajectory = _summary(), _full_trajectory()
    ledger = build_run_ledger(summary, trajectory)

    assert ledger["schema"] == RUN_LEDGER_SCHEMA
    assert ledger["summary"] is summary
    assert ledger["trajectory"] is trajectory
    assert ledger["trajectory_action_count"] == 4


@pytest.mark.parametrize("bad_summary", [
    {"run_id": "x"},                 # missing case_id, config, status, rounds, completed_rounds
    {"run_id": "x", "case_id": "y", "config": "not-an-object",
     "status": "complete", "completed_rounds": 1, "rounds": []},
])
def test_a_malformed_summary_is_refused_rather_than_exported(bad_summary):
    with pytest.raises(ValueError):
        build_run_ledger(bad_summary, [])


def test_a_non_list_trajectory_is_refused():
    with pytest.raises(ValueError):
        build_run_ledger(_summary(), {"not": "a list"})


def test_the_digests_match_the_evaluators_canonical_form():
    ledger = build_run_ledger(_summary(), _full_trajectory())
    digests = ledger_digests(ledger)

    # The evaluator digests canonical JSON: sorted keys, compact separators.
    expected = canonical_digest(ledger)
    assert digests["run_ledger_sha256"] == expected
    assert digests["run_config_sha256"] == canonical_digest(ledger["summary"]["config"])
    # An unchanged document must reproduce its digest exactly.
    assert canonical_digest(json.loads(json.dumps(ledger))) == expected


def test_an_unchanged_run_reproduces_the_same_digest():
    first = build_run_ledger(_summary(), _full_trajectory())
    second = build_run_ledger(_summary(), _full_trajectory())
    assert ledger_digests(first) == ledger_digests(second)


def test_reconciliation_passes_every_gate_precondition_when_the_run_is_clean():
    report = reconciliation_report(build_run_ledger(_summary(), _full_trajectory()))
    assert report["gate_preconditions"] == {
        "real_historical_and_real_model": True,
        "status_complete": True,
        "all_configured_rounds_recorded": True,
        "no_abandoned_rounds": True,
        "action_count_matches_agents_times_rounds": True,
        "every_action_row_valid": True,
    }


def test_reconciliation_names_each_condition_that_fails():
    # A synthetic case with a substituted backend is exactly the state the local
    # archive is in: the ledger is well formed and still cannot enter the gate.
    summary = _summary()
    summary["config"] = dict(summary["config"], backend_mode="test_substitute",
                             data_mode="synthetic")
    report = reconciliation_report(build_run_ledger(summary, _full_trajectory()))
    assert report["gate_preconditions"]["real_historical_and_real_model"] is False
    assert report["backend_mode"] == "test_substitute"
    assert report["data_mode"] == "synthetic"
    # Everything else about the run is still consistent.
    assert report["gate_preconditions"]["action_count_matches_agents_times_rounds"] is True


def test_an_abandoned_round_is_named_and_fails_the_gate():
    summary = _summary()
    summary["rounds"] = [{"round": 1, "round_abandoned": False},
                         {"round": 2, "round_abandoned": True}]
    report = reconciliation_report(build_run_ledger(summary, _full_trajectory()))
    assert report["abandoned_round_rows"] == [2]
    assert report["gate_preconditions"]["no_abandoned_rounds"] is False


def test_a_short_trajectory_fails_the_action_count_condition():
    report = reconciliation_report(build_run_ledger(_summary(), _full_trajectory()[:3]))
    assert report["gate_preconditions"]["action_count_matches_agents_times_rounds"] is False
    assert report["expected_actions"] == 4
    assert report["trajectory_action_count"] == 3


def test_a_non_valid_action_row_fails_the_all_valid_condition():
    trajectory = _full_trajectory()
    trajectory[1] = _action(1, "agent_0002", status="deadline_exceeded", decision=None)
    report = reconciliation_report(build_run_ledger(_summary(), trajectory))
    assert report["gate_preconditions"]["every_action_row_valid"] is False
    assert report["valid_action_rows"] == 3


def test_the_report_totals_models_and_tokens_from_the_rows():
    trajectory = _full_trajectory()
    trajectory[0] = _action(1, "agent_0001", requested_model="qwen-turbo")
    report = reconciliation_report(build_run_ledger(_summary(), trajectory))
    assert report["model_calls_by_model"] == {"qwen-flash": 3, "qwen-turbo": 1}
    assert report["tokens"]["prompt_tokens"] == 40
    assert report["tokens"]["completion_tokens"] == 20


def test_export_refuses_to_overwrite_a_frozen_ledger(tmp_path):
    from riskshield.simulation_run_ledger import export_run_ledger

    class _StubSimulation:
        def __init__(self):
            self.summary_value = _summary()
            self.trajectory_value = _full_trajectory()

        def summary(self, run_id):
            return self.summary_value

        def trajectory(self, run_id):
            return self.trajectory_value

    out = tmp_path / "run-ledger-v1.json"
    document = export_run_ledger(_StubSimulation(), "run_test_0001", out)
    assert out.is_file()

    # The digest covers the ledger body, not the record that carries the digest:
    # including it would be self-referential. This is exactly how the evaluator
    # re-derives it -- it reads the file, strips the two digest fields and compares.
    stored = json.loads(out.read_text(encoding="utf-8"))
    body = {key: value for key, value in stored.items()
            if key not in ("run_ledger_sha256", "run_config_sha256")}
    assert document["run_ledger_sha256"] == canonical_digest(body)
    assert document["run_config_sha256"] == canonical_digest(stored["summary"]["config"])

    with pytest.raises(FileExistsError):
        export_run_ledger(_StubSimulation(), "run_test_0001", out)
