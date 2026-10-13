"""Ledger-derived production evidence, the honest execution mode, and the CLI entry points.

Offline and deterministic: no model call, no network. Three things are covered that were
previously either asserted by hand or not covered at all:

* ``model_calls`` and the token accounting are re-derived from the run's ledger, and a value
  that cannot be derived raises instead of being written as ``null`` -- a placeholder must not
  be able to pass an admission check;
* the execution mode names what actually produced the prediction, so a real historical run
  driven by an offline substitute is not labelled as a live model run;
* the two CLIs are exercised through ``subprocess`` so their argument handling, their
  material-root rule and their exit codes are actually tested.
"""

import json
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from riskshield.direction_prediction import (LIVE_SIMULATION_MODE, SIMULATION_MODE,
                                             LedgerEvidenceError,
                                             build_documents, ledger_evidence,
                                             verify_import_evidence, write_documents)
from riskshield.evidence import EvidencePipeline
from riskshield.schemas import AgentDecision, CaseImport
from riskshield.simulation import Day3Simulation, DecisionResult
from riskshield.store import Store
from tests.fixtures.day3_synthetic_case import CASE_ID, GRAPH_ID, prepare_synthetic_case

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "data/public/unh_change_20240222_day2_multisource_event.json"


class Substitute:
    mode = "test_substitute"
    reservation_cny = Decimal("0")
    input_cny_per_million = Decimal("0")
    cached_input_cny_per_million = Decimal("0")
    output_cny_per_million = Decimal("0")
    price_version = "evidence-test-v1"
    model = "evidence-test"

    def decide(self, observation):
        reference = observation["evidence"][0]["evidence_id"]
        action = "comment" if observation["messages"] else "observe"
        cited = ([observation["messages"][0]["message_id"]] if observation["messages"]
                 else [reference])
        return DecisionResult(AgentDecision(
            agent_id=observation["agent_id"], action=action, evidence_ids=cited,
            reason="Deterministic offline decision for the evidence tests."),
            usage={"prompt_tokens": 3, "completion_tokens": 2}, model=self.model)


class RealModelSubstitute(Substitute):
    """Reports the real-model backend mode without ever calling one.

    The engine requires a positive price and reservation for that mode, so this carries the
    shape a real run has; the decision is still produced locally, so no request leaves the
    process and the counts remain the run's own.
    """

    mode = "real_model"
    reservation_cny = Decimal("0.05")
    input_cny_per_million = Decimal("1.00")
    cached_input_cny_per_million = Decimal("0.20")
    output_cny_per_million = Decimal("2.00")
    price_version = "evidence-test-real-v1"
    model = "evidence-test-real"

    def describe(self):
        return {"type": "evidence_test_real_model", "model_calls": 0,
                "network_requests": 0, "price_version": self.price_version}


def _fictional(tmp_path, name="ev.db", backend=None, agents=3, rounds=2):
    store = Store(tmp_path / name)
    Day3Simulation(store)
    prepare_synthetic_case(store)
    engine = Day3Simulation(store)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=agents, rounds=rounds,
                               concurrency=2, seed=5)
    engine.advance(run_id, backend or Substitute())
    return store, run_id


def _real_run(tmp_path, *, backend=None, name="real-ev.db"):
    package = json.loads((PROJECT_ROOT / PACKAGE).read_text(encoding="utf-8"))
    store = Store(tmp_path / name)
    EvidencePipeline(store)
    case = CaseImport.model_validate(package["case_import_projection"])
    store.import_case(case)
    from riskshield.tasks import Day4Tasks
    graph = Day4Tasks(store)._build_archived_projection(case.case_id)
    engine = Day3Simulation(store)
    run_id = engine.create_run(case.case_id, graph["graph_id"], agent_count=3, rounds=2,
                               concurrency=2, seed=9)
    engine.advance(run_id, backend or Substitute())
    return store, case, graph, run_id


# ------------------------------------------------- ledger-derived production evidence

def test_counts_and_tokens_are_derived_from_the_ledger(tmp_path):
    store, run_id = _fictional(tmp_path)
    evidence = ledger_evidence(store, run_id)
    # 3 agents x 2 rounds, every decision valid, so every dispatched call is accounted for.
    assert evidence["model_calls"] == 6
    assert evidence["valid_decisions"] == 6
    assert evidence["requests_with_usage"] == 6
    assert evidence["prompt_tokens"] == 18 and evidence["completion_tokens"] == 12
    assert evidence["scale"] == "3x2"
    assert evidence["run_status"] == "complete"
    assert evidence["not_dispatched_requests"] == 0
    assert evidence["provenance"]


