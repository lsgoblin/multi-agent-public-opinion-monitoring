"""The three hard constraints on the simulation-derived direction prediction.

Each constraint is covered by a positive and a negative case, offline and deterministic:

1. event-level uniqueness -- before formal scoring, one event must carry exactly one
   pre-selected direction prediction, and the simulation prediction must not be pooled with
   a baseline;
2. emotional state -- a formal direction prediction is only admitted for a complete run
   whose last completed round is the round the final state belongs to; a partial run is
   tentative and refused, and no per-round emotion is fabricated from memory rows;
3. evidence paths -- the adapter receives a material root, resolves every reference against
   it, and refuses a path that escapes it or does not resolve.

The data-evaluation branch's reader is exercised read-only by file path where it is
available; it is never modified, and its execution-mode policy is not worked around.
"""

import importlib.util
import json
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from riskshield import evaluation
from riskshield.direction_prediction import (FORMAL_QUALIFICATION,
                                             PREDICTION_IMPORT_SCHEMA,
                                             TENTATIVE_QUALIFICATION,
                                             DirectionPredictionError,
                                             build_documents,
                                             build_prediction_import_document,
                                             canonical_digest,
                                             finalize_import_freeze_record,
                                             material_relative,
                                             preflight_event_level_uniqueness,
                                             preflight_import_references,
                                             resolve_material_root, write_documents)
from riskshield.evidence import EvidencePipeline
from riskshield.schemas import AgentDecision, CaseImport
from riskshield.simulation import Day3Simulation, DecisionResult
from riskshield.store import Store
from tests.fixtures.day3_synthetic_case import CASE_ID, GRAPH_ID, prepare_synthetic_case

PACKAGE = "data/public/unh_change_20240222_day2_multisource_event.json"
DATA_BRANCH = Path(r"C:\Users\李\.codex\worktrees\riskshield-data-evaluation"
                   r"\多智能体仿真舆情监测与投诉预警")


class Substitute:
    mode = "test_substitute"
    reservation_cny = Decimal("0")
    input_cny_per_million = Decimal("0")
    cached_input_cny_per_million = Decimal("0")
    output_cny_per_million = Decimal("0")
    price_version = "constraint-test-v1"
    model = "constraint-test"

    def __init__(self, gate=None, *, block_from_call=None):
        self.gate = gate
        self.block_from_call = block_from_call
        self.calls = 0

    def decide(self, observation):
        self.calls += 1
        if self.gate is not None and self.block_from_call is not None \
                and self.calls >= self.block_from_call:
            self.gate.wait()
        reference = observation["evidence"][0]["evidence_id"]
        if observation["messages"]:
            return DecisionResult(AgentDecision(
                agent_id=observation["agent_id"], action="share",
                evidence_ids=[observation["messages"][0]["message_id"]],
                reason="Pass on what was heard."),
                usage={"prompt_tokens": 1, "completion_tokens": 1}, model=self.model)
        return DecisionResult(AgentDecision(
            agent_id=observation["agent_id"], action="observe",
            evidence_ids=[reference], reason="Read the notice."),
            usage={"prompt_tokens": 1, "completion_tokens": 1}, model=self.model)


def _fictional(tmp_path, name="c.db"):
    store = Store(tmp_path / name)
    Day3Simulation(store)
    prepare_synthetic_case(store)
    return store


def _real_case(tmp_path, name="real.db"):
    root = Path(__file__).resolve().parents[1]
    package = json.loads((root / PACKAGE).read_text(encoding="utf-8"))
    store = Store(tmp_path / name)
    EvidencePipeline(store)
    case = CaseImport.model_validate(package["case_import_projection"])
    store.import_case(case)
    from riskshield.tasks import Day4Tasks
    graph = Day4Tasks(store)._build_archived_projection(case.case_id)
    return store, case, graph


def _complete_run(store, case_id, graph_id, *, agents=4, rounds=2, concurrency=2, seed=11):
    engine = Day3Simulation(store)
    run_id = engine.create_run(case_id, graph_id, agent_count=agents, rounds=rounds,
                               concurrency=concurrency, seed=seed)
    engine.advance(run_id, Substitute())
    return run_id


# ------------------------------------------------------- constraint 1: event uniqueness

def _entry(event_id, unit_id, label, run_id, source, *, qualification=FORMAL_QUALIFICATION):
    return {"event_id": event_id, "unit_id": unit_id, "label": label, "run_id": run_id,
            "source_label": source, "qualification": qualification,
            "prediction_source": source}


