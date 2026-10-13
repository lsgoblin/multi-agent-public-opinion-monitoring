"""The simulation-derived direction prediction and its path into the existing evaluator.

Offline and deterministic: no model is called, no network is used, and the evaluator is only
imported and read -- it belongs to the data-evaluation branch and is not modified here.

What is proven here:
  * the frozen method cannot be edited without changing its hash, and its parameters are read
    from the document rather than hard-coded in the code;
  * a produced document satisfies the evaluator's own input schema and its admission gates;
  * one event carries exactly one direction prediction (two would exclude the event);
  * every evidence reference resolves to the value the case claims;
  * a prediction that contradicts the observed direction still lands in the denominator;
  * a run whose selected round decided nothing yields no prediction instead of a default.
"""

import json
import socket
import threading
from decimal import Decimal

import pytest

from riskshield import evaluation
from riskshield.direction_prediction import (DirectionPredictionError, METHOD_PATH,
                                             PREDICTION_SOURCE, build_documents,
                                             classify_round, decide_label, load_method,
                                             sha256_bytes, write_documents)
from riskshield.evidence import EvidencePipeline
from riskshield.simulation import Day3Simulation, DecisionResult
from riskshield.schemas import AgentDecision, CaseImport
from riskshield.store import Store
from tests.fixtures.day3_synthetic_case import CASE_ID, GRAPH_ID, prepare_synthetic_case

PACKAGE = ("data/public/unh_change_20240222_day2_multisource_event.json")


class Substitute:
    """Deterministic offline backend: escalate when the agent has heard something."""

    mode = "test_substitute"
    reservation_cny = Decimal("0")
    input_cny_per_million = Decimal("0")
    cached_input_cny_per_million = Decimal("0")
    output_cny_per_million = Decimal("0")
    price_version = "direction-test-v1"
    model = "direction-test"

    def __init__(self, gate=None):
        self.gate = gate

    def decide(self, observation):
        if self.gate is not None:
            self.gate.wait()
        reference = observation["evidence"][0]["evidence_id"]
        if observation["messages"]:
            # Share what was heard: an unverified narrative, so the method reads escalation.
            return DecisionResult(AgentDecision(
                agent_id=observation["agent_id"], action="share",
                evidence_ids=[observation["messages"][0]["message_id"]],
                reason="Pass on what was heard."),
                usage={"prompt_tokens": 1, "completion_tokens": 1}, model=self.model)
        return DecisionResult(AgentDecision(
            agent_id=observation["agent_id"], action="observe",
            evidence_ids=[reference], reason="Read the notice."),
            usage={"prompt_tokens": 1, "completion_tokens": 1}, model=self.model)


def _fictional_store(tmp_path, name="direction.db"):
    store = Store(tmp_path / name)
    Day3Simulation(store)
    prepare_synthetic_case(store)
    return store


def _run(store, *, agents=4, rounds=2, concurrency=2, gate=None, seed=7):
    engine = Day3Simulation(store)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=agents, rounds=rounds,
                               concurrency=concurrency, seed=seed)
    engine.advance(run_id, Substitute(gate))
    return run_id


# ------------------------------------------------------------------ the frozen method

def test_the_method_document_is_frozen_and_self_describing():
    method, digest = load_method()
    assert method["schema"] == "riskshield.simulation_scale.direction-method.v1"
    assert method["status"] == "frozen_before_any_outcome_access"
    assert digest == sha256_bytes(METHOD_PATH.read_bytes())
    # The rule that produced a prediction must be recoverable from the document alone.
    assert method["parameters"]["escalation_share_threshold"] == 0.5
    assert method["parameters"]["negative_emotion_threshold"] == -0.35
    # The method must state the limits it inherits from the data, not hide them.
    assert "per_round_state_is_not_persisted" in method["known_data_limits"]
    assert method["freeze_declaration"]["never_uses"]


