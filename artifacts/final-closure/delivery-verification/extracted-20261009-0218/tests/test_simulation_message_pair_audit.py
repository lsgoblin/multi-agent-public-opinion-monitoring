import copy
import hashlib
import json
import sqlite3
from decimal import Decimal

import pytest

import experiments.day3_simulation_message_pair_audit as message_pair_audit
from riskshield.simulation import Day3Simulation, DecisionResult
from experiments.day3_bailian_synthetic_backend import (BEIJING_BASE_URL, BailianBackend,
                                     BailianConfig, BailianRoute)
from experiments.day3_simulation_message_pair_audit import MessagePairAuditError, audit_message_pair_candidate, main
from tests.fixtures.day3_synthetic_case import CASE_ID, GRAPH_ID, prepare_synthetic_case
from riskshield.schemas import AgentDecision
from riskshield.store import Store


def make_run(tmp_path, *, with_frozen_audit=False):
    path = tmp_path / "synthetic-run.sqlite3"
    store = Store(path)
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    route = BailianRoute(
        model="qwen-turbo", price_version="offline-test", max_input_tokens=8192,
        max_output_tokens=160, input_cny_per_million=Decimal("0.3"),
        cached_input_cny_per_million=Decimal("0.06"),
        output_cny_per_million=Decimal("0.6"),
    )
    adapter = BailianBackend(BailianConfig("", BEIJING_BASE_URL,
                                           {"qwen-turbo": route}))

    class OfflineBackend:
        mode = "real_model"  # The test never constructs or calls an HTTP client.

        def describe(self):
            return adapter.describe()

        def pricing(self, observation):
            return adapter.pricing(observation)

        def decide(self, observation):
            if observation["round"] == 1 and observation["agent_id"] == "agent_0000":
                action = "share"
                ref = observation["evidence"][0]["evidence_id"]
            elif observation["messages"]:
                action = "comment"
                ref = observation["messages"][0]["message_id"]
            else:
                action = "observe"
                ref = observation["evidence"][0]["evidence_id"]
            return DecisionResult(
                AgentDecision(agent_id=observation["agent_id"], action=action,
                              evidence_ids=[ref], reason="synthetic-private-reason"),
                usage={"prompt_tokens": 20, "completion_tokens": 5},
                model="qwen-turbo-offline-test",
            )

    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=2, rounds=2,
                               concurrency=2, budget_cny=Decimal("1"))
    offline = OfflineBackend()
    if with_frozen_audit:
        def audited_decide(observation, record_audit):
            payload, _ = adapter.prepare_request(observation)
            record_audit(adapter._request_audit(payload))
            return offline.decide(observation)
        offline.decide_with_audit = audited_decide
    result = engine.advance(run_id, offline)
    assert result["status"] == "complete"
    return path, run_id


def test_message_pair_audit_is_read_only_and_outputs_only_hashes(tmp_path, monkeypatch, capsys):
    path, run_id = make_run(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    payloads = []
    real_hash = message_pair_audit._hash
    def capture_hash(payload):
        payloads.append(copy.deepcopy(payload))
        return real_hash(payload)
    monkeypatch.setattr(message_pair_audit, "_hash", capture_hash)
    monkeypatch.setattr("experiments.day3_bailian_synthetic_backend.httpx.Client", lambda *a, **k:
                        pytest.fail("message pair audit created an HTTP client"))
    report = audit_message_pair_candidate(path, run_id=run_id)
    assert report["audit_status"] == "paired_candidate_reconstructed"
    assert report["message_id"] == "msg_1_agent_0000_agent_0001"
    assert report["recipient_round"] == 2
    assert report["recipient_cited_message"] is True
    assert (report["reconstructed_treatment_payload_canonical_json_sha256"] !=
            report["reconstructed_control_payload_canonical_json_sha256"])
    assert "reconstructed" in report["hash_basis"]
    assert report["field_differences"] == ["received_simulated_messages",
                                            "allowed_citation_ids"]
    assert report["replay_checks"] == {
        "agent_personas_and_neighbors": 2,
        "observation_reference_rows": 4,
        "message_edges": 2,
        "memory_rows": 5,
        "final_agent_states": 2,
    }
    assert report["g3_causality_proven"] is False
    assert report["recorded_prepared_payload_hash_match"] is None
    assert "synthetic-private-reason" not in json.dumps(report)
    treatment, control = payloads
    assert {key: value for key, value in treatment.items() if key != "messages"} == {
        key: value for key, value in control.items() if key != "messages"}
    assert treatment["messages"][0] == control["messages"][0]
    treatment_fields = json.loads(treatment["messages"][1]["content"])
    control_fields = json.loads(control["messages"][1]["content"])
    assert {key: value for key, value in treatment_fields.items()
            if key not in report["field_differences"]} == {
                key: value for key, value in control_fields.items()
                if key not in report["field_differences"]}
    assert [m["message_id"] for m in treatment_fields["received_simulated_messages"]] == [
        report["message_id"]]
    assert control_fields["received_simulated_messages"] == []
    assert control_fields["allowed_citation_ids"] == [
        ref for ref in treatment_fields["allowed_citation_ids"]
        if ref != report["message_id"]]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert main(["--db", str(path), "--run-id", run_id]) == 0
    printed = capsys.readouterr().out
    assert "synthetic-private-reason" not in printed
    assert "api_key" not in printed
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_message_pair_audit_checks_recorded_prepared_payload_hash(tmp_path, monkeypatch):
    path, run_id = make_run(tmp_path, with_frozen_audit=True)
    monkeypatch.setattr("experiments.day3_bailian_synthetic_backend.httpx.Client", lambda *a, **k:
                        pytest.fail("message pair audit created an HTTP client"))
    report = audit_message_pair_candidate(path, run_id=run_id)
    assert report["recorded_prepared_payload_hash_match"] is True
    assert report["g3_causality_proven"] is False
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT request_audit FROM simulation_actions WHERE run_id=? "
                         "AND round_number=2 AND agent_id='agent_0001'",
                         (run_id,)).fetchone()
        audit = json.loads(row[0])
        audit["frozen_local_state_sha256"] = "0" * 64
        db.execute("UPDATE simulation_actions SET request_audit=? WHERE run_id=? "
                   "AND round_number=2 AND agent_id='agent_0001'",
                   (json.dumps(audit), run_id))
    with pytest.raises(MessagePairAuditError, match="recorded prepared-payload audit"):
        audit_message_pair_candidate(path, run_id=run_id)


@pytest.mark.parametrize("corruption", ["state", "message", "memory"])
def test_message_pair_audit_fails_closed_on_inconsistent_ledger(tmp_path, corruption):
    path, run_id = make_run(tmp_path)
    with sqlite3.connect(path) as db:
        if corruption == "state":
            db.execute("UPDATE simulation_agents SET state=? WHERE agent_id='agent_0001'",
                       (json.dumps({"emotion": 0, "trust": 0}),))
        elif corruption == "message":
            db.execute("UPDATE simulation_messages SET content='tampered'")
        else:
            db.execute("UPDATE simulation_memories SET content='tampered' WHERE memory_id=("
                       "SELECT memory_id FROM simulation_memories LIMIT 1)")
    with pytest.raises(MessagePairAuditError):
        audit_message_pair_candidate(path, run_id=run_id)


def test_message_pair_audit_rejects_missing_message_id(tmp_path):
    path, run_id = make_run(tmp_path)
    with pytest.raises(MessagePairAuditError, match="no cited runtime message"):
        audit_message_pair_candidate(path, run_id=run_id, message_id="msg_absent")