def test_preflight_accepts_exactly_one_prediction_per_event_per_source():
    report = preflight_event_level_uniqueness(
        [_entry("evt-a", "unit-1", "calming", "run-1", "simulation")],
        declared_events={"simulation": {"evt-a"}})
    assert report["ok"] is True
    assert report["formal"] is True
    assert report["collisions"] == [] and report["refusals"] == []


def test_preflight_refuses_two_predictions_for_one_event_and_names_both_sources():
    """Formally one event carries one prediction, so two must fail and name who disagrees."""
    report = preflight_event_level_uniqueness(
        [_entry("evt-a", "unit-1", "calming", "run-sim", "simulation"),
         _entry("evt-a", "unit-2", "escalation", "run-baseline", "baseline_model")],
        formal=True)
    assert report["ok"] is False
    assert len(report["collisions"]) == 1
    collision = report["collisions"][0]
    assert collision["event_id"] == "evt-a" and collision["count"] == 2
    assert collision["reason"] == "more_than_one_direction_prediction_for_one_event"
    listed = {(e["unit_id"], e["run_id"], e["label"], e["source_label"])
              for e in collision["entries"]}
    assert listed == {("unit-1", "run-sim", "calming", "simulation"),
                      ("unit-2", "run-baseline", "escalation", "baseline_model")}


def test_two_sources_for_one_event_are_legal_per_source_but_not_formally():
    """Each source is a separate report; only a formal run forbids the pair."""
    simulation = [_entry("evt-a", "unit-1", "calming", "run-sim", "simulation")]
    baseline = [_entry("evt-a", "unit-2", "escalation", "run-base", "baseline_model")]
    assert preflight_event_level_uniqueness(
        simulation, declared_events={"simulation": {"evt-a"}})["ok"] is True
    assert preflight_event_level_uniqueness(
        baseline, declared_events={"baseline_model": {"evt-a"}})["ok"] is True
    formal = preflight_event_level_uniqueness(simulation + baseline, formal=True)
    assert formal["ok"] is False and formal["collisions"][0]["count"] == 2


def test_a_second_prediction_inside_one_source_is_a_collision():
    report = preflight_event_level_uniqueness(
        [_entry("evt-a", "unit-1", "calming", "run-1", "simulation"),
         _entry("evt-a", "unit-2", "calming", "run-2", "simulation")],
        declared_events={"simulation": {"evt-a"}})
    assert report["ok"] is False
    assert report["collisions"][0]["count"] == 2


def test_preflight_refuses_a_declared_event_with_no_prediction():
    report = preflight_event_level_uniqueness(
        [], declared_events={"simulation": {"evt-a", "evt-b"}})
    assert report["ok"] is False
    assert {row["reason"] for row in report["refusals"]} == {"no_direction_prediction"}
    assert {row["event_id"] for row in report["refusals"]} == {"evt-a", "evt-b"}


def test_preflight_refuses_a_prediction_for_an_undeclared_event():
    report = preflight_event_level_uniqueness(
        [_entry("evt-z", "unit-9", "calming", "run-9", "simulation")],
        declared_events={"simulation": {"evt-a"}})
    assert report["ok"] is False
    reasons = {row["reason"] for row in report["refusals"]}
    assert "event_not_declared_for_source" in reasons


