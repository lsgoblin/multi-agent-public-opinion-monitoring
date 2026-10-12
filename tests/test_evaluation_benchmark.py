"""Tests for the single configurable benchmark entry point.

Required coverage:

* a config is accepted only when it is complete enough to be reproducible;
* recorded human annotations are attached only when they are independent,
  distinct and inside the formal three-class scope, and a cohort with no
  configured sources keeps the annotations its loader already derived;
* the owner authorisation record binds named units and leaves the permission
  gate open for units it does not name;
* the eligibility table and the scoring denominator are the same set, and the
  ways they can drift -- a leaked excluded unit, a sample-size mismatch, and an
  engine that drops a gated unit -- each raise instead of quietly shrinking the
  denominator;
* a frozen expectation is reported rather than satisfied by force;
* cohorts that share an identity are refused instead of merged.

The synthetic fixtures reuse the qualifying-prediction helpers of
``test_evaluation_alignment``, so these tests exercise the real admission path
rather than a stub.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from riskshield import evaluation_benchmark as bench
from riskshield import evaluation_prediction_import as imp
from riskshield.evaluation_alignment import unit_gates
from tests.test_evaluation_alignment import (
    SOURCE_VERSION,
    _prediction_document,
    _write,
    _write_prediction_support,
)

TITLE = "Insurer faces class action over denied claims"
TEXT_SHA = hashlib.sha256(TITLE.encode("utf-8")).hexdigest()
CUTOFF = "2023-11-23T14:29:55Z"
VISIBLE_AT = "2023-11-23T13:59:55Z"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _register_fixture_unit_source(monkeypatch):
    """Make ``fixture_units`` a name the config validator accepts.

    Tests that need real units replace this with ``_install_units``; the config
    validation tests only need the name to exist.
    """
    monkeypatch.setitem(bench.UNIT_SOURCES, "fixture_units", lambda root: [])


def _fixture_root(tmp_path: Path, unit_ids: list[str]) -> Path:
    """A minimal but real frozen material root the engine can resolve."""
    packet_units = []
    records = []
    for index, unit_id in enumerate(unit_ids):
        packet_units.append({
            "unit_id": unit_id,
            "source_record_id": f"reddit_archived_post_fixture{index}",
            "source_version": SOURCE_VERSION,
            "text_sha256": TEXT_SHA,
            "source": {"source_version": SOURCE_VERSION, "captured_at": VISIBLE_AT},
            "cutoff_utc": CUTOFF,
        })
        records.append({
            "source_ref": f"reddit_archived_post_fixture{index}",
            "post_id": f"fixture{index}",
            "title_sha256": TEXT_SHA,
            "retrieved_on_utc": VISIBLE_AT,
        })
    _write(tmp_path, "frozen/packet.json", {"units": packet_units})
    _write(tmp_path, "frozen/archive.json", {"records": records})
    _write(tmp_path, "labels/v16.json",
           {"annotator_id": "leo", "prediction_exposure": False,
            "saw_other_annotator_answer": False,
            "units": [{"unit_id": unit_id, "label": "neutral"} for unit_id in unit_ids]})
    _write(tmp_path, "labels/v18.json",
           {"annotator_id": "dick", "prediction_exposure": False,
            "saw_other_annotator_answer": False,
            "records": [{"unit_id": unit_id, "label": "neutral"} for unit_id in unit_ids]})
    return tmp_path


def _fixture_unit(root: Path, index: int, unit_id: str, **overrides) -> dict:
    unit = {
        "_root": str(root),
        "unit_id": unit_id,
        "event_id": f"event_{index}",
        "unit_kind": "original_title",
        "unit_scope": "exact_frozen_title",
        "source_document": "frozen/packet.json",
        "source_row_key": "units",
        "source_row_index": index,
        "source_record_id": f"reddit_archived_post_fixture{index}",
        "publisher": "Reddit r/insurance archived post",
        "source_version": SOURCE_VERSION,
        "source_version_pointer": f"/units/{index}/source/source_version",
        "source_url": "https://example.invalid/",
        "archive_pointer": f"/records/{index}",
        "attribution_class": "archived_public_post_with_archive_record",
        "permission_basis": "fixture",
        "permission_evidence_ref": None,
        "frozen_text": TITLE,
        "text_sha256": TEXT_SHA,
        "archive_title_sha256": TEXT_SHA,
        "title_hash_match": True,
        "unit_hash_ref": {"path": "frozen/archive.json",
                          "pointer": f"/records/{index}/title_sha256"},
        "visible_at": VISIBLE_AT,
        "visible_at_pointer": {"path": "frozen/packet.json",
                               "pointer": f"/units/{index}/source/captured_at"},
        "cutoff": CUTOFF,
        "cutoff_provenance": "declared_in_packet",
        "cutoff_margin_seconds": 1800,
        "source_record_pointer": {"path": "frozen/archive.json",
                                  "pointer": f"/records/{index}/source_ref"},
        "annotations": [],
        "labels_agree": False,
        "human_exclusions": ["fewer_than_two_independent_human_labels"],
        "prediction": None,
        "prediction_source": None,
        "split": "holdout",
    }
    unit.update(overrides)
    return unit


def _config(unit_ids: list[str], *, unit_source: str = "fixture_units",
            annotation_sources: bool = True, expected: list[str] | None = None,
            out_dir: str = "out/run") -> dict:
    return {
        "schema": bench.CONFIG_SCHEMA,
        "config_version": "fixture-v1",
        "material_root": ".",
        "outputs": {"dir": out_dir},
        "cutoffs_document": None,
        "combined_dataset_version": "fixture-combined-v1",
        "combined_label_version": "fixture-combined-labels-v1",
        "rules": {"denominator_not_filtered_by_prediction_correctness": True},
        "cohorts": [{
            "cohort_id": "fixture-cohort",
            "unit_source": unit_source,
            "dataset_version": "fixture-v1",
            "label_version": "fixture-labels-v1",
            "annotation_sources": ([{"path": "labels/v16.json"}, {"path": "labels/v18.json"}]
                                   if annotation_sources else []),
            "prediction_document": "pred/predictions.json",
            "permission_record": "auth/permission.json",
            "expected_gated_unit_ids": expected if expected is not None else unit_ids,
        }],
    }


def _permission_record(root: Path, unit_ids: list[str]) -> None:
    _write(root, "auth/permission.json", {
        "schema": "riskshield.data_evaluation.source_permission_record.v1",
        "status": "owner_authorised_this_round",
        "units": {unit_id: {"unit_id": unit_id} for unit_id in unit_ids},
        "note": "owner authorisation, not a per-platform licence record",
    })


def _install_units(monkeypatch, units: list[dict]) -> None:
    monkeypatch.setitem(bench.UNIT_SOURCES, "fixture_units", lambda root: units)


def _write_predictions(root: Path, units: list[dict]) -> None:
    """One document holding every unit's admitted row, plus its support files."""
    rows: dict[str, dict] = {}
    for unit in units:
        rows.update(_prediction_document(unit)["predictions"])
    _write_prediction_support(root, {
        "schema": imp.PREDICTION_IMPORT_SCHEMA,
        "task_type": imp.TASK_SENTIMENT,
        "document_version": "fixture-v1",
        "predictions": rows,
    })


def _scored_run(tmp_path, monkeypatch, unit_ids, *, permission=True):
    """Run the whole entry point once on synthetic material and return its parts."""
    root = _fixture_root(tmp_path, unit_ids)
    units = [_fixture_unit(root, index, unit_id) for index, unit_id in enumerate(unit_ids)]
    if permission:
        _permission_record(root, unit_ids)
    _write_predictions(root, units)
    _install_units(monkeypatch, units)
    config = _config(unit_ids)
    if not permission:
        config["cohorts"][0]["permission_record"] = None
        config["cohorts"][0]["expected_gated_unit_ids"] = []
    return root, config, bench.run_benchmark(config, root)


# --------------------------------------------------------------------------
# group 1: config validation
# --------------------------------------------------------------------------

def test_config_round_trips_through_a_file(tmp_path):
    path = _write(tmp_path, "config.json", _config(["u1"]))
    config = bench.load_config(path)
    assert config["config_version"] == "fixture-v1"
    assert bench.config_digest(config) == bench.config_digest(config)


