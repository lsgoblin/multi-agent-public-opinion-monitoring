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
    unit = _fixture_unit(root, 0, "u1",
                         cutoff_provenance="derived_backtest_window_rule_not_historical_freeze")

    state = bench.ensure_cutoff_attestation([unit], "frozen/attestation.json", "out/run", root)

    assert state == {"reference": "frozen/attestation.json", "generated": False,
                     "derived_cutoff_unit_ids": ["u1"]}
    assert not (root / "out/run/derived-cutoff-attestation.json").exists()


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