def test_the_evidence_agrees_with_the_run_summary_it_describes(tmp_path):
    """The derived numbers must match the ledger's own summary, not a parallel tally."""
    store, run_id = _fictional(tmp_path)
    evidence = ledger_evidence(store, run_id)
    summary = Day3Simulation(store).summary(run_id)
    assert evidence["model_calls"] == sum(
        count for status, count in summary["action_counts"].items()
        if status != "not_dispatched")
    assert evidence["prompt_tokens"] == summary["usage"]["prompt_tokens"]
    assert evidence["completion_tokens"] == summary["usage"]["completion_tokens"]
    assert evidence["cost_cny"] == summary["cost_cny"]


def test_no_count_is_ever_null(tmp_path):
    """A placeholder must not exist: every count is an integer, including a legitimate zero."""
    store, run_id = _fictional(tmp_path)
    evidence = ledger_evidence(store, run_id)
    for field in ("model_calls", "prompt_tokens", "completion_tokens", "valid_decisions",
                  "requests_with_usage", "not_dispatched_requests"):
        assert isinstance(evidence[field], int), field
        assert not isinstance(evidence[field], bool), field


def test_a_disagreeing_token_total_is_refused_rather_than_reported(tmp_path):
    """If the two records disagree, neither may be emitted as the run's accounting."""
    store, run_id = _fictional(tmp_path)
    with store.connect() as db:
        db.execute("UPDATE simulation_runs SET usage=? WHERE run_id=?",
                   (json.dumps({"prompt_tokens": 999, "completion_tokens": 999,
                                "cached_input_tokens": 0, "unknown_usage_calls": 0,
                                "by_model": {}}), run_id))
    with pytest.raises(LedgerEvidenceError, match="token totals disagree"):
        ledger_evidence(store, run_id)


def test_a_by_model_total_that_does_not_add_up_is_refused(tmp_path):
    store, run_id = _fictional(tmp_path)
    with store.connect() as db:
        row = db.execute("SELECT usage FROM simulation_runs WHERE run_id=?",
                         (run_id,)).fetchone()
        usage = json.loads(row["usage"])
        usage["by_model"] = {"wrong-model": dict(calls=2, prompt_tokens=6,
                                                 completion_tokens=4,
                                                 price_version="x", reserved_cny="0",
                                                 cost_cny="0", cached_input_tokens=0,
                                                 unknown_usage_calls=0,
                                                 actual_models={})}
        db.execute("UPDATE simulation_runs SET usage=? WHERE run_id=?",
                   (json.dumps(usage), run_id))
    with pytest.raises(LedgerEvidenceError, match="per-model call counts"):
        ledger_evidence(store, run_id)


def test_a_partial_run_reports_dispatched_and_undispatched_separately(tmp_path):
    """A run that stopped early legitimately holds rows that were never sent."""
    import threading
    store = Store(tmp_path / "partial-ev.db")
    Day3Simulation(store)
    prepare_synthetic_case(store)
    engine = Day3Simulation(store)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=2, rounds=2, concurrency=1,
                               seed=3)
    gate = threading.Event()

    class Blocks(Substitute):
        def __init__(self):
            self.calls = 0

        def decide(self, observation):
            self.calls += 1
            if self.calls >= 2:
                gate.wait()
            return super().decide(observation)

    try:
        engine.advance(run_id, Blocks(), collect_performance=True,
                       call_timeout_seconds=0.2)
    finally:
        gate.set()
    evidence = ledger_evidence(store, run_id)
    assert evidence["run_status"] == "partial"
    assert evidence["model_calls"] >= 1
    assert evidence["not_dispatched_requests"] >= 1
    assert evidence["model_calls"] + evidence["not_dispatched_requests"] == 4


# --------------------------------------------------------------- the honest mode name

def test_a_real_historical_run_with_the_real_model_gets_the_live_mode(tmp_path):
    store, case, graph, run_id = _real_run(tmp_path, backend=RealModelSubstitute())
    built = build_documents(store, run_id, outdir=tmp_path / "b", material_root=tmp_path,
                            execution_mode=LIVE_SIMULATION_MODE)
    case_doc = built["inputs"]["cases"][0]
    assert case_doc["data_mode"] == "real_historical"
    assert case_doc["predictions"]["direction"]["execution_mode"] == LIVE_SIMULATION_MODE
    assert built["summary"]["execution_mode"] == LIVE_SIMULATION_MODE
    # The mode is also what the run's own ledger recorded, not only what the caller said.
    assert json.loads(Day3Simulation(store)._run_row(run_id)["config"])["backend_mode"] == \
        "real_model"