def test_config_rejects_a_wrong_schema():
    payload = _config(["u1"])
    payload["schema"] = "something.else"
    with pytest.raises(bench.BenchmarkConfigError, match="schema"):
        bench.validate_config(payload)


@pytest.mark.parametrize("field", ["dataset_version", "label_version"])
def test_config_requires_versions(field):
    payload = _config(["u1"])
    payload["cohorts"][0][field] = ""
    with pytest.raises(bench.BenchmarkConfigError, match=field):
        bench.validate_config(payload)


def test_config_rejects_an_unknown_unit_source():
    payload = _config(["u1"], unit_source="not_a_loader")
    with pytest.raises(bench.BenchmarkConfigError, match="unit_source"):
        bench.validate_config(payload)


def test_config_rejects_duplicate_cohort_ids():
    payload = _config(["u1"])
    payload["cohorts"].append(dict(payload["cohorts"][0]))
    with pytest.raises(bench.BenchmarkConfigError, match="Duplicate cohort_id"):
        bench.validate_config(payload)


def test_config_requires_a_frozen_expected_unit_list():
    payload = _config(["u1"])
    del payload["cohorts"][0]["expected_gated_unit_ids"]
    with pytest.raises(bench.BenchmarkConfigError, match="expected_gated_unit_ids"):
        bench.validate_config(payload)


# --------------------------------------------------------------------------
# group 2: attaching recorded human annotations
# --------------------------------------------------------------------------

def test_no_configured_sources_keeps_the_loaders_annotations(tmp_path):
    """Regression: an empty source list must not wipe an already-labelled cohort."""
    root = _fixture_root(tmp_path, ["u1"])
    existing = [{"annotator_id": "leo", "label": "neutral"},
                {"annotator_id": "dick", "label": "neutral"}]
    unit = _fixture_unit(root, 0, "u1", annotations=list(existing), labels_agree=True)

    report = bench.attach_annotations([unit], [], root)

    assert unit["annotations"] == existing
    assert unit["labels_agree"] is True
    assert report["units"] == {}


def test_annotation_sources_attach_two_independent_labels(tmp_path):
    root = _fixture_root(tmp_path, ["u1"])
    unit = _fixture_unit(root, 0, "u1")

    report = bench.attach_annotations([unit], [{"path": "labels/v16.json"},
                                               {"path": "labels/v18.json"}], root)

    assert [row["annotator_id"] for row in unit["annotations"]] == ["leo", "dick"]
    assert unit["labels_agree"] is True
    assert report["units"]["u1"]["attached"] == 2
    assert unit["annotation_exclusions"] == []


def test_a_repeated_annotator_is_not_counted_twice(tmp_path):
    root = _fixture_root(tmp_path, ["u1"])
    _write(root, "labels/v18.json",
           {"annotator_id": "leo", "prediction_exposure": False,
            "saw_other_annotator_answer": False,
            "records": [{"unit_id": "u1", "label": "neutral"}]})
    unit = _fixture_unit(root, 0, "u1")

    bench.attach_annotations([unit], [{"path": "labels/v16.json"},
                                      {"path": "labels/v18.json"}], root)

    assert len(unit["annotations"]) == 1
    assert unit["annotation_exclusions"] == ["annotator_ids_not_distinct_in_v18"]
    assert unit["labels_agree"] is False


def test_a_submission_that_saw_a_prediction_is_not_attached(tmp_path):
    root = _fixture_root(tmp_path, ["u1"])
    _write(root, "labels/v18.json",
           {"annotator_id": "dick", "prediction_exposure": True,
            "saw_other_annotator_answer": False,
            "records": [{"unit_id": "u1", "label": "neutral"}]})
    unit = _fixture_unit(root, 0, "u1")

    bench.attach_annotations([unit], [{"path": "labels/v16.json"},
                                      {"path": "labels/v18.json"}], root)

    assert [row["annotator_id"] for row in unit["annotations"]] == ["leo"]
    assert unit["annotation_exclusions"] == ["blinding_not_established_in_v18"]


def test_a_label_outside_the_three_class_scope_is_not_attached(tmp_path):
    root = _fixture_root(tmp_path, ["u1"])
    _write(root, "labels/v18.json",
           {"annotator_id": "dick", "prediction_exposure": False,
            "saw_other_annotator_answer": False,
            "records": [{"unit_id": "u1", "label": "mixed"}]})
    unit = _fixture_unit(root, 0, "u1")

    bench.attach_annotations([unit], [{"path": "labels/v16.json"},
                                      {"path": "labels/v18.json"}], root)

    assert len(unit["annotations"]) == 1
    assert unit["annotation_exclusions"] == ["label_outside_formal_three_class_scope_in_v18"]


def test_label_side_status_separates_agreement_from_disagreement():
    agree = {"annotations": [{"annotator_id": "a", "label": "neutral"},
                             {"annotator_id": "b", "label": "neutral"}]}
    disagree = {"annotations": [{"annotator_id": "a", "label": "neutral"},
                                {"annotator_id": "b", "label": "negative"}]}
    assert bench.label_side_status(agree) == bench.LABEL_SIDE_AGREED
    assert bench.label_side_status(disagree) == bench.LABEL_SIDE_DISAGREE
    assert bench.label_side_status({"annotations": []}) == bench.LABEL_SIDE_INSUFFICIENT


# --------------------------------------------------------------------------
# group 3: the authorisation record
# --------------------------------------------------------------------------

def test_permission_record_binds_named_units(tmp_path):
    root = _fixture_root(tmp_path, ["u1", "u2"])
    _permission_record(root, ["u1"])
    units = [_fixture_unit(root, 0, "u1"), _fixture_unit(root, 1, "u2")]

    report = bench.load_permission_record(units, "auth/permission.json", root)

    assert report["bound_unit_ids"] == ["u1"]
    assert report["unbound_unit_ids"] == ["u2"]
    assert report["limit_note"].startswith("owner authorisation")
    assert units[0]["permission_evidence_ref"]["pointer"] == "/units/u1"
    assert units[1]["permission_evidence_ref"] is None


def test_without_a_permission_record_the_permission_gate_is_open(tmp_path):
    root = _fixture_root(tmp_path, ["u1"])
    units = [_fixture_unit(root, 0, "u1")]

    report = bench.load_permission_record(units, None, root)

    assert report == {"recorded": False, "path": None, "bound_unit_ids": []}
    assert units[0]["permission_evidence_ref"] is None
    gates = unit_gates(units[0])
    assert gates["gates"]["source_permission_evidence"] is False
    assert bench.PERMISSION_GATE in gates["reasons"]


# --------------------------------------------------------------------------
# group 4: the denominator invariant
# --------------------------------------------------------------------------

def test_run_scores_exactly_the_gated_units(tmp_path, monkeypatch):
    _, _, report = _scored_run(tmp_path, monkeypatch, ["u1", "u2"])

    cohort = report["cohorts"][0]
    assert cohort["gated_unit_ids"] == ["u1", "u2"]
    assert cohort["candidate_count"] == 2
    assert cohort["consistency_check"]["admitted_equals_scored"] is True
    assert cohort["consistency_check"]["denominator_equals_admitted_count"] is True
    assert cohort["consistency_check"]["excluded_units_present_in_denominator"] is False
    assert report["formal_combined"]["metrics"]["sentiment_three_class"]["denominator"] == 2
    assert report["formal_combined"]["metrics"]["sentiment_three_class"]["numerator"] == 2
    assert report["expectations_all_match"] is True
    assert report["headline"]["against_the_proclamation_thresholds"][
        "three_class_accuracy"]["required"] == 0.88


def test_a_unit_without_a_permission_entry_is_excluded_from_the_denominator(tmp_path, monkeypatch):
    root = _fixture_root(tmp_path, ["u1", "u2"])
    units = [_fixture_unit(root, 0, "u1"), _fixture_unit(root, 1, "u2")]
    _permission_record(root, ["u1"])
    _write_predictions(root, units)
    _install_units(monkeypatch, units)

    report = bench.run_benchmark(_config(["u1", "u2"], expected=["u1"]), root)

    cohort = report["cohorts"][0]
    assert cohort["gated_unit_ids"] == ["u1"]
    assert cohort["excluded_unit_ids"] == ["u2"]
    assert cohort["scoreable_without_permission_record"] == []
    assert cohort["expectation_matches"] is True
    row = next(row for row in cohort["eligibility_table"] if row["unit_id"] == "u2")
    assert row["gated"] is False
    assert row["all_gaps"] == [bench.PERMISSION_GATE]


