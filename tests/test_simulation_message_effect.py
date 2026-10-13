"""Runtime message effect, memory effect and isolation boundaries.

The probe these tests exercise lives in ``scripts/simulation_message_effect_probe.py``.
It runs two *separate* paired contrasts with one shared decision rule:

* message control -- evidence, persona, state, neighbours and own memory held fixed,
  only visibility of the runtime message varies;
* memory control -- evidence, persona, state, neighbours and received messages held
  fixed, only visibility of the agent's own persisted memory varies.

What is compared and hashed is the **effective observation handed to the decision
component**, not the pre-intervention observation. The claim these tests support is
therefore: the engine routes a runtime message and an agent's isolated memory into that
agent's decision input, and a decision difference is attributable to that input. They do
**not** support any claim about how a real language model would behave, and no
multi-round "only the message differs" claim is made.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_probe():
    path = ROOT / "scripts" / "simulation_message_effect_probe.py"
    spec = importlib.util.spec_from_file_location("message_effect_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def evidence():
    return load_probe().probe(agents=3, rounds=3, seed=20261011)


@pytest.fixture(scope="module")
def message_control(evidence):
    return evidence["message_control"]


@pytest.fixture(scope="module")
def memory_control(evidence):
    return evidence["memory_control"]


def assert_single_field_intervention(pair):
    """Everything except the intervened field is identical in the decision input."""
    assert pair["effective_input_sha256"]["identical"] is True
    assert pair["effective_input_differing_keys"] == []
    assert pair["frozen_input_equal_except_intervention"] is True
    assert pair["state_equal"] is True
    assert pair["persona_equal"] is True
    assert pair["neighbours_equal"] is True
    assert pair["evidence_equal"] is True
    # The intervention really did differ, otherwise the contrast proves nothing.
    assert pair["intervened_value"]["treatment_count"] >= 1
    assert pair["intervened_value"]["control_count"] == 0


def test_message_control_isolates_the_runtime_message(message_control):
    assert message_control["intervened_field"] == "messages"
    assert message_control["held_fixed"] == ["memories"]
    assert_single_field_intervention(message_control)


def test_memory_control_isolates_the_agents_own_memory(memory_control):
    assert memory_control["intervened_field"] == "memories"
    assert memory_control["held_fixed"] == ["messages"]
    assert_single_field_intervention(memory_control)


def test_message_visibility_changes_the_next_decision(message_control):
    decision = message_control["decision"]
    assert decision["changed"] is True
    assert decision["treatment_cited_intervention"] is True
    assert decision["control_cited_intervention"] is False
    message_id = message_control["intervened_value"]["treatment"][0]["message_id"]
    assert decision["treatment"]["evidence_ids"] == [message_id]
    assert decision["treatment"]["action"] == "comment"


def test_memory_visibility_changes_the_next_decision(memory_control):
    decision = memory_control["decision"]
    assert decision["changed"] is True
    assert decision["treatment_cited_intervention"] is True
    assert decision["control_cited_intervention"] is False
    memory_id = memory_control["intervened_value"]["treatment"][0]["memory_id"]
    assert decision["treatment"]["evidence_ids"] == [memory_id]
    assert decision["treatment"]["action"] == "seek_clarification"
    # Without its own memory the control agent falls back to plain observation.
    assert decision["control"]["action"] == "observe"


@pytest.mark.parametrize("pair_name", ["message_control", "memory_control"])
@pytest.mark.parametrize("arm", ["treatment", "control"])
def test_agents_keep_independent_state_and_memory(evidence, pair_name, arm):
    isolation = evidence[pair_name]["isolation"][arm]
    assert isolation["agents"] == 3
    # A distinct state per agent, not one shared blob.
    assert isolation["distinct_agent_states"] == isolation["agents"]
    assert isolation["memory_rows_naming_another_agent"] == 0
    assert isolation["foreign_memories_visible"] == 0
    assert isolation["memories_from_current_or_future_round"] == 0
    assert isolation["messages_delivered_to_wrong_inbox"] == 0
    assert isolation["messages_not_from_the_previous_round"] == 0


def test_unrelated_addresses_and_future_rounds_are_never_visible(evidence):
    checks = evidence["isolation_checks"]
    assert checks["violations"] == []
    assert checks["violation_count"] == 0
    assert checks["inbox_mismatches"] == 0
    assert checks["all_stored_memories_owned"] is True
    assert checks["messages_considered"] > 0
    assert checks["memories_considered"] > 0


def test_probe_declares_its_limits_and_records_no_model_calls(evidence):
    method = evidence["method"]
    assert method["is_real_model"] is False
    assert method["model_calls"] == 0
    assert method["network_requests"] == 0
    # A rule-based decision component must be declared as such.
    assert "rule" in method["rule_based_limit"].lower()
    assert "multi-round" in method["multi_round_limit"].lower()
    assert any("real language model" in item for item in method["does_not_support"])
    plan = evidence["real_model_experiment_plan"]
    assert plan["status"] == "not_executed_no_authorization"
    assert plan["planned_size"]["total_calls"] == 4
    for pair in ("message_control", "memory_control"):
        assert evidence[pair]["runs"]["treatment"]["all_decisions_valid"] is True
        assert evidence[pair]["runs"]["control"]["all_decisions_valid"] is True
