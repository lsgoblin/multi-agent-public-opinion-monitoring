"""Runtime message effect, independent state and memory isolation.

The probe these tests exercise lives in ``scripts/simulation_message_effect_probe.py``.
It runs one seeded scenario twice with the same decision rule: the treatment arm can
see the runtime message an agent received, the control arm cannot. Everything else --
personas, neighbours, evidence, own memory, per-agent state -- is identical, and that
identity is asserted on the frozen observation rather than assumed.
"""

import importlib.util
import json
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


def test_receiver_input_is_identical_except_for_the_runtime_message(evidence):
    frozen = evidence["frozen_input_check"]
    assert frozen["differing_observation_keys"] == []
    assert frozen["frozen_observations_equal_apart_from_messages"] is True
    assert frozen["treatment_without_messages_sha256"] == (
        frozen["control_without_messages_sha256"])
    for field in ("state_equal", "memories_equal", "evidence_equal", "persona_equal",
                  "neighbours_equal"):
        assert frozen[field] is True, field


def test_only_the_message_list_differs_between_the_two_arms(evidence):
    frozen = evidence["frozen_input_check"]
    # The engine froze the same message for both arms; the control rule simply was
    # not shown it, which is what makes the difference attributable to the message.
    assert frozen["treatment_frozen_messages"] == frozen["control_frozen_messages"]
    assert len(frozen["treatment_frozen_messages"]) == 1
    assert frozen["treatment_effective_messages"] == frozen["treatment_frozen_messages"]
    assert frozen["control_effective_messages"] == []


def test_request_opportunities_are_equal_before_the_decision_differs(evidence):
    """Both arms did the same work up to the point of interest."""
    sender = evidence["sender_round_1"]
    assert sender["identical"] is True
    assert sender["treatment_action"]["action"] == "share"
    assert evidence["runs"]["treatment"]["action_counts"] == (
        evidence["runs"]["control"]["action_counts"])


def test_received_message_changes_the_next_decision_and_is_cited(evidence):
    receiver = evidence["receiver_round_2"]
    assert receiver["treatment_status"] == receiver["control_status"] == "valid"
    assert receiver["decision_changed"] is True
    assert receiver["treatment_cited_received_message"] is True
    message_id = evidence["frozen_input_check"]["treatment_frozen_messages"][0][
        "message_id"]
    assert receiver["treatment_decision"]["evidence_ids"] == [message_id]
    assert message_id in receiver["treatment_observation_refs"]["messages"]
    # Both arms were frozen with the same visible message, so the recorded
    # observation reference is identical; what differs is what each rule could read.
    assert receiver["control_observation_refs"]["messages"] == (
        receiver["treatment_observation_refs"]["messages"])
    # The control arm could not read the message, so it fell back on its own memory.
    assert receiver["control_decision"]["evidence_ids"] == (
        receiver["control_observation_refs"]["memories"])
    assert receiver["treatment_decision"] != receiver["control_decision"]


@pytest.mark.parametrize("arm", ["treatment", "control"])
def test_agents_keep_independent_state_and_memory(evidence, arm):
    isolation = evidence["isolation"][arm]
    assert isolation["agents"] == 3
    # A distinct state per agent, not one shared blob.
    assert isolation["distinct_agent_states"] == isolation["agents"]
    # No memory row or visible memory belongs to another agent.
    assert isolation["memory_rows_naming_another_agent"] == 0
    assert isolation["foreign_memories_visible"] == 0
    # No visibility of the current or a future round.
    assert isolation["memories_from_current_or_future_round"] == 0
    assert isolation["messages_delivered_to_wrong_inbox"] == 0
    assert isolation["messages_not_from_the_previous_round"] == 0


def test_probe_records_zero_model_calls_and_network_requests(evidence):
    method = evidence["method"]
    assert method["is_real_model"] is False
    assert method["model_calls"] == 0
    assert method["network_requests"] == 0
    assert evidence["runs"]["treatment"]["all_decisions_valid"] is True
    assert evidence["runs"]["control"]["all_decisions_valid"] is True
    # The evidence must be serialisable and free of backend response text.
    assert "provider" not in json.dumps(evidence).lower()