def test_a_frozen_expectation_is_reported_not_forced(tmp_path, monkeypatch):
    root, _, report = _scored_run(tmp_path, monkeypatch, ["u1", "u2"])
    assert report["expectations_all_match"] is True

    mismatch = bench.run_benchmark(_config(["u1", "u2"], expected=["u1"]), root)

    assert mismatch["expectations_all_match"] is False
    assert mismatch["headline"]["three_class"]["denominator"] == 2


def test_zero_gated_units_reports_not_computable(tmp_path, monkeypatch):
    _, _, report = _scored_run(tmp_path, monkeypatch, ["u1"], permission=False)

    headline = report["headline"]["three_class"]
    assert headline["denominator"] == 0
    assert headline["value"] is None
    assert headline["undefined_reason"]


def test_invariant_rejects_an_excluded_unit_in_the_denominator():
    units = [{"unit_id": "u1"}, {"unit_id": "u2"}]
    metrics = {"sentiment_three_class": {
        "scored_units": [{"unit_id": "u1"}, {"unit_id": "u2"}],
        "sample_size": 2, "denominator": 2}}
    with pytest.raises(bench.BenchmarkInvariantError, match="excluded"):
        bench.assert_denominator_matches(units, {"u2"}, metrics)


def test_invariant_rejects_a_sample_size_mismatch():
    units = [{"unit_id": "u1"}]
    metrics = {"sentiment_three_class": {
        "scored_units": [{"unit_id": "u1"}], "sample_size": 0, "denominator": 1}}
    with pytest.raises(bench.BenchmarkInvariantError, match="sample size"):
        bench.assert_denominator_matches(units, set(), metrics)


def test_invariant_rejects_an_engine_that_dropped_a_gated_unit():
    units = [{"unit_id": "u1"}]
    metrics = {"sentiment_three_class": {
        "scored_units": [], "sample_size": 0, "denominator": 0}}
    with pytest.raises(bench.BenchmarkInvariantError, match="different unit set"):
        bench.assert_denominator_matches(units, set(), metrics)


def test_an_engine_that_drops_a_gated_unit_fails_the_run(tmp_path, monkeypatch):
    """The gates look at a unit's own fields; the engine re-resolves the evidence.

    A unit whose label evidence does not resolve passes the gate table and is
    dropped by the engine. That is exactly the drift the invariant exists to
    catch, so the run must fail rather than report a smaller denominator.
    """
    root = _fixture_root(tmp_path, ["u1"])
    unresolvable = [
        {"annotator_id": "leo", "label": "neutral", "independent": True,
         "prediction_seen": False, "submitted_at": "2026-10-12T00:21:45+08:00",
         "evidence_refs": [{"path": "labels/missing.json", "pointer": "/units/0/label"}]},
        {"annotator_id": "dick", "label": "neutral", "independent": True,
         "prediction_seen": False, "submitted_at": "2026-10-12T00:25:48+08:00",
         "evidence_refs": [{"path": "labels/missing.json", "pointer": "/records/0/label"}]},
    ]
    unit = _fixture_unit(root, 0, "u1", annotations=unresolvable, labels_agree=True)
    _permission_record(root, ["u1"])
    _write_predictions(root, [unit])
    _install_units(monkeypatch, [unit])

    prepared = [dict(unit)]
    bench.prepare_declared_cutoff_pointers(prepared)
    bench.load_permission_record(prepared, "auth/permission.json", root)
    bench.admit_predictions(prepared, "pred/predictions.json", root)
    assert unit_gates(prepared[0])["passed"] is True

    with pytest.raises(bench.BenchmarkInvariantError, match="different unit set"):
        bench.run_benchmark(_config(["u1"], annotation_sources=False), root)


# --------------------------------------------------------------------------
# group 5: cohort handling
# --------------------------------------------------------------------------

def test_cohorts_sharing_an_identity_are_refused(tmp_path, monkeypatch):
    root = _fixture_root(tmp_path, ["u1"])
    unit = _fixture_unit(root, 0, "u1")
    _permission_record(root, ["u1"])
    _write_predictions(root, [unit])
    _install_units(monkeypatch, [unit])

    config = _config(["u1"])
    second = dict(config["cohorts"][0])
    second["cohort_id"] = "second-cohort"
    config["cohorts"].append(second)

    with pytest.raises(bench.BenchmarkInvariantError, match="share candidate identities"):
        bench.run_benchmark(config, root)


def test_the_output_directory_must_live_inside_the_material_root(tmp_path, monkeypatch):
    root, _, _ = _scored_run(tmp_path, monkeypatch, ["u1"])

    with pytest.raises(bench.BenchmarkConfigError, match="material root"):
        bench.run_benchmark(_config(["u1"], out_dir="../outside"), root)


def test_evaluate_combined_refuses_a_duplicated_unit():
    unit = {"unit_id": "u1", "event_id": "e1", "source_record_id": "s1"}
    cohort = {"cohort_id": "c", "units": [unit, dict(unit)], "gated_unit_ids": ["u1"],
              "excluded_unit_ids": [], "prediction_report": {"admitted": []}}

    with pytest.raises(bench.BenchmarkInvariantError, match="more than one cohort"):
        bench.evaluate_combined([cohort], root=Path("."), cutoffs_document=None,
                                dataset_version="d", label_version="l")


# --------------------------------------------------------------------------
# group 6: cutoff evidence
# --------------------------------------------------------------------------

def test_a_configured_attestation_is_used_when_it_covers_every_derived_unit(tmp_path):
    root = _fixture_root(tmp_path, ["u1"])
    _write(root, "frozen/attestation.json", {"units": {"u1": {"cutoff": CUTOFF}}})
    unit = _fixture_unit(
        root, 0, "u1",
        cutoff_provenance="derived_backtest_window_rule_not_historical_freeze")

    state = bench.ensure_cutoff_attestation([unit], "frozen/attestation.json", "out/run", root)

    assert state["generated"] is False
    assert state["reference"] == "frozen/attestation.json"
    assert not (root / "out/run/derived-cutoff-attestation.json").exists()


def test_a_configured_attestation_that_misses_a_unit_is_extended(tmp_path):
    """Regression: a config written before a cohort existed left that cohort unscorable.

    The v04 attestation covers the five frozen units only. The sixty collected
    ones resolve their cutoff against the same document, so once their labels
    arrived they passed the gate and were then dropped by the engine for a cutoff
    that could not resolve -- silently shrinking the denominator.
    """
    root = _fixture_root(tmp_path, ["u1"])
    _write(root, "frozen/attestation.json", {"units": {"someone-else": {"cutoff": CUTOFF}}})
    unit = _fixture_unit(
        root, 0, "u1",
        cutoff_provenance="derived_backtest_window_rule_not_historical_freeze")

    state = bench.ensure_cutoff_attestation([unit], "frozen/attestation.json", "out/run", root)

    assert state["generated"] is True
    assert state["covered_by_the_configured_document"] == []
    assert state["added_by_generation"] == ["u1"]
    document = json.loads((root / state["reference"]).read_text(encoding="utf-8"))
    assert "someone-else" in document["units"]
    assert "u1" in document["units"]
    case = bench._cohort_case(unit, {"sentiment": None, "direction": None}, "v1", "holdout",
                              state["reference"])
    assert case["cutoff_evidence_ref"]["path"] == state["reference"]


def test_a_declared_cutoff_unit_points_at_its_own_packet(tmp_path):
    root = _fixture_root(tmp_path, ["u1"])
    prepared = [dict(_fixture_unit(root, 0, "u1"))]
    bench.prepare_declared_cutoff_pointers(prepared)
    assert prepared[0]["declared_cutoff_pointer"] == "/units/0/cutoff_utc"

    case = bench._cohort_case(prepared[0], {"sentiment": None, "direction": None},
                              "v1", "holdout", None)

    assert case["cutoff_evidence_ref"] == {"path": "frozen/packet.json",
                                           "pointer": "/units/0/cutoff_utc"}