def test_two_units_same_event_preflight_and_evaluator_agree(tmp_path, monkeypatch):
    """The same collision is refused by the preflight and by the evaluator's own rule.

    The two cases are two *different units* of one event, because the evaluator rejects the
    same source record appearing twice for one event before its direction rule is reached.
    """
    monkeypatch.chdir(tmp_path)
    from riskshield.tasks import Day4Tasks
    store, case, graph = _real_case(tmp_path)
    run_id = _complete_run(store, case.case_id, graph["graph_id"])
    outdir = tmp_path / "pred"
    written = write_documents(store, run_id, outdir, material_root=tmp_path,
                             unit_kind="original_post")
    inputs = json.loads(written["inputs"].read_text(encoding="utf-8"))
    first = inputs["cases"][0]
    second = json.loads(json.dumps(first))
    other = second["sources"][1]
    second["evaluation_id"] = first["evaluation_id"] + "__baseline"
    second["unit_id"] = other["record_id"]
    second["target_record_id"] = other["record_id"]
    second["sources"] = [other]

    # The preflight sees the same shape and refuses it, naming both units.
    report = preflight_event_level_uniqueness(
        [_entry(first["event_id"], first["unit_id"], "calming", run_id, "simulation"),
         _entry(second["event_id"], second["unit_id"], "escalation", "run-baseline",
                "baseline_model")],
        formal=True)
    assert report["ok"] is False
    assert report["collisions"][0]["count"] == 2

    # The evaluator refuses it too, for its own documented reason.
    inputs["cases"] = [first, second]
    label_evidence = outdir / "label-evidence.json"
    label_evidence.write_text(json.dumps({
        "observed_direction": "escalation",
        "sentiment_label_by_evaluation_id": {c["evaluation_id"]: "neutral"
                                             for c in inputs["cases"]},
    }), encoding="utf-8")
    relative = str(label_evidence.relative_to(tmp_path)).replace("\\", "/")
    labels = {
        "schema": evaluation.LABEL_SCHEMA, "label_version": "synthetic-collision-check-v1",
        "sentiment_labels": [{
            "evaluation_id": c["evaluation_id"], "event_id": c["event_id"],
            "unit_id": c["unit_id"], "source_record_id": c["target_record_id"],
            "text_sha256": c["text_sha256"],
            "sentiment_annotations": [
                {"annotator_id": f"a{i}", "independent": True, "prediction_seen": False,
                 "label": "neutral",
                 "evidence_refs": [{"path": relative,
                                    "pointer": "/sentiment_label_by_evaluation_id/"
                                               + c["evaluation_id"]}]}
                for i in (1, 2)],
        } for c in inputs["cases"]],
        "events": [{"event_id": first["event_id"], "observed_direction": "escalation",
                    "evidence_refs": [{"path": relative,
                                       "pointer": "/observed_direction"}]}],
    }
    labels_path = outdir / "labels.json"
    labels_path.write_text(json.dumps(labels), encoding="utf-8")
    inputs_path = outdir / "two-units.json"
    inputs_path.write_text(json.dumps(inputs), encoding="utf-8")
    result = evaluation.evaluate_files(inputs_path, labels_path)
    direction = result["direction_agreement"]
    assert direction["denominator"] == 0
    reasons = [r for row in direction["excluded_events_or_rows"] for r in row["reasons"]]
    assert "multiple_direction_predictions_for_one_event" in reasons


# ------------------------------------------------------- constraint 2: emotional state

def test_a_complete_run_yields_a_formal_candidate_bound_to_its_last_round(tmp_path):
    store = _fictional(tmp_path)
    run_id = _complete_run(store, CASE_ID, GRAPH_ID)
    built = build_documents(store, run_id, outdir=tmp_path / "b", material_root=tmp_path)
    completeness = built["unit"] and built["inputs"]["cases"][0]["run_completeness"]
    assert completeness["run_status"] == "complete"
    assert completeness["selected_round"] == completeness["configured_rounds"]
    assert completeness["state_basis"] == "run_final_state"
    # The selected round is the run's last completed round, so the final state the rule
    # reads really is the state at the end of the predicted round.
    assert completeness["state_is_at_selected_round"] is True
    assert completeness["qualification"] == FORMAL_QUALIFICATION
    assert built["freeze_record"]["qualification"] == FORMAL_QUALIFICATION


class BlocksOnceAgentsHaveMemory(Substitute):
    """Answers round 1, then blocks for agents that already hold memory (round 2 on)."""

    def decide(self, observation):
        if observation.get("memories"):
            self.gate.wait()
        return super().decide(observation)


def test_a_partial_run_is_tentative_and_refused_by_the_formal_preflight(tmp_path):
    """A partial run still yields a label, but only as a tentative one.

    Round 1 decides normally, so the run's last completed round holds classifiable
    decisions; round 2 then times out and leaves the run partial. A partial run cannot be a
    formal direction prediction, because the prediction would rest on a state the run never
    reached the end of.
    """
    store = _fictional(tmp_path)
    gate = threading.Event()
    engine = Day3Simulation(store)
    # concurrency 1 keeps the two rounds distinguishable: agent_0000 answers in round 1, then
    # the next dispatch blocks, so the run ends partial while its last completed round still
    # holds one classifiable decision.
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=2, rounds=2, concurrency=1,
                               seed=3)
    try:
        summary = engine.advance(run_id, Substitute(gate, block_from_call=2),
                                 collect_performance=True, call_timeout_seconds=0.2)
    finally:
        gate.set()
    assert summary["status"] == "partial"
    assert summary["action_counts"].get("valid", 0) >= 1
    assert summary["completed_rounds"] >= 1

    built = build_documents(store, run_id, outdir=tmp_path / "p", material_root=tmp_path)
    completeness = built["inputs"]["cases"][0]["run_completeness"]
    assert completeness["qualification"] == TENTATIVE_QUALIFICATION
    assert completeness["state_is_at_selected_round"] is False
    assert completeness["run_status"] == "partial"

    entry = _entry(built["unit"]["event_id"], built["unit"]["unit_id"],
                   built["summary"]["label"], run_id, "simulation",
                   qualification=TENTATIVE_QUALIFICATION)
    refused = preflight_event_level_uniqueness(
        [entry], declared_events={"simulation": {entry["event_id"]}})
    assert refused["ok"] is False
    assert {row["reason"] for row in refused["refusals"]} == {
        "tentative_partial_run_not_admitted_for_a_formal_prediction"}
    # Tentative analysis is still possible when it is asked for explicitly.
    allowed = preflight_event_level_uniqueness(
        [entry], declared_events={"simulation": {entry["event_id"]}}, allow_tentative=True)
    assert allowed["ok"] is True