def test_the_live_mode_is_refused_when_the_run_did_not_use_the_real_model(tmp_path):
    """A live label is a claim about production, so it is checked rather than trusted."""
    from riskshield.direction_prediction import DirectionPredictionError
    store, case, graph, run_id = _real_run(tmp_path)  # offline substitute
    with pytest.raises(DirectionPredictionError, match="requires a run that used the real model"):
        build_documents(store, run_id, outdir=tmp_path / "b", material_root=tmp_path,
                        execution_mode=LIVE_SIMULATION_MODE)


def test_the_live_mode_is_refused_for_synthetic_material(tmp_path):
    from riskshield.direction_prediction import DirectionPredictionError
    store, run_id = _fictional(tmp_path, backend=RealModelSubstitute())
    with pytest.raises(DirectionPredictionError, match="requires real historical material"):
        build_documents(store, run_id, outdir=tmp_path / "b", material_root=tmp_path,
                        execution_mode=LIVE_SIMULATION_MODE)


def test_a_real_historical_run_with_an_offline_substitute_is_not_called_live(tmp_path):
    """The mode follows the run's own backend, so an engineering run is not mislabelled."""
    store, case, graph, run_id = _real_run(tmp_path)
    built = build_documents(store, run_id, outdir=tmp_path / "b", material_root=tmp_path)
    case_doc = built["inputs"]["cases"][0]
    assert case_doc["data_mode"] == "real_historical"
    assert case_doc["predictions"]["direction"]["execution_mode"] == SIMULATION_MODE
    assert case_doc["predictions"]["direction"]["execution_mode"] != LIVE_SIMULATION_MODE


def test_a_synthetic_run_never_claims_the_live_mode(tmp_path):
    store, run_id = _fictional(tmp_path, backend=RealModelSubstitute())
    built = build_documents(store, run_id, outdir=tmp_path / "b", material_root=tmp_path)
    assert built["inputs"]["cases"][0]["predictions"]["direction"]["execution_mode"] == \
        SIMULATION_MODE


# ---------------------------------------------------- the admission-shape anti-placeholder

def test_the_import_document_carries_real_counts(tmp_path):
    store, case, graph, run_id = _real_run(tmp_path)
    root = tmp_path / "material"
    outdir = root / "predictions"
    outdir.mkdir(parents=True)
    written = write_documents(store, run_id, outdir, material_root=root)
    check = verify_import_evidence(written["import_document"], root)
    assert check["ok"] is True, check["problems"]
    body = json.loads(written["evidence"].read_text(encoding="utf-8"))
    production = body["production_evidence"]
    assert production["model_calls"] == 6
    assert production["prompt_tokens"] == 18
    assert production["run_status"] == "complete"


def test_a_null_count_would_be_refused(tmp_path):
    """The point of the check: the placeholder that v15 emitted cannot pass."""
    store, case, graph, run_id = _real_run(tmp_path)
    root = tmp_path / "material"
    outdir = root / "predictions"
    outdir.mkdir(parents=True)
    written = write_documents(store, run_id, outdir, material_root=root)
    document = json.loads(json.dumps(written["import_document"]))
    evidence_path = written["evidence"]
    body = json.loads(evidence_path.read_text(encoding="utf-8"))
    body["production_evidence"]["model_calls"] = None
    evidence_path.write_text(json.dumps(body), encoding="utf-8")
    check = verify_import_evidence(document, root)
    assert check["ok"] is False
    assert any(row["reason"] == "production_evidence_count_not_an_integer"
               for row in check["problems"])


def test_a_missing_count_field_would_be_refused(tmp_path):
    store, case, graph, run_id = _real_run(tmp_path)
    root = tmp_path / "material"
    outdir = root / "predictions"
    outdir.mkdir(parents=True)
    written = write_documents(store, run_id, outdir, material_root=root)
    document = json.loads(json.dumps(written["import_document"]))
    body = json.loads(written["evidence"].read_text(encoding="utf-8"))
    del body["production_evidence"]["prompt_tokens"]
    written["evidence"].write_text(json.dumps(body), encoding="utf-8")
    check = verify_import_evidence(document, root)
    assert check["ok"] is False
    assert any(row["reason"] == "production_evidence_field_missing"
               and row.get("field") == "prompt_tokens" for row in check["problems"])