def test_an_edited_method_changes_its_hash_and_is_rejected_when_malformed(tmp_path):
    original = json.loads(METHOD_PATH.read_text(encoding="utf-8"))
    edited = dict(original, parameters=dict(original["parameters"],
                                            escalation_share_threshold=0.9))
    edited_path = tmp_path / "edited.json"
    edited_path.write_text(json.dumps(edited), encoding="utf-8")
    _, original_hash = load_method()
    _, edited_hash = load_method(edited_path)
    assert edited_hash != original_hash

    edited["status"] = "draft"
    edited_path.write_text(json.dumps(edited), encoding="utf-8")
    with pytest.raises(DirectionPredictionError, match="not in its frozen state"):
        load_method(edited_path)


def test_parameters_come_from_the_document_not_from_the_code(tmp_path):
    """A stricter threshold must change the label without touching the code."""
    counts = {"escalating": ["a", "b"], "calming": ["c"], "undecided": []}
    method, _ = load_method()
    label, share = decide_label(counts, method)
    assert share == round(2 / 3, 6) and label == "escalation"

    stricter = dict(method, parameters=dict(method["parameters"],
                                            escalation_share_threshold=0.75))
    assert decide_label(counts, stricter)[0] == "calming"


def test_classification_follows_the_stated_conditions():
    rows = [
        {"agent_id": "a", "status": "valid",
         "decision": json.dumps({"action": "share", "evidence_ids": ["msg_1"]}),
         "observation_refs": json.dumps({"messages": ["msg_1"], "evidence": []})},
        {"agent_id": "b", "status": "valid",
         "decision": json.dumps({"action": "observe", "evidence_ids": ["rec_1"]}),
         "observation_refs": json.dumps({"messages": [], "evidence": ["rec_1"]})},
        {"agent_id": "c", "status": "deadline_exceeded", "decision": None,
         "observation_refs": json.dumps({"messages": [], "evidence": []})},
    ]
    counts = classify_round(rows, negative_emotion_threshold=-0.35,
                            agent_state={"a": 0.0, "b": 0.0})
    assert counts["escalating"] == ["a"]
    assert counts["calming"] == ["b"]
    assert counts["undecided"] == ["c"]

    # The emotion condition alone is enough to classify an agent as escalating.
    distressed = classify_round(rows, negative_emotion_threshold=-0.35,
                                agent_state={"a": -0.9, "b": -0.9})
    assert distressed["escalating"] == ["a", "b"]

    # A row that matches neither condition is not silently assigned to either side.
    unmatched = classify_round(
        [{"agent_id": "d", "status": "valid",
          "decision": json.dumps({"action": "seek_clarification", "evidence_ids": ["m"]}),
          "observation_refs": json.dumps({"messages": ["m"], "evidence": []})}],
        negative_emotion_threshold=-0.35, agent_state={"d": 0.0})
    assert unmatched == {"escalating": [], "calming": [], "undecided": ["d"]}


# --------------------------------------------------------------- produced documents

def test_a_produced_document_meets_the_evaluators_input_schema(tmp_path):
    store = _fictional_store(tmp_path)
    run_id = _run(store)
    built = build_documents(store, run_id, outdir=tmp_path / "build", base=tmp_path)
    inputs = built["inputs"]

    assert inputs["schema"] == evaluation.INPUT_SCHEMA
    assert inputs["dataset_version"]
    assert len(inputs["cases"]) == 1
    case = inputs["cases"][0]
    # Exactly one prediction, and it is a direction prediction from this simulation.
    assert list(case["predictions"]) == ["direction"]
    direction = case["predictions"]["direction"]
    assert direction["label"] in evaluation.DIRECTIONS
    assert direction["prediction_source"] == PREDICTION_SOURCE
    assert direction["execution_mode"] == "multi_agent_simulation"
    assert direction["method_version"] == built["summary"]["method_version"]
    assert direction["run_id"] == run_id
    assert direction["result_evidence_refs"]
    # The case is bound to the run's own cutoff and source version.
    assert case["cutoff"] == built["freeze_record"]["cutoff"]
    assert case["source_version"] == built["freeze_record"]["source_version"]
    assert case["unit_kind"] in {"original_title", "original_post"}
    assert case["sources"] and case["target_record_id"]