def test_no_per_round_emotion_is_fabricated_from_memory_rows(tmp_path):
    """The engine stores no per-round emotion, so the adapter must not claim to read one.

    The method document states this limit; here it is checked against the code: the
    classification takes the run's persisted final state, and the recorded state basis says
    so instead of asserting a per-round snapshot.
    """
    store = _fictional(tmp_path)
    run_id = _complete_run(store, CASE_ID, GRAPH_ID)
    with store.connect() as db:
        memory_rows = db.execute(
            "SELECT content FROM simulation_memories WHERE run_id=?", (run_id,)).fetchall()
    # Memory content carries the action and its reason, not an emotion value.
    assert memory_rows
    for row in memory_rows:
        assert "emotion" not in row["content"].lower()
    built = build_documents(store, run_id, outdir=tmp_path / "f", material_root=tmp_path)
    assert built["inputs"]["cases"][0]["run_completeness"]["state_basis"] == "run_final_state"


# ---------------------------------------------------------- constraint 3: evidence paths

def test_the_material_root_must_be_declared_and_absolute(tmp_path):
    with pytest.raises(DirectionPredictionError, match="absolute path"):
        resolve_material_root("relative/dir")
    with pytest.raises(DirectionPredictionError, match="not a directory"):
        resolve_material_root(tmp_path / "missing")
    assert resolve_material_root(tmp_path) == tmp_path.resolve()