def test_derived_cutoff_units_get_a_generated_attestation(tmp_path):
    root = _fixture_root(tmp_path, ["u1"])
    unit = _fixture_unit(root, 0, "u1",
                         cutoff_provenance="derived_backtest_window_rule_not_historical_freeze")

    state = bench.ensure_cutoff_attestation([unit], None, "out/run", root)

    assert state["generated"] is True
    assert state["reference"] == "out/run/derived-cutoff-attestation.json"
    document = json.loads((root / state["reference"]).read_text(encoding="utf-8"))
    assert document["units"]["u1"]["cutoff"] == CUTOFF
    assert document["units"]["u1"]["frozen_visible_at"] == VISIBLE_AT


def test_a_configured_attestation_is_used_instead_of_generating_one(tmp_path):
    root = _fixture_root(tmp_path, ["u1"])
    _write(root, "frozen/attestation.json", {"units": {"u1": {"cutoff": CUTOFF}}})
    unit = _fixture_unit(root, 0, "u1",
                         cutoff_provenance="derived_backtest_window_rule_not_historical_freeze")

    state = bench.ensure_cutoff_attestation([unit], "frozen/attestation.json", "out/run", root)

    assert state["generated"] is False
    assert state["reference"] == "frozen/attestation.json"
    assert not (root / "out/run/derived-cutoff-attestation.json").exists()


# --------------------------------------------------------------------------
# group 8: recorded adjudication
# --------------------------------------------------------------------------

def _adjudication_record(root: Path, rows: list[dict], **overrides) -> None:
    """A minimal but real adjudication record plus the rule it cites."""
    _write(root, "adjudication/label-rule-v1.json", {"rule": "fixture rule"})
    document = {
        "schema": "riskshield.data_evaluation.adjudication_record.v3",
        "decided_by": "project owner",
        "rule_source": "label-rule-v1.json",
        "records": rows,
    }
    document.update(overrides)
    _write(root, "adjudication/record-v3.json", document)


def _adjudication_row(unit_id: str, label: str = "negative", **overrides) -> dict:
    row = {"unit_id": unit_id, "resolved_label": label,
           "rule_arm": "negative_arm: fixture", "both_original_answers_preserved": True}
    row.update(overrides)
    return row


def test_unit_gates_accepts_a_valid_recorded_adjudication(tmp_path):
    root = _fixture_root(tmp_path, ["u1"])
    unit = _fixture_unit(root, 0, "u1", annotations=[
        {"annotator_id": "leo", "label": "neutral"},
        {"annotator_id": "dick", "label": "negative"},
    ], labels_agree=False)

    assert "independent_labels_disagree_no_adjudication" in unit_gates(unit)["reasons"]

    _adjudication_record(root, [_adjudication_row("u1")])
    bench.attach_adjudications([unit], "adjudication/record-v3.json", root)

    gates = unit_gates(unit)
    assert "independent_labels_disagree_no_adjudication" not in gates["reasons"]
    assert gates["label_basis"] == "adjudicated_disagreement"
    assert gates["gates"]["adjudicated_disagreement"] is True
    assert gates["gates"]["independent_human_labels"] is True


@pytest.mark.parametrize("row_overrides", [
    {"resolved_label": "mixed"},
    {"both_original_answers_preserved": False},
])
def test_unit_gates_rejects_an_invalid_recorded_adjudication(tmp_path, row_overrides):
    root = _fixture_root(tmp_path, ["u1"])
    unit = _fixture_unit(root, 0, "u1", annotations=[
        {"annotator_id": "leo", "label": "neutral"},
        {"annotator_id": "dick", "label": "negative"},
    ], labels_agree=False)
    _adjudication_record(root, [_adjudication_row("u1", **row_overrides)])

    report = bench.attach_adjudications([unit], "adjudication/record-v3.json", root)

    assert report["attached_unit_ids"] == []
    assert report["invalid_unit_ids"] == ["u1"]
    assert "independent_labels_disagree_no_adjudication" in unit_gates(unit)["reasons"]


def test_attach_adjudications_locates_the_cited_rule(tmp_path):
    root = _fixture_root(tmp_path, ["u1"])
    unit = _fixture_unit(root, 0, "u1")
    _adjudication_record(root, [_adjudication_row("u1")])

    report = bench.attach_adjudications([unit], "adjudication/record-v3.json", root)

    assert report["attached_unit_ids"] == ["u1"]
    assert report["rule_source_located"] == "adjudication/label-rule-v1.json"
    assert report["adjudicated_at_recorded"] is None
    assert "no timestamp" in report["adjudicated_at_note"]
    assert unit["adjudication"]["evidence_refs"] == [
        {"path": "adjudication/record-v3.json", "pointer": "/records/0/resolved_label"}]


def test_attach_adjudications_refuses_a_document_that_is_not_one(tmp_path):
    root = _fixture_root(tmp_path, ["u1"])
    _write(root, "adjudication/record-v3.json", {"schema": "something.else", "records": []})

    with pytest.raises(bench.BenchmarkConfigError, match="not an adjudication record"):
        bench.attach_adjudications([_fixture_unit(root, 0, "u1")],
                                   "adjudication/record-v3.json", root)


def test_attach_adjudications_without_a_record_leaves_units_untouched(tmp_path):
    root = _fixture_root(tmp_path, ["u1"])
    unit = _fixture_unit(root, 0, "u1")

    report = bench.attach_adjudications([unit], None, root)

    assert report["recorded"] is False
    assert unit["adjudication"] is None


def test_label_side_accounting_separates_agreement_from_adjudication(tmp_path):
    root = _fixture_root(tmp_path, ["u1", "u2"])
    agreed = _fixture_unit(root, 0, "u1", annotations=[
        {"annotator_id": "leo", "label": "neutral"},
        {"annotator_id": "dick", "label": "neutral"}], labels_agree=True)
    disputed = _fixture_unit(root, 1, "u2", annotations=[
        {"annotator_id": "leo", "label": "neutral"},
        {"annotator_id": "dick", "label": "negative"}], labels_agree=False)
    _adjudication_record(root, [_adjudication_row("u2")])
    bench.attach_adjudications([agreed, disputed], "adjudication/record-v3.json", root)

    accounting = bench.label_side_accounting([agreed, disputed])

    assert accounting["raw_agreement_units"] == 1
    assert accounting["adjudicated_units"] == 1
    assert accounting["coverage_after_adjudication_units"] == 2
    assert accounting["unresolved_disagreement_units"] == 0
    assert accounting["raw_agreement_share_of_covered"]["value"] == 0.5


def test_an_adjudicated_unit_scores_in_a_full_run(tmp_path, monkeypatch):
    root = _fixture_root(tmp_path, ["u1"])
    _write(root, "labels/v18.json",
           {"annotator_id": "dick", "prediction_exposure": False,
            "saw_other_annotator_answer": False,
            "records": [{"unit_id": "u1", "label": "negative"}]})
    unit = _fixture_unit(root, 0, "u1")
    _permission_record(root, ["u1"])
    _write_predictions(root, [unit])
    _install_units(monkeypatch, [unit])
    _adjudication_record(root, [_adjudication_row("u1")])

    config = _config(["u1"])
    config["cohorts"][0]["adjudication_record"] = "adjudication/record-v3.json"
    report = bench.run_benchmark(config, root)

    cohort = report["cohorts"][0]
    assert cohort["gated_unit_ids"] == ["u1"]
    assert cohort["adjudication_report"]["attached_unit_ids"] == ["u1"]
    assert cohort["label_side_accounting"]["adjudicated_units"] == 1
    scored = report["headline"]["scored_units"][0]
    assert scored["truth"] == "negative"
    assert scored["label_basis"] == "adjudicated_disagreement"
    assert report["formal_combined"]["metrics"]["label_basis_accounting"][
        "coverage_after_adjudication"]["units"] == 1


# --------------------------------------------------------------------------
# group 9: a collected (expansion) cohort
# --------------------------------------------------------------------------

def _expansion_title(index: int) -> str:
    return f"Expansion fixture title {index} about an insurance claim decision"