def test_every_evidence_reference_resolves_for_the_evaluator(tmp_path, monkeypatch):
    """Resolution is done with the evaluator's own helper, so the check cannot drift."""
    monkeypatch.chdir(tmp_path)
    store = _fictional_store(tmp_path)
    run_id = _run(store)
    outdir = tmp_path / "out"
    written = write_documents(store, run_id, outdir)
    inputs = json.loads(written["inputs"].read_text(encoding="utf-8"))
    # The evaluator resolves references against the process working directory, so run the
    # resolution from the directory the document describes.
    root = outdir.parent
    case = inputs["cases"][0]
    checks = [("cutoff", case["cutoff_evidence_ref"], case["cutoff"]),
              ("title", case["title_sha256_ref"], case["text_sha256"])]
    for source in case["sources"]:
        checks.extend([
            ("source_record", source["source_record_ref"], source["record_id"]),
            ("source_version", source["source_version_ref"], source["source_version"]),
            ("available_at", source["source_available_at_ref"], source["available_at"]),
            ("visibility", source["visibility_evidence_ref"], source["available_at"]),
        ])
    for index, ref in enumerate(case["predictions"]["direction"]["result_evidence_refs"]):
        checks.append((f"direction_evidence[{index}]", ref,
                       case["predictions"]["direction"]["label"]))
    for name, ref, expected in checks:
        assert evaluation._ref_matches(ref, expected, root), (name, ref, expected)