# ------------------------------------------------------------------------- the CLIs

def _run_cli(script, *arguments, cwd=None):
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / script), *arguments],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(cwd or PROJECT_ROOT))


@pytest.fixture
def cli_root(tmp_path_factory):
    """A working directory outside every temp root.

    The live entry point refuses a ledger path inside a temp root -- that guard is the fix for
    the run whose 15,000-row ledger was left in a scratch directory -- and pytest's own
    ``tmp_path`` lives under the temp root, so the CLI tests need a directory that is not.
    """
    import shutil
    root = (PROJECT_ROOT / "runtime" / f"cli-test-{uuid.uuid4().hex[:8]}").resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _real_cli_store(tmp_path, root):
    """A store under the material root, plus one completed real historical run."""
    return _real_run(tmp_path, name="cli-ev.db")


def test_the_export_cli_writes_a_document_and_reports_its_checks(tmp_path):
    store, case, graph, run_id = _real_cli_store(tmp_path, tmp_path)
    root = tmp_path / "material"
    outdir = root / "predictions"
    outdir.mkdir(parents=True)
    store_path = tmp_path / "cli-ev.db"
    result = _run_cli("export_direction_prediction.py", "--db", str(store_path),
                      "--run-id", run_id, "--outdir", str(outdir),
                      "--material-root", str(root), "--unit-kind", "original_post")
    assert result.returncode == 0, result.stderr
    assert "reference check   : ok" in result.stdout
    assert "event uniqueness  : ok" in result.stdout
    assert (outdir / "simulation-prediction-import-v1.json").is_file()


def test_the_export_cli_refuses_an_output_directory_outside_the_material_root(tmp_path):
    store, case, graph, run_id = _real_cli_store(tmp_path, tmp_path)
    root = tmp_path / "material"
    root.mkdir()
    result = _run_cli("export_direction_prediction.py",
                      "--db", str(tmp_path / "cli-ev.db"), "--run-id", run_id,
                      "--outdir", str(tmp_path / "elsewhere"),
                      "--material-root", str(root))
    assert result.returncode == 1
    assert "outside the material root" in (result.stderr + result.stdout)


def test_the_export_cli_refuses_to_overwrite_a_frozen_document(tmp_path):
    store, case, graph, run_id = _real_cli_store(tmp_path, tmp_path)
    root = tmp_path / "material"
    outdir = root / "predictions"
    outdir.mkdir(parents=True)
    arguments = ("--db", str(tmp_path / "cli-ev.db"), "--run-id", run_id,
                 "--outdir", str(outdir), "--material-root", str(root),
                 "--unit-kind", "original_post")
    assert _run_cli("export_direction_prediction.py", *arguments).returncode == 0
    second = _run_cli("export_direction_prediction.py", *arguments)
    assert second.returncode == 1
    assert "refusing to overwrite" in (second.stderr + second.stdout)


def test_the_verify_cli_passes_from_another_working_directory(tmp_path):
    store, case, graph, run_id = _real_cli_store(tmp_path, tmp_path)
    root = tmp_path / "material"
    outdir = root / "predictions"
    outdir.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = _run_cli("verify_direction_prediction.py",
                      "--db", str(tmp_path / "cli-ev.db"), "--run-id", run_id,
                      "--outdir", str(outdir), "--material-root", str(root),
                      "--unit-kind", "original_post", cwd=elsewhere)
    assert result.returncode == 0, result.stderr
    assert "import reference check: ok" in result.stdout
    assert "event uniqueness  : ok" in result.stdout
    assert "direction numerator/denominator: 0/1" in result.stdout


def test_the_verify_cli_succeeds_under_a_non_ascii_material_root(tmp_path):
    root = tmp_path / "材料 根 目录"
    outdir = root / "predictions"
    outdir.mkdir(parents=True)
    store, case, graph, run_id = _real_cli_store(tmp_path, root)
    result = _run_cli("verify_direction_prediction.py",
                      "--db", str(tmp_path / "cli-ev.db"), "--run-id", run_id,
                      "--outdir", str(outdir), "--material-root", str(root),
                      "--unit-kind", "original_post")
    assert result.returncode == 0, result.stderr
    record = json.loads((outdir / "simulation-prediction-inputs-v1.json")
                        .read_text(encoding="utf-8"))
    # Paths inside the document stay relative, so they carry no absolute machine path.
    assert not record["cases"][0]["cutoff_evidence_ref"]["path"].startswith(("/", "C:", "D:"))