def _expansion_documents(root: Path, unit_ids: list[str], *, archive_hash_for=None) -> None:
    """A candidate pool plus the archive evidence that proves its titles."""
    units = []
    records = []
    for index, unit_id in enumerate(unit_ids):
        title = _expansion_title(index)
        text_sha = hashlib.sha256(title.encode("utf-8")).hexdigest()
        units.append({
            "unit_id": unit_id,
            "post_id": f"exp{index}",
            "subreddit": "Insurance",
            "source_record_id": f"reddit_archived_post_exp{index}",
            "source_version": SOURCE_VERSION,
            "frozen_text": title,
            "text_sha256": text_sha,
            "source_url": f"https://www.reddit.com/comments/exp{index}/",
            "created_at_utc": "2024-01-01T00:00:00Z",
            "retrieved_on_utc": "2024-01-01T00:00:15Z",
            "dataset_split": "development_set" if index % 2 == 0 else "final_evaluation_set",
        })
        records.append({
            "source_ref": f"reddit_archived_post_exp{index}",
            "post_id": f"exp{index}",
            "subreddit": "Insurance",
            "title_sha256": (archive_hash_for or {}).get(unit_id, text_sha),
            "created_at_utc": "2024-01-01T00:00:00Z",
            "retrieved_on_utc": "2024-01-01T00:00:15Z",
        })
    _write(root, "expansion/pool.json",
           {"schema": "riskshield.data_evaluation.candidate_pool.v1", "units": units})
    _write(root, "expansion/archive.json",
           {"schema": "riskshield.final_benchmark.archive_post_evidence.v1", "records": records})


def _expansion_cohort(unit_ids: list[str], **overrides) -> dict:
    cohort = {
        "cohort_id": "expansion",
        "unit_source": "expansion_candidate_pool",
        "dataset_version": "expansion-v1",
        "label_version": "expansion-labels-v1",
        "annotation_sources": [{"path": "labels/v16.json"}, {"path": "labels/v18.json"}],
        "prediction_document": "pred/predictions.json",
        "permission_record": "auth/permission.json",
        "candidate_pool": "expansion/pool.json",
        "archive_evidence": "expansion/archive.json",
        "expected_gated_unit_ids": sorted(unit_ids),
    }
    cohort.update(overrides)
    return cohort


def _expansion_config(unit_ids: list[str], **cohort_overrides) -> dict:
    config = _config(unit_ids)
    config["cohorts"] = [_expansion_cohort(unit_ids, **cohort_overrides)]
    return config


def test_the_expansion_loader_derives_units_from_the_pool_and_the_archive(tmp_path):
    root = _fixture_root(tmp_path, ["exp_u1"])
    _expansion_documents(root, ["exp_u1"])
    cohort = _expansion_cohort(["exp_u1"])

    units = bench.load_expansion_units(root, cohort)

    assert len(units) == 1
    unit = units[0]
    assert unit["unit_id"] == "exp_u1"
    assert unit["event_id"] == "exp_u1"
    assert unit["publisher"] == "Reddit r/Insurance archived post"
    assert unit["title_hash_match"] is True
    assert unit["archive_title_sha256"] == unit["text_sha256"]
    assert unit["cutoff_provenance"] == bench.DERIVED_CUTOFF_PROVENANCE
    assert unit["visible_at"] == "2024-01-01T00:00:15Z"
    assert unit["cutoff"] == "2024-01-01T00:30:15Z"
    assert unit["dataset_split"] == "development_set"
    assert unit["permission_evidence_ref"] is None


def test_the_expansion_loader_refuses_a_pool_post_missing_from_the_archive(tmp_path):
    root = _fixture_root(tmp_path, ["exp_u1"])
    _expansion_documents(root, ["exp_u1"])
    _write(root, "expansion/archive.json",
           {"schema": "x", "records": [{"source_ref": "something_else",
                                        "title_sha256": "0" * 64}]})

    with pytest.raises(bench.BenchmarkConfigError, match="no record for"):
        bench.load_expansion_units(root, _expansion_cohort(["exp_u1"]))


def test_the_expansion_loader_reports_a_title_hash_mismatch_rather_than_hiding_it(tmp_path):
    root = _fixture_root(tmp_path, ["exp_u1"])
    _expansion_documents(root, ["exp_u1"], archive_hash_for={"exp_u1": "f" * 64})

    unit = bench.load_expansion_units(root, _expansion_cohort(["exp_u1"]))[0]

    assert unit["title_hash_match"] is False
    assert unit["archive_title_sha256"] == "f" * 64
    assert "original_text_hash_missing_or_mismatched" in unit_gates(unit)["reasons"]


@pytest.mark.parametrize("missing", ["candidate_pool", "archive_evidence"])
def test_a_collected_cohort_needs_both_documents(missing):
    payload = _expansion_config(["exp_u1"])
    del payload["cohorts"][0][missing]

    with pytest.raises(bench.BenchmarkConfigError, match=missing):
        bench.validate_config(payload)


def test_an_unknown_unit_source_names_the_known_ones():
    payload = _config(["u1"], unit_source="not_a_loader")

    with pytest.raises(bench.BenchmarkConfigError, match="expansion_candidate_pool"):
        bench.validate_config(payload)


@pytest.mark.parametrize("field", ["unit_id", "source_record_id", "source_version",
                                   "frozen_text", "text_sha256", "retrieved_on_utc"])
def test_a_pool_unit_missing_a_required_field_is_refused(tmp_path, field):
    root = _fixture_root(tmp_path, ["exp_u1"])
    _expansion_documents(root, ["exp_u1"])
    pool = json.loads((root / "expansion/pool.json").read_text(encoding="utf-8"))
    del pool["units"][0][field]
    _write(root, "expansion/pool.json", pool)

    with pytest.raises(bench.BenchmarkConfigError, match=field):
        bench.load_expansion_units(root, _expansion_cohort(["exp_u1"]))


def test_an_expansion_cohort_scores_end_to_end(tmp_path):
    unit_ids = ["exp_u1", "exp_u2"]
    root = _fixture_root(tmp_path, unit_ids)
    _expansion_documents(root, unit_ids)
    cohort = _expansion_cohort(unit_ids)
    _permission_record(root, unit_ids)

    units = bench.load_expansion_units(root, cohort)
    for unit in units:
        unit["_root"] = str(root)
    _write_predictions(root, units)

    report = bench.run_benchmark(_expansion_config(unit_ids), root)

    row = report["cohorts"][0]
    assert row["gated_unit_ids"] == unit_ids
    assert row["consistency_check"]["denominator_equals_admitted_count"] is True
    assert report["headline"]["three_class"]["denominator"] == 2
    assert report["headline"]["three_class"]["numerator"] == 2
    assert report["expectations_all_match"] is True
    assert report["cutoff_attestation"]["generated"] is True
    assert (root / report["cutoff_attestation"]["reference"]).is_file()


def test_an_expansion_unit_without_a_permission_entry_is_excluded(tmp_path):
    unit_ids = ["exp_u1", "exp_u2"]
    root = _fixture_root(tmp_path, unit_ids)
    _expansion_documents(root, unit_ids)
    _permission_record(root, [unit_ids[0]])

    units = bench.load_expansion_units(root, _expansion_cohort(unit_ids))
    for unit in units:
        unit["_root"] = str(root)
    _write_predictions(root, units)

    report = bench.run_benchmark(
        _expansion_config(unit_ids, expected_gated_unit_ids=[unit_ids[0]]), root)

    row = report["cohorts"][0]
    assert row["gated_unit_ids"] == [unit_ids[0]]
    assert row["excluded_unit_ids"] == [unit_ids[1]]


# --------------------------------------------------------------------------
# group 10: the trajectory track
# --------------------------------------------------------------------------

TRAJ_CUTOFF = "2024-03-01T00:00:00Z"