def test_the_freeze_record_states_the_binding_and_the_blinding(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = _fictional_store(tmp_path)
    run_id = _run(store)
    built = build_documents(store, run_id, outdir=tmp_path / "build", base=tmp_path)
    freeze = built["freeze_record"]
    assert freeze["schema"] == "riskshield.simulation_scale.direction-freeze.v1"
    assert freeze["outcomes_accessed"] is False
    assert freeze["labels_accessed"] is False
    assert freeze["cutoff_evidence_only"] is True
    assert freeze["direction_predictions_for_this_event"] == 1
    assert freeze["prediction_source"] == PREDICTION_SOURCE
    assert freeze["method_document_sha256"] == load_method()[1]
    assert freeze["run_id"] == run_id
    # The recorded hash covers the document that was actually written.
    written = write_documents(store, run_id, tmp_path / "out2", base=tmp_path)
    raw = written["inputs"].read_bytes()
    written_freeze = json.loads(
        (tmp_path / "out2" / "simulation-prediction-freeze-v1.json").read_text("utf-8"))
    assert written_freeze["inputs_sha256"] == sha256_bytes(raw)
    # The freeze record's own reference points at the written inputs document.
    assert written_freeze["inputs_ref"].endswith("simulation-prediction-inputs-v1.json")


def test_a_frozen_document_is_never_overwritten(tmp_path):
    store = _fictional_store(tmp_path)
    run_id = _run(store)
    outdir = tmp_path / "out3"
    write_documents(store, run_id, outdir)
    with pytest.raises(DirectionPredictionError, match="refusing to overwrite"):
        write_documents(store, run_id, outdir)


def test_the_adapter_makes_no_network_call(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the adapter must not open a socket")

    monkeypatch.setattr(socket, "socket", forbidden)
    store = _fictional_store(tmp_path)
    run_id = _run(store)
    built = build_documents(store, run_id, outdir=tmp_path / "build", base=tmp_path)
    assert built["summary"]["prediction_source"] == PREDICTION_SOURCE


def test_a_run_whose_selected_round_decided_nothing_yields_no_prediction(tmp_path):
    """No decision means no label: the method must not fall back to a default."""
    store = _fictional_store(tmp_path)
    gate = threading.Event()
    engine = Day3Simulation(store)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=4, rounds=2, concurrency=4,
                               seed=3)
    try:
        engine.advance(run_id, Substitute(gate), collect_performance=True,
                       call_timeout_seconds=0.2)
    finally:
        gate.set()
    with pytest.raises(DirectionPredictionError, match="no classifiable valid decision"):
        build_documents(store, run_id, outdir=tmp_path / "build", base=tmp_path)


# --------------------------------------------------------- the evaluator, end to end

def _archived_real_case(tmp_path):
    """A real-historical case with an archived-visibility source record, built offline."""
    package = json.loads((tmp_path / PACKAGE).read_text(encoding="utf-8")
                         if (tmp_path / PACKAGE).is_file() else
                         _read_project_file(PACKAGE))
    store = Store(tmp_path / "historical.db")
    EvidencePipeline(store)
    case = CaseImport.model_validate(package["case_import_projection"])
    store.import_case(case)
    projection = {"case_id": case.case_id, "graph_id": "offline-archived-projection"}
    return store, case, projection


def _read_project_file(relative):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    return (root / relative).read_text(encoding="utf-8")


def _label_evidence(path, inputs, observed):
    """The values a label document's references point at.

    Labels live in their own file by contract, so their evidence does too: pointing an
    observed outcome at the prediction's evidence would make the evaluator compare a value
    against itself and prove nothing.
    """
    document = {
        "observed_direction": observed,
        "sentiment_label_by_evaluation_id": {case["evaluation_id"]: "neutral"
                                             for case in inputs["cases"]},
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return document


def _synthetic_labels(inputs, *, observed, label_evidence_relative):
    """Two independent, prediction-blind annotations so the label gate can be exercised."""
    return {
        "schema": evaluation.LABEL_SCHEMA,
        "label_version": "synthetic-offline-path-check-v1",
        "sentiment_labels": [{
            "evaluation_id": case["evaluation_id"], "event_id": case["event_id"],
            "unit_id": case["unit_id"], "source_record_id": case["target_record_id"],
            "text_sha256": case["text_sha256"],
            "sentiment_annotations": [
                {"annotator_id": f"a{index}", "independent": True, "prediction_seen": False,
                 "label": "neutral",
                 "evidence_refs": [{"path": label_evidence_relative,
                                    "pointer": "/sentiment_label_by_evaluation_id/"
                                               + case["evaluation_id"]}]}
                for index in (1, 2)],
        } for case in inputs["cases"]],
        "events": [{"event_id": event_id, "observed_direction": observed,
                    "evidence_refs": [{"path": label_evidence_relative,
                                       "pointer": "/observed_direction"}]}
                   for event_id in sorted({case["event_id"] for case in inputs["cases"]})],
    }


def test_the_existing_evaluator_admits_the_document_and_keeps_a_wrong_prediction(tmp_path,
                                                                               monkeypatch):
    """The whole point: the simulation's prediction survives the real admission gates.

    The synthetic observed direction is deliberately the opposite of the prediction, so a
    correct-by-construction score is impossible; the assertion is that the event is still
    counted rather than excluded.
    """
    monkeypatch.chdir(tmp_path)
    from pathlib import Path
    project = Path(__file__).resolve().parents[1]
    store = Store(tmp_path / "real.db")
    EvidencePipeline(store)
    package = json.loads((project / PACKAGE).read_text(encoding="utf-8"))
    case = CaseImport.model_validate(package["case_import_projection"])
    store.import_case(case)
    engine = Day3Simulation(store)
    graph = engine.day2.engine_build_graph if hasattr(engine.day2, "engine_build_graph") else None
    # Build the archived projection through the task layer, which is the supported path.
    from riskshield.tasks import Day4Tasks
    tasks = Day4Tasks(store)
    graph = tasks._build_archived_projection(case.case_id)
    run_id = engine.create_run(case.case_id, graph["graph_id"], agent_count=4, rounds=2,
                               concurrency=2, seed=11)
    engine.advance(run_id, Substitute())

    outdir = tmp_path / "direction"
    written = write_documents(store, run_id, outdir, unit_kind="original_post",
                             text_source="summary", base=tmp_path)
    inputs = json.loads(written["inputs"].read_text(encoding="utf-8"))
    assert inputs["cases"][0]["data_mode"] == "real_historical"
    predicted = inputs["cases"][0]["predictions"]["direction"]["label"]
    observed = "calming" if predicted == "escalation" else "escalation"
    label_evidence = outdir / "synthetic-label-evidence.json"
    _label_evidence(label_evidence, inputs, observed)
    label_evidence_relative = str(
        label_evidence.relative_to(tmp_path)).replace("\\", "/")
    labels_path = outdir / "synthetic-labels.json"
    labels_path.write_text(json.dumps(
        _synthetic_labels(inputs, observed=observed,
                          label_evidence_relative=label_evidence_relative),
        ensure_ascii=False), encoding="utf-8")

    report = evaluation.evaluate_files(written["inputs"], labels_path)
    direction = report["direction_agreement"]
    assert direction["denominator"] == 1, (
        f"the event was excluded instead of scored: {direction['excluded_events_or_rows']}")
    assert direction["numerator"] == 0
    scored = direction["scored_events"][0]
    assert scored["prediction"] == predicted and scored["truth"] == observed
    assert scored["correct"] is False
    # One event, one direction prediction: two would have excluded it instead.
    assert report["provenance"]["multiple_sources_same_event_counted_as_distinct_direction_events"] is False


def test_two_direction_predictions_for_one_event_are_excluded_not_counted(tmp_path,
                                                                        monkeypatch):
    """Why the adapter emits exactly one: a second one silently removes the event."""
    monkeypatch.chdir(tmp_path)
    from pathlib import Path
    project = Path(__file__).resolve().parents[1]
    store = Store(tmp_path / "two.db")
    EvidencePipeline(store)
    package = json.loads((project / PACKAGE).read_text(encoding="utf-8"))
    case = CaseImport.model_validate(package["case_import_projection"])
    store.import_case(case)
    from riskshield.tasks import Day4Tasks
    tasks = Day4Tasks(store)
    graph = tasks._build_archived_projection(case.case_id)
    engine = Day3Simulation(store)
    run_id = engine.create_run(case.case_id, graph["graph_id"], agent_count=4, rounds=2,
                               concurrency=2, seed=11)
    engine.advance(run_id, Substitute())
    outdir = tmp_path / "two-out"
    written = write_documents(store, run_id, outdir, unit_kind="original_post",
                             text_source="summary", base=tmp_path)
    inputs = json.loads(written["inputs"].read_text(encoding="utf-8"))
    # A second case for the same event, as an independent baseline would produce. It must be
    # a *different unit*: the evaluator rejects the same source record appearing twice for
    # one event before the direction aggregation is even reached. The unit is the case's
    # second source record, which the run's own cutoff-eligible snapshot provides.
    duplicate = json.loads(json.dumps(inputs["cases"][0]))
    other = duplicate["sources"][1]
    duplicate["evaluation_id"] = duplicate["evaluation_id"] + "__baseline"
    duplicate["unit_id"] = other["record_id"]
    duplicate["target_record_id"] = other["record_id"]
    duplicate["sources"] = [other]
    duplicate["predictions"]["direction"]["prediction_source"] = "baseline_model"
    inputs["cases"].append(duplicate)
    label_evidence = outdir / "synthetic-label-evidence-2.json"
    _label_evidence(label_evidence, inputs, "escalation")
    label_evidence_relative = str(
        label_evidence.relative_to(tmp_path)).replace("\\", "/")
    labels_path = outdir / "synthetic-labels-2.json"
    labels_path.write_text(json.dumps(
        _synthetic_labels(inputs, observed="escalation",
                          label_evidence_relative=label_evidence_relative),
        ensure_ascii=False), encoding="utf-8")
    inputs_path = outdir / "two-inputs.json"
    inputs_path.write_text(json.dumps(inputs, ensure_ascii=False), encoding="utf-8")

    report = evaluation.evaluate_files(inputs_path, labels_path)
    direction = report["direction_agreement"]
    assert direction["denominator"] == 0
    reasons = [reason for row in direction["excluded_events_or_rows"]
               for reason in row["reasons"]]
    assert "multiple_direction_predictions_for_one_event" in reasons