def test_a_reference_that_escapes_the_material_root_is_refused(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(DirectionPredictionError, match="outside the material root"):
        material_relative(outside, root, what="the evidence document")


def test_output_outside_the_material_root_is_refused_not_silently_recorded(tmp_path):
    store = _fictional(tmp_path)
    run_id = _complete_run(store, CASE_ID, GRAPH_ID)
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(DirectionPredictionError, match="outside the material root"):
        write_documents(store, run_id, tmp_path / "elsewhere", material_root=root)


def test_references_resolve_from_a_different_working_directory(tmp_path, monkeypatch):
    """The document must not depend on the directory the adapter happened to run in."""
    store = _fictional(tmp_path)
    run_id = _complete_run(store, CASE_ID, GRAPH_ID)
    root = tmp_path / "material"
    outdir = root / "predictions"
    outdir.mkdir(parents=True)
    written = write_documents(store, run_id, outdir, material_root=root)
    inputs = json.loads(written["inputs"].read_text(encoding="utf-8"))
    case = inputs["cases"][0]

    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    for name, ref, expected in (("cutoff", case["cutoff_evidence_ref"], case["cutoff"]),
                                ("title", case["title_sha256_ref"], case["text_sha256"])):
        assert evaluation._ref_matches(ref, expected, root), name
    for source in case["sources"]:
        assert evaluation._ref_matches(source["source_record_ref"], source["record_id"],
                                       root)


def test_a_material_root_with_spaces_and_non_ascii_is_supported(tmp_path, monkeypatch):
    root = tmp_path / "材料 根 目录"
    outdir = root / "predictions"
    outdir.mkdir(parents=True)
    store = _fictional(tmp_path)
    run_id = _complete_run(store, CASE_ID, GRAPH_ID)
    written = write_documents(store, run_id, outdir, material_root=root)
    inputs = json.loads(written["inputs"].read_text(encoding="utf-8"))
    case = inputs["cases"][0]

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert evaluation._ref_matches(case["cutoff_evidence_ref"], case["cutoff"], root)
    for source in case["sources"]:
        assert evaluation._ref_matches(source["visibility_evidence_ref"],
                                       source["available_at"], root)
    # The recorded paths are relative, so they carry no absolute machine path.
    assert not case["cutoff_evidence_ref"]["path"].startswith(("/", "C:", "D:"))


def test_the_import_preflight_reports_an_unresolvable_reference(tmp_path):
    store = _fictional(tmp_path)
    run_id = _complete_run(store, CASE_ID, GRAPH_ID)
    root = tmp_path / "material"
    outdir = root / "predictions"
    outdir.mkdir(parents=True)
    written = write_documents(store, run_id, outdir, material_root=root)
    document = written["import_document"]
    assert preflight_import_references(document, root)["ok"] is True

    broken = json.loads(json.dumps(document))
    entry = next(iter(broken["predictions"].values()))
    entry["production_evidence_ref"] = {"path": "../../escape.json", "pointer": ""}
    report = preflight_import_references(broken, root)
    assert report["ok"] is False
    reasons = {row["reason"] for row in report["problems"]}
    assert "reference_escapes_material_root" in reasons

    missing = json.loads(json.dumps(document))
    entry = next(iter(missing["predictions"].values()))
    entry["freeze_record_ref"] = {"path": "predictions/nope.json", "pointer": ""}
    assert "reference_file_missing" in {
        row["reason"] for row in preflight_import_references(missing, root)["problems"]}


# ------------------------------------------- the import shape and its unresolved policy

def test_the_import_document_binds_its_package_and_its_own_digest(tmp_path):
    store, case, graph = _real_case(tmp_path)
    run_id = _complete_run(store, case.case_id, graph["graph_id"])
    root = tmp_path / "material"
    outdir = root / "predictions"
    outdir.mkdir(parents=True)
    written = write_documents(store, run_id, outdir, material_root=root)
    document = written["import_document"]

    assert document["schema"] == PREDICTION_IMPORT_SCHEMA
    assert document["task_type"] == "event_direction"
    assert document["material_class"] == "real_frozen_material"
    entry = next(iter(document["predictions"].values()))
    # The claimed package digest is the digest of the package that was actually written.
    package = json.loads(written["input_package"].read_text(encoding="utf-8"))
    assert entry["input_binding"]["input_package_sha256"] == canonical_digest(package)
    assert entry["input_binding"]["cutoff_before_label_read"] is True
    assert entry["labels_accessed"] is False and entry["outcomes_accessed"] is False
    assert entry["execution_mode"] == "multi_agent_simulation"

    # The freeze record binds the document's canonical digest, indentation-independent.
    freeze = json.loads(written["import_freeze"].read_text(encoding="utf-8"))
    assert freeze["prediction_document_sha256"] == canonical_digest(document)
    assert freeze["labels_read_before_freeze"] is False
    assert freeze["outcomes_read_before_freeze"] is False
    assert freeze["frozen_inputs_sha256"] == canonical_digest(package)
    assert preflight_import_references(document, root)["ok"] is True


def _data_branch_import_module():
    path = DATA_BRANCH / "src" / "riskshield" / "evaluation_prediction_import.py"
    if not path.is_file():
        pytest.skip(f"data-evaluation branch not available at {DATA_BRANCH}")
    spec = importlib.util.spec_from_file_location("de_prediction_import", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_data_branch_reader_still_refuses_the_multi_agent_mode(tmp_path):
    """Read-only: this records the blocker, it does not work around it.

    The document is structurally complete, so the only refusal left is the execution-mode
    policy. Reporting that honestly is the point; renaming the mode to something on the
    allow-list would be a false claim about how the prediction was produced.
    """
    module = _data_branch_import_module()
    store, case, graph = _real_case(tmp_path)
    run_id = _complete_run(store, case.case_id, graph["graph_id"])
    root = tmp_path / "material"
    outdir = root / "predictions"
    outdir.mkdir(parents=True)
    written = write_documents(store, run_id, outdir, material_root=root)
    document = written["import_document"]
    entry = next(iter(document["predictions"].values()))

    reasons: list[str] = []
    module._check_execution_mode(entry, reasons)
    assert reasons == ["unknown_execution_mode_not_admitted"]

    units = [{"unit_id": written["summary"]["unit_id"],
              "event_id": written["summary"]["event_id"],
              "cutoff": written["summary"]["cutoff"],
              "source_version": written["summary"]["source_version"],
              "text_sha256": written["summary"]["text_sha256"]}]
    report = module.import_formal_predictions(
        document, task_type="event_direction", units=units, material_root=root)
    assert report["admitted"] == []
    rejected_reasons = {r for row in report["rejected"] for r in row["reasons"]}
    assert "unknown_execution_mode_not_admitted" in rejected_reasons
    # Every other structural requirement is met, so the mode policy is the only open item.
    structural = {r for r in rejected_reasons if r != "unknown_execution_mode_not_admitted"}
    assert structural == set(), sorted(structural)