def _seal_event(root: Path, run_dir: str, event_id: str, *, leak: int = 0,
                query: str = "Example Insurer") -> dict:
    base = f"trajectory/{run_dir}"
    evidence = {
        "event_id": event_id,
        "subject": event_id,
        "event_deduplication": "one controversy",
        "cutoff": TRAJ_CUTOFF,
        "cutoff_basis": "fixture",
        "baseline_window_utc": ["2024-02-23T00:00:00Z", TRAJ_CUTOFF],
        "observation_window_utc": [TRAJ_CUTOFF, "2024-03-09T00:00:00Z"],
        "baseline_daily_series": {
            "2024-02-23": {"story": {"nb_hits": 2}, "comment": {"nb_hits": 5}},
            "2024-02-24": {"story": {"nb_hits": 1}, "comment": {"nb_hits": 3}},
        },
        "source": {"source_family": "Hacker News",
                   "endpoint": "https://hn.algolia.com/api/v1/search_by_date",
                   "query": query, "numeric_filter": "fixture"},
        "title_screen_applied": False,
    }
    sealed_document = {**evidence, "baseline_totals": {"stories": 3, "comments": 8},
                       "baseline_is_empty": False, "frame_document": "trajectory/frame.json"}
    _write(root, f"{base}/sealed-input-v1.json", sealed_document)
    evidence_digest = hashlib.sha256(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()
    _write(root, f"{base}/input-freeze-record-v1.json", {
        "schema": "riskshield.data_evaluation.trajectory_input_freeze.v1",
        "event_id": event_id, "frozen_at": "2024-03-01T00:00:00Z",
        "sealed_evidence_sha256": evidence_digest,
        "sealed_input_path": f"{base}/sealed-input-v1.json",
        "baseline_days_fetched": 2,
        "observation_days_fetched_in_phase_1": leak,
        "observation_leak_days": ["2024-03-02"] if leak else [],
        "predictor_saw_only_the_baseline_window": leak == 0,
    })
    _write(root, f"{base}/prediction-v1.json", {
        "schema": "riskshield.data_evaluation.trajectory_prediction.v1",
        "event_id": event_id, "task_type": "event_direction",
        "label": "escalation", "reason": "fixture",
        "execution_mode": "formal_model_run_v1",
        "method_version": "fixture-direction-v1",
        "run_id": f"run_fixture_{event_id}",
        "predicted_at": "2024-03-01T00:00:00Z",
        "input_binding": {"sealed_input_path": f"{base}/sealed-input-v1.json",
                          "sealed_evidence_sha256": evidence_digest,
                          "frozen_at": "2024-03-01T00:00:00Z"},
        "labels_accessed": False, "outcomes_accessed": False,
    })
    return {"run_dir": run_dir, "event_id": event_id,
            "subject": event_id, "query": query, "cutoff": TRAJ_CUTOFF}


def _trajectory_root(tmp_path: Path, event_ids: list[str], **seal_overrides) -> Path:
    events = [_seal_event(tmp_path, f"event-{index + 1}", event_id, **seal_overrides)
              for index, event_id in enumerate(event_ids)]
    _write(tmp_path, "trajectory/frame.json",
           {"schema": "riskshield.data_evaluation.trajectory_event_frame.v1",
            "events": events})
    return tmp_path


def _direction_submissions(root: Path, event_ids: list[str], labels: dict[str, str]) -> None:
    _write(root, "direction/pass-a.json",
           {"annotator_id": "leo", "prediction_exposure": False,
            "saw_other_annotator_answer": False,
            "units": [{"unit_id": event_id, "direction_label": labels[event_id]}
                      for event_id in event_ids]})
    _write(root, "direction/pass-b.json",
           {"annotator_id": "dick", "prediction_exposure": False,
            "saw_other_annotator_answer": False,
            "records": [{"unit_id": event_id, "direction_label": labels[event_id]}
                        for event_id in event_ids]})


def _trajectory_cohort(event_ids: list[str], **overrides) -> dict:
    cohort = {
        "cohort_id": "trajectory",
        "track": "direction",
        "unit_source": "trajectory_event_frame",
        "dataset_version": "trajectory-v1",
        "label_version": "trajectory-labels-v1",
        "event_frame": "trajectory/frame.json",
        "event_root": "trajectory",
        "annotation_sources": [{"path": "direction/pass-a.json"},
                               {"path": "direction/pass-b.json"}],
        "expected_gated_unit_ids": sorted(event_ids),
        "outputs_dir": "out/run",
    }
    cohort.update(overrides)
    return cohort


def test_the_trajectory_loader_builds_one_unit_per_sealed_event(tmp_path):
    root = _trajectory_root(tmp_path, ["evt-1", "evt-2"])

    units = bench.trajectory_units(root, _trajectory_cohort(["evt-1", "evt-2"]))

    assert [unit["unit_id"] for unit in units] == ["evt-1", "evt-2"]
    assert units[0]["cutoff"] == TRAJ_CUTOFF
    assert units[0]["visible_at"] == TRAJ_CUTOFF
    assert units[0]["unit_kind"] == bench.TRAJECTORY_UNIT_KIND
    assert units[0]["source_version"].endswith("/chat/completions") is False
    assert units[0]["publisher"] == "Hacker News (Algolia daily series)"
    assert units[0]["human_direction_label"] is None


def test_the_trajectory_loader_refuses_an_event_with_no_sealed_input(tmp_path):
    root = _trajectory_root(tmp_path, ["evt-1"])
    (root / "trajectory/event-1/sealed-input-v1.json").unlink()

    with pytest.raises(bench.BenchmarkConfigError, match="no sealed input"):
        bench.trajectory_units(root, _trajectory_cohort(["evt-1"]))


def test_the_trajectory_loader_refuses_an_event_that_leaked_the_observation_window(tmp_path):
    root = _trajectory_root(tmp_path, ["evt-1"], leak=1)

    with pytest.raises(bench.BenchmarkConfigError, match="not ex-ante"):
        bench.trajectory_units(root, _trajectory_cohort(["evt-1"]))


def test_direction_labels_attach_only_when_blind_and_distinct(tmp_path):
    root = _trajectory_root(tmp_path, ["evt-1"])
    _direction_submissions(root, ["evt-1"], {"evt-1": "calming"})
    units = bench.trajectory_units(root, _trajectory_cohort(["evt-1"]))

    report = bench.attach_direction_labels(units, _trajectory_cohort(["evt-1"])["annotation_sources"],
                                           root)

    assert report["units"]["evt-1"]["attached"] == 2
    assert units[0]["human_direction_label"] == "calming"
    assert bench.trajectory_direction_gate(units[0])["reasons"] == [
        "no_admitted_formal_direction_prediction"]


def test_a_repeated_direction_annotator_is_not_counted_twice(tmp_path):
    root = _trajectory_root(tmp_path, ["evt-1"])
    _write(root, "direction/pass-a.json",
           {"annotator_id": "leo", "prediction_exposure": False,
            "saw_other_annotator_answer": False,
            "units": [{"unit_id": "evt-1", "direction_label": "calming"}]})
    _write(root, "direction/pass-b.json",
           {"annotator_id": "leo", "prediction_exposure": False,
            "saw_other_annotator_answer": False,
            "records": [{"unit_id": "evt-1", "direction_label": "calming"}]})
    units = bench.trajectory_units(root, _trajectory_cohort(["evt-1"]))

    bench.attach_direction_labels(units, _trajectory_cohort(["evt-1"])["annotation_sources"], root)

    assert len(units[0]["direction_annotations"]) == 1
    assert units[0]["direction_annotation_exclusions"] == ["annotator_ids_not_distinct_in_pass-b"]


def test_a_stable_human_label_is_reported_not_mapped_onto_two_labels(tmp_path):
    root = _trajectory_root(tmp_path, ["evt-1"])
    _direction_submissions(root, ["evt-1"], {"evt-1": "stable"})
    units = bench.trajectory_units(root, _trajectory_cohort(["evt-1"]))
    bench.attach_direction_labels(units, _trajectory_cohort(["evt-1"])["annotation_sources"], root)

    reasons = bench.trajectory_direction_gate(units[0])["reasons"]

    assert "human_direction_label_outside_the_engine_label_space" in reasons


def test_a_disagreeing_direction_pair_is_excluded(tmp_path):
    root = _trajectory_root(tmp_path, ["evt-1"])
    _write(root, "direction/pass-a.json",
           {"annotator_id": "leo", "prediction_exposure": False,
            "saw_other_annotator_answer": False,
            "units": [{"unit_id": "evt-1", "direction_label": "calming"}]})
    _write(root, "direction/pass-b.json",
           {"annotator_id": "dick", "prediction_exposure": False,
            "saw_other_annotator_answer": False,
            "records": [{"unit_id": "evt-1", "direction_label": "escalation"}]})
    units = bench.trajectory_units(root, _trajectory_cohort(["evt-1"]))
    bench.attach_direction_labels(units, _trajectory_cohort(["evt-1"])["annotation_sources"], root)

    assert "direction_labels_disagree_no_adjudication" in \
        bench.trajectory_direction_gate(units[0])["reasons"]


def test_trajectory_predictions_pass_through_the_same_admission_point(tmp_path):
    root = _trajectory_root(tmp_path, ["evt-1", "evt-2"])
    units = bench.trajectory_units(root, _trajectory_cohort(["evt-1", "evt-2"]))

    outcome = bench.admit_trajectory_predictions(units, root, "out/run")

    assert outcome["admission"]["counts"] == {"rows": 2, "admitted": 2, "rejected": 0,
                                              "unmatched": 0}
    assert all(unit["prediction"]["admitted"] for unit in units)
    assert (root / outcome["package_relative"]).is_file()
    assert outcome["admission"]["task_type"] == "event_direction"


def test_the_trajectory_track_scores_its_own_denominator(tmp_path):
    event_ids = ["evt-1", "evt-2"]
    root = _trajectory_root(tmp_path, event_ids)
    _direction_submissions(root, event_ids, {"evt-1": "escalation", "evt-2": "escalation"})
    cohort = _trajectory_cohort(event_ids)
    units = bench.trajectory_units(root, cohort)
    bench.attach_direction_labels(units, cohort["annotation_sources"], root)
    admission = bench.admit_trajectory_predictions(units, root, cohort["outputs_dir"])
    gated = [unit for unit in units if bench.trajectory_direction_gate(unit)["passed"]]
    assert len(gated) == 2

    _, _, metrics = bench.score_trajectory_set(
        gated, dataset_version="trajectory-v1", label_version="trajectory-labels-v1",
        evidence_root=root, package_relative=admission["package_relative"])
    consistency = bench.assert_direction_denominator_matches(gated, metrics)

    assert metrics["direction_agreement"]["denominator"] == 2
    assert metrics["direction_agreement"]["numerator"] == 2
    assert metrics["sentiment_three_class"]["denominator"] == 0
    assert metrics["sentiment_three_class"]["undefined_reason"]
    assert consistency["admitted_equals_scored"] is True
    assert consistency["engine_scored_event_ids"] == event_ids


def test_the_direction_denominator_invariant_rejects_a_mismatch():
    units = [{"event_id": "evt-1"}]
    metrics = {"direction_agreement": {"scored_events": [], "sample_size_events": 0,
                                       "denominator": 0}}

    with pytest.raises(bench.BenchmarkInvariantError, match="different event set"):
        bench.assert_direction_denominator_matches(units, metrics)


# --------------------------------------------------------------------------
# group 11: one entry point, two tracks
# --------------------------------------------------------------------------

def _mixed_config(tmp_path, monkeypatch, *, same_identity: bool = False) -> tuple[Path, dict]:
    """A config holding one sentiment cohort and one direction cohort."""
    sentiment_id = "evt-1" if same_identity else "u1"
    root = _fixture_root(tmp_path, [sentiment_id])
    _trajectory_root(tmp_path, ["evt-1"])
    _direction_submissions(root, ["evt-1"], {"evt-1": "escalation"})
    _permission_record(root, [sentiment_id])
    sentiment_unit = _fixture_unit(root, 0, sentiment_id)
    _write_predictions(root, [sentiment_unit])
    _install_units(monkeypatch, [sentiment_unit])

    config = _config([sentiment_id])
    direction_cohort = _trajectory_cohort(["evt-1"])
    direction_cohort["track"] = "direction"
    config["cohorts"].append(direction_cohort)
    return root, config


def test_a_mixed_config_reports_both_denominators_separately(tmp_path, monkeypatch):
    root, config = _mixed_config(tmp_path, monkeypatch)

    report = bench.run_benchmark(config, root)

    denominators = report["track_denominators"]
    assert denominators["sentiment"]["denominator"] == 1
    assert denominators["sentiment"]["track"] == "sentiment"
    assert denominators["direction"]["denominator"] == 1
    assert denominators["direction"]["track"] == "direction"
    assert denominators["direction"]["scored_event_ids"] == ["evt-1"]
    assert report["formal_combined"]["metrics"]["sentiment_three_class"]["denominator"] == 1
    assert report["formal_combined_direction"]["metrics"][
        "direction_agreement"]["denominator"] == 1
    assert report["expectations_all_match"] is True
    assert [row["track"] for row in report["cohorts"]] == ["sentiment", "direction"]


def test_the_two_tracks_never_share_a_denominator(tmp_path, monkeypatch):
    root, config = _mixed_config(tmp_path, monkeypatch)

    report = bench.run_benchmark(config, root)

    sentiment = report["formal_combined"]["metrics"]
    direction = report["formal_combined_direction"]["metrics"]
    assert sentiment["sentiment_three_class"]["denominator"] == 1
    assert sentiment["direction_agreement"]["denominator"] == 0
    assert direction["direction_agreement"]["denominator"] == 1
    assert direction["sentiment_three_class"]["denominator"] == 0


def test_the_same_identifier_may_appear_in_both_tracks(tmp_path, monkeypatch):
    """One event can carry a title unit and a trajectory event. That is not double counting."""
    root, config = _mixed_config(tmp_path, monkeypatch, same_identity=True)

    report = bench.run_benchmark(config, root)

    assert report["identity_overlap_between_cohorts"] == []
    assert report["track_denominators"]["sentiment"]["denominator"] == 1
    assert report["track_denominators"]["direction"]["denominator"] == 1


def test_two_cohorts_on_one_track_may_not_share_an_identity(tmp_path, monkeypatch):
    root, config = _mixed_config(tmp_path, monkeypatch)
    duplicate = dict(config["cohorts"][0])
    duplicate["cohort_id"] = "second-sentiment-cohort"
    config["cohorts"].append(duplicate)

    with pytest.raises(bench.BenchmarkInvariantError, match="same track"):
        bench.run_benchmark(config, root)


@pytest.mark.parametrize("missing", ["event_frame", "event_root"])
def test_a_direction_cohort_needs_its_frame_and_root(missing):
    payload = _config(["u1"])
    cohort = _trajectory_cohort(["evt-1"])
    cohort["track"] = "direction"
    del cohort[missing]
    payload["cohorts"] = [cohort]

    with pytest.raises(bench.BenchmarkConfigError, match=missing):
        bench.validate_config(payload)


def test_an_unknown_track_is_refused():
    payload = _config(["u1"])
    payload["cohorts"][0]["track"] = "vibes"

    with pytest.raises(bench.BenchmarkConfigError, match="track"):
        bench.validate_config(payload)


def test_a_sentiment_cohort_without_a_track_field_still_defaults_to_sentiment():
    payload = _config(["u1"])

    bench.validate_config(payload)

    assert bench.cohort_track(payload["cohorts"][0]) == "sentiment"


def _direction_submissions_disagreeing(root: Path, event_id: str, *,
                                       label_a: str, label_b: str) -> None:
    _write(root, "direction/pass-a.json",
           {"annotator_id": "leo", "prediction_exposure": False,
            "saw_other_annotator_answer": False,
            "units": [{"unit_id": event_id, "direction_label": label_a}]})
    _write(root, "direction/pass-b.json",
           {"annotator_id": "dick", "prediction_exposure": False,
            "saw_other_annotator_answer": False,
            "records": [{"unit_id": event_id, "direction_label": label_b}]})


def _direction_adjudication(root: Path, event_id: str, *, label: str = "escalation",
                            decided_by: str = "project owner",
                            preserved: bool = True) -> None:
    _write(root, "direction/rule.json", {"rule": "the frozen direction rule"})
    _write(root, "direction/adjudication.json", {
        "schema": "riskshield.data_evaluation.adjudication_record.v1",
        "decided_by": decided_by, "decided_at": "2026-10-12T00:00:00Z",
        "rule_source": "direction/rule.json",
        "records": [{"unit_id": event_id, "resolved_direction_label": label,
                     "both_original_answers_preserved": preserved,
                     "resolution": "the two passes were reconciled against the observed series",
                     "rule_arm": "frozen_direction_rule"}]})


def test_direction_disagreement_without_adjudication_stays_out(tmp_path):
    root = _trajectory_root(tmp_path, ["evt-1"])
    _direction_submissions_disagreeing(root, "evt-1", label_a="escalation", label_b="calming")
    cohort = _trajectory_cohort([], expected_excluded_unit_ids=["evt-1"])

    result = bench.evaluate_cohort(cohort, bench.trajectory_units(root, cohort),
                                   root=root, cutoffs_document=None,
                                   out_dir_relative="out/run")

    assert result["gated_unit_ids"] == []
    assert result["metrics"]["direction_agreement"]["denominator"] == 0
    gate = bench.trajectory_direction_gate(result["units"][0])
    assert gate["reasons"] == ["direction_labels_disagree_no_adjudication",
                               "no_independent_human_direction_label"]
    assert gate["label_basis"] is None


def test_an_adjudicated_direction_enters_on_the_adjudicated_label(tmp_path):
    root = _trajectory_root(tmp_path, ["evt-1"])
    _direction_submissions_disagreeing(root, "evt-1", label_a="escalation", label_b="calming")
    _direction_adjudication(root, "evt-1", label="escalation")
    cohort = _trajectory_cohort(["evt-1"],
                                direction_adjudication_record="direction/adjudication.json")

    result = bench.evaluate_cohort(cohort, bench.trajectory_units(root, cohort),
                                   root=root, cutoffs_document=None,
                                   out_dir_relative="out/run")

    assert result["gated_unit_ids"] == ["evt-1"]
    assert result["metrics"]["direction_agreement"]["denominator"] == 1
    unit = result["units"][0]
    # Both original answers stay on the unit; the adjudication sits beside them.
    assert [row["label"] for row in unit["direction_annotations"]] == ["escalation", "calming"]
    gate = bench.trajectory_direction_gate(unit)
    assert gate["label_basis"] == "adjudicated_disagreement"
    assert gate["resolved_label"] == "escalation"
    assert result["direction_adjudication_report"]["attached_event_ids"] == ["evt-1"]


def test_an_invalid_direction_adjudication_leaves_the_disagreement_excluded(tmp_path):
    root = _trajectory_root(tmp_path, ["evt-1"])
    _direction_submissions_disagreeing(root, "evt-1", label_a="escalation", label_b="calming")
    _direction_adjudication(root, "evt-1", decided_by="")
    cohort = _trajectory_cohort([], expected_excluded_unit_ids=["evt-1"],
                                direction_adjudication_record="direction/adjudication.json")

    result = bench.evaluate_cohort(cohort, bench.trajectory_units(root, cohort),
                                   root=root, cutoffs_document=None,
                                   out_dir_relative="out/run")

    assert result["gated_unit_ids"] == []
    unit = result["units"][0]
    assert unit["direction_adjudication"]["valid"] is False
    assert "missing_adjudicator_id" in unit["direction_adjudication"]["invalid_reasons"]
    assert "direction_labels_disagree_no_adjudication" in bench.trajectory_direction_gate(
        unit)["reasons"]


def test_an_adjudication_to_a_label_the_engine_cannot_express_is_still_excluded(tmp_path):
    """`stable` stays open question 6 even when an adjudicator picks it."""
    root = _trajectory_root(tmp_path, ["evt-1"])
    _direction_submissions_disagreeing(root, "evt-1", label_a="escalation", label_b="calming")
    _direction_adjudication(root, "evt-1", label="stable")
    cohort = _trajectory_cohort([], expected_excluded_unit_ids=["evt-1"],
                                direction_adjudication_record="direction/adjudication.json")

    result = bench.evaluate_cohort(cohort, bench.trajectory_units(root, cohort),
                                   root=root, cutoffs_document=None,
                                   out_dir_relative="out/run")

    assert result["gated_unit_ids"] == []
    assert bench.trajectory_direction_gate(result["units"][0])["reasons"] == [
        "human_direction_label_outside_the_engine_label_space"]


def test_two_agreeing_direction_labels_need_no_adjudication(tmp_path):
    root = _trajectory_root(tmp_path, ["evt-1"])
    _direction_submissions(root, ["evt-1"], {"evt-1": "calming"})
    cohort = _trajectory_cohort(["evt-1"])

    result = bench.evaluate_cohort(cohort, bench.trajectory_units(root, cohort),
                                   root=root, cutoffs_document=None,
                                   out_dir_relative="out/run")

    gate = bench.trajectory_direction_gate(result["units"][0])
    assert result["gated_unit_ids"] == ["evt-1"]
    assert gate["label_basis"] == "two_independent_agreeing_labels"
    assert result["direction_adjudication_report"]["recorded"] is False


def test_a_direction_adjudication_record_is_validated_before_it_is_trusted(tmp_path):
    root = _trajectory_root(tmp_path, ["evt-1"])
    unit = bench.trajectory_units(root, _trajectory_cohort(["evt-1"]))[0]
    _write(root, "direction/not-an-adjudication.json", {"schema": "something else"})

    with pytest.raises(bench.BenchmarkConfigError, match="not an adjudication record"):
        bench.attach_direction_adjudications([unit], "direction/not-an-adjudication.json", root)


def test_metrics_are_reported_for_each_declared_split(tmp_path):
    """The development set and the final evaluation set must be readable apart.

    A split that is only recorded per unit, and never scored per split, cannot
    show that anything was reserved -- which is the whole point of reserving it.
    """
    unit_ids = ["exp_u1", "exp_u2", "exp_u3", "exp_u4"]
    root = _fixture_root(tmp_path, unit_ids)
    _expansion_documents(root, unit_ids)
    _permission_record(root, unit_ids)
    units = bench.load_expansion_units(root, _expansion_cohort(unit_ids))
    for unit in units:
        unit["_root"] = str(root)
    _write_predictions(root, units)

    report = bench.run_benchmark(_expansion_config(unit_ids), root)

    by_split = report["formal_combined"]["metrics_by_split"]
    assert set(by_split) == {"development_set", "final_evaluation_set"}
    for block in by_split.values():
        assert block["metrics"]["sentiment_three_class"]["denominator"] == len(block["unit_ids"])
    total = sum(len(block["unit_ids"]) for block in by_split.values())
    assert total == report["formal_combined"]["metrics"]["sentiment_three_class"]["denominator"]
    assert "not a substitute for the split that was reserved" in (
        report["formal_combined"]["metrics_by_split_note"])


def test_a_direction_cohort_reports_its_splits_separately(tmp_path):
    root = _fixture_root(tmp_path, [])
    _trajectory_root(tmp_path, ["evt-1", "evt-2"])
    _direction_submissions(root, ["evt-1", "evt-2"],
                           {"evt-1": "escalation", "evt-2": "calming"})
    payload = _config(["u1"])
    payload["cohorts"] = [_trajectory_cohort(["evt-1", "evt-2"])]

    report = bench.run_benchmark(payload, root)

    by_split = report["formal_combined_direction"]["metrics_by_split"]
    assert set(by_split) == {"holdout"}
    assert by_split["holdout"]["metrics"]["direction_agreement"]["denominator"] == 2


def test_the_frozen_units_are_reported_as_unassigned_rather_than_given_a_split(tmp_path, monkeypatch):
    """The frozen set predates the split rule, and inventing one now would be post hoc.

    Applying the hash rule retroactively would partition material that has already
    been scored, and a later claim about a reserved split would then rest on a
    boundary drawn after the fact. It is reported as unassigned instead.
    """
    root = _fixture_root(tmp_path, ["u1"])
    _permission_record(root, ["u1"])
    unit = _fixture_unit(root, 0, "u1")
    _write_predictions(root, [unit])
    _install_units(monkeypatch, [unit])

    report = bench.run_benchmark(_config(["u1"]), root)

    by_split = report["formal_combined"]["metrics_by_split"]
    assert list(by_split) == ["unassigned"]
    assert by_split["unassigned"]["unit_ids"] == ["u1"]


# --------------------------------------------------------------------------
# group 7: the command line contract
# --------------------------------------------------------------------------

def test_cli_writes_the_report_and_exits_zero(tmp_path, monkeypatch, capsys):
    root, config, _ = _scored_run(tmp_path, monkeypatch, ["u1"])
    config_path = _write(root, "config.json", config)

    code = bench.main(["--config", str(config_path), "--material-root", str(root)])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["scored_units"] == 1
    assert (root / "out/run/benchmark-report-v1.json").is_file()
    assert (root / "out/run/eligibility-fixture-cohort.json").is_file()


def test_cli_flags_a_frozen_expectation_mismatch(tmp_path, monkeypatch, capsys):
    root, _, _ = _scored_run(tmp_path, monkeypatch, ["u1"])
    config_path = _write(root, "config.json", _config(["u1"], expected=[]))

    code = bench.main(["--config", str(config_path), "--material-root", str(root)])

    assert code == 3
    assert "frozen expectations do not match" in capsys.readouterr().err