def test_the_live_entry_refuses_a_case_that_is_not_real_historical(cli_root):
    """The entry point must not run real-model work over a synthetic fixture by accident."""
    store = Store(cli_root / "entry.db")
    Day3Simulation(store)
    prepare_synthetic_case(store)
    result = _run_cli("run_live_tier.py", "--agents", "2", "--rounds", "1",
                      "--concurrency", "1", "--max-calls", "0",
                      "--out", str(cli_root / "out.json"),
                      "--db", str(cli_root / "entry.db"),
                      "--case-id", CASE_ID, "--dry-run",
                      "--env", str(cli_root / "missing.env"))
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "runs real historical material only" in combined, combined


def test_the_live_entry_refuses_a_missing_case(cli_root):
    result = _run_cli("run_live_tier.py", "--agents", "2", "--rounds", "1",
                      "--concurrency", "1", "--max-calls", "0",
                      "--out", str(cli_root / "out.json"),
                      "--db", str(cli_root / "nothing.db"),
                      "--case-id", "no_such_case", "--dry-run")
    assert result.returncode != 0
    assert "not in this store" in (result.stdout + result.stderr)


# ---------------------------------------------- the cutoff boundary, compared as time

def _live_tier_module():
    import importlib.util
    path = PROJECT_ROOT / "scripts" / "run_live_tier.py"
    spec = importlib.util.spec_from_file_location("live_tier_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_same_instant_written_two_ways_is_not_a_boundary_violation():
    """`Z` and `+00:00` are the same instant; only a string comparison disagrees.

    The graph stores availability as "...Z" while the snapshot cutoff reads back as
    "...+00:00". Comparing those as text made material that is exactly at the cutoff look
    post-cutoff, which is how this was found: the pilot was refused for material a few
    milliseconds inside its own boundary.
    """
    module = _live_tier_module()
    cutoff, problem = module.parse_instant("2023-07-25T15:53:54+00:00", what="cutoff")
    assert problem is None
    at_boundary, problem = module.parse_instant("2023-07-25T15:53:54Z", what="claim")
    assert problem is None
    assert at_boundary == cutoff
    assert not at_boundary > cutoff
    # Sanity: as raw text the two are different strings, which is why the parse is required.
    assert "2023-07-25T15:53:54Z" != "2023-07-25T15:53:54+00:00"


def test_material_genuinely_after_the_cutoff_is_still_refused():
    module = _live_tier_module()
    cutoff, _ = module.parse_instant("2023-07-25T15:53:54Z", what="cutoff")
    after, problem = module.parse_instant("2023-07-25T15:54:10Z", what="claim")
    assert problem is None
    assert after > cutoff


def test_a_time_without_a_timezone_is_refused_rather_than_compared():
    module = _live_tier_module()
    instant, problem = module.parse_instant("2023-07-25T15:53:54", what="cutoff")
    assert instant is None
    assert "no timezone" in problem
    for bad in (None, "", "not-a-time"):
        instant, problem = module.parse_instant(bad, what="claim")
        assert instant is None and problem


def test_the_live_entry_dry_run_dispatches_nothing(cli_root):
    """A dry run proves the entry point and the cutoff boundary, and reports zero calls."""
    import threading
    from riskshield.evidence import EvidencePipeline as _Pipeline
    from riskshield.tasks import Day4Tasks
    package = json.loads((PROJECT_ROOT / PACKAGE).read_text(encoding="utf-8"))
    store = Store(cli_root / "real-entry.db")
    _Pipeline(store)
    case = CaseImport.model_validate(package["case_import_projection"])
    store.import_case(case)
    graph = Day4Tasks(store)._build_archived_projection(case.case_id)
    result = _run_cli("run_live_tier.py", "--agents", "3", "--rounds", "2",
                      "--concurrency", "2", "--max-calls", "0",
                      "--out", str(cli_root / "dry.json"),
                      "--db", str(cli_root / "real-entry.db"),
                      "--case-id", case.case_id, "--dry-run")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout[result.stdout.index("{"):])
    assert payload["dry_run"] is True
    assert payload["model_calls"] == 0
    assert payload["data_mode"] == "real_historical"
    assert payload["observations_validated"] == 6
    # Nothing was written: a dry run leaves no prediction document behind.
    assert not (cli_root / "dry.json").exists()
