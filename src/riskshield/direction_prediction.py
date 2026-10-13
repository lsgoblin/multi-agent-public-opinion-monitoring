"""Turn one simulation run into an evaluation-inputs document and a freeze record.

Why this exists
---------------
The scale result (500x30 with the real model) lives in a run ledger, and the evaluation
branch scores predictions from a *separate* inputs document bound to a cutoff. Nothing
connected the two: the simulation had no escalation/calming prediction at all, so a
simulation-derived direction result could not exist. This adapter is that connection and
nothing more. It does not score anything, it does not read labels, and it does not modify
the evaluator, which belongs to the data-evaluation branch.

What it produces
----------------
``inputs``
    A document the existing evaluator accepts (``riskshield.day5.evaluation-inputs.v1``):
    one case per candidate unit, carrying exactly **one** direction prediction for the
    event. The case is built from the run's own cutoff-bound configuration, so the cutoff,
    source version and source records are the ones the run actually used.
``freeze_record``
    The evidence that the prediction was frozen before any label or outcome was read: the
    hashes of the inputs, the method document and the prediction, plus the run binding.
``evidence``
    The concrete values a JSON pointer can resolve to, so every reference the evaluator
    checks -- cutoff, cutoff evidence, source version, source record, availability time and
    visibility, the prediction label -- matches what the case claims.
``summary``
    What was decided, in a form a reviewer can check without re-deriving it.

The separation this keeps
-------------------------
* The method is frozen in ``direction_method.json`` next to this module, and its hash is
  recorded. The rule that produced a prediction cannot be edited afterwards without the
  hash changing.
* Only cutoff-eligible material and the run's own records are read. No post-cutoff series,
  no human label, no evaluation-only input is opened.
* Exactly one direction prediction per event is emitted. The evaluator excludes an event
  whose direction candidates are not exactly one, so emitting two -- for example this
  method's and a baseline's -- would silently remove the event from the denominator.
* A simulation-derived prediction is never labelled as a baseline, and a baseline result
  must never be reported as this method's output.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from riskshield.store import Store

# The evaluator's own schema names, quoted verbatim so a drift is visible in review.
EVALUATION_INPUT_SCHEMA = "riskshield.day5.evaluation-inputs.v1"
METHOD_SCHEMA = "riskshield.simulation_scale.direction-method.v1"
FREEZE_SCHEMA = "riskshield.simulation_scale.direction-freeze.v1"
EVIDENCE_SCHEMA = "riskshield.simulation_scale.prediction-evidence.v1"
PREDICTION_SOURCE = "simulation_derived"

METHOD_PATH = Path(__file__).with_name("direction_method.json")

ESCALATING_ACTIONS = frozenset({"share", "comment", "express_complaint_intent"})
CALMING_ACTIONS = frozenset({"observe", "seek_clarification"})


class DirectionPredictionError(ValueError):
    """The run cannot be turned into a defensible prediction."""


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_method(path: Path | None = None) -> tuple[dict, str]:
    """Load the frozen method document and its hash.

    A method whose parameters are not the frozen ones must not be used silently, so the
    caller always carries the hash into the freeze record.
    """
    target = Path(path) if path is not None else METHOD_PATH
    raw = target.read_bytes()
    method = json.loads(raw.decode("utf-8"))
    if method.get("schema") != METHOD_SCHEMA:
        raise DirectionPredictionError(f"unexpected method schema in {target}")
    if method.get("status") != "frozen_before_any_outcome_access":
        raise DirectionPredictionError("the direction method is not in its frozen state")
    for name in ("escalation_share_threshold", "negative_emotion_threshold"):
        value = method.get("parameters", {}).get(name)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise DirectionPredictionError(f"method parameter {name} is missing or invalid")
    return method, sha256_bytes(raw)


def classify_round(rows: list[dict], *, negative_emotion_threshold: float,
                   agent_state: dict | None = None) -> dict:  # noqa: D401
    """Classify one round's rows into escalating / calming / undecided.

    The rule is deliberately coarse and stated in the frozen method document: an agent is
    escalating when its action amplifies an unverified narrative, and calming when it stays
    with cutoff-eligible evidence. Undecided rows carry no decision and never enter the
    share, so a round that decided nothing yields no label rather than a default one.

    ``agent_state`` carries the run's persisted final local state. It is the final state and
    not the state as of the selected round, because the engine does not persist per-round
    state: a memory row stores the action and its reason, and the emotion update happens
    after that row is written. The method document states this limit, so the prediction is
    never described as resting on a per-round state snapshot.
    """
    state = agent_state or {}
    escalating, calming, undecided = [], [], []
    for row in rows:
        agent_id = row["agent_id"]
        if row["status"] != "valid" or not row["decision"]:
            undecided.append(agent_id)
            continue
        decision = json.loads(row["decision"])
        refs = json.loads(row["observation_refs"]) if row["observation_refs"] else {}
        action = decision.get("action")
        cited = set(decision.get("evidence_ids") or [])
        cites_message = bool(cited & set(refs.get("messages") or []))
        cites_source = bool(cited & set(refs.get("evidence") or []))
        emotion = state.get(agent_id)

        amplifying = (action in ESCALATING_ACTIONS and (cites_message or not cites_source))
        distressed = (isinstance(emotion, (int, float)) and not isinstance(emotion, bool)
                      and emotion <= negative_emotion_threshold)
        grounded = (action in CALMING_ACTIONS and cites_source and not cites_message)
        if amplifying or distressed:
            escalating.append(agent_id)
        elif grounded:
            calming.append(agent_id)
        else:
            # Matches neither stated condition. Counted as undecided rather than folded
            # into either side, because the method does not claim it.
            undecided.append(agent_id)
    return {"escalating": escalating, "calming": calming, "undecided": undecided}


def decide_label(counts: dict, method: dict) -> tuple[str, float]:
    universe = len(counts["escalating"]) + len(counts["calming"])
    if universe < 1:
        raise DirectionPredictionError(
            "the selected round has no classifiable valid decision, so no direction "
            "prediction can be produced from it")
    share = round(len(counts["escalating"]) / universe, 6)
    threshold = method["parameters"]["escalation_share_threshold"]
    comparison = method["parameters"].get("threshold_comparison", ">=")
    if comparison == ">=":
        label = "escalation" if share >= threshold else "calming"
    else:
        label = "escalation" if share > threshold else "calming"
    return label, share


def _evidence_relative(outdir: Path, base: Path) -> str:
    """The path to record for the evidence document, relative to the evaluator's base.

    The evaluator resolves every reference against its own working directory, so this must
    be relative to that directory -- not to the output directory.
    """
    target = (outdir / "simulation-prediction-evidence-v1.json").resolve()
    try:
        return str(target.relative_to(base.resolve())).replace("\\", "/")
    except ValueError:
        raise DirectionPredictionError(
            f"the evidence document {target} is outside the base directory {base}; a "
            f"reference to it could not be resolved by the evaluator") from None


def build_documents(store: Store, run_id: str, *, outdir: Path | None = None,
                    base: Path | None = None,
                    method_path: Path | None = None,
                    dataset_version: str | None = None,
                    predictions_ref_path: str | None = None,
                    unit_kind: str = "original_title",
                    text_source: str = "summary",
                    evidence_relative: str | None = None) -> dict:
    """Build inputs, freeze record, evidence and summary for one run.

    ``predictions_ref_path`` is where the caller will write the inputs document; the freeze
    record stores it so a reviewer can find the document the hash belongs to.
    ``unit_kind`` and ``text_source`` describe the unit being predicted: a case whose
    original text is a title uses ``original_title`` with the record's ``title``, while a
    post-shaped source uses ``original_post`` with its ``summary``.
    ``evidence_relative`` may be given directly, otherwise it is derived from ``outdir``
    relative to ``base`` (the directory the evaluator will run from).
    """
    if unit_kind not in {"original_title", "original_post"}:
        raise DirectionPredictionError(f"unsupported unit_kind {unit_kind!r}")
    if text_source not in {"title", "summary"}:
        raise DirectionPredictionError(f"unsupported text_source {text_source!r}")
    if evidence_relative is None:
        if outdir is None:
            raise DirectionPredictionError(
                "either outdir or evidence_relative is required, because the evaluator "
                "resolves references against its own working directory")
        evidence_relative = _evidence_relative(Path(outdir), Path(base) if base
                                               else Path(outdir).parent)
    method, method_sha = load_method(method_path)
    with store.connect() as db:
        run = db.execute("SELECT * FROM simulation_runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(run_id)
        run = dict(run)
        config = json.loads(run["config"])
        if run["completed_rounds"] < 1:
            raise DirectionPredictionError("the run has no completed round to predict from")
        selected_round = run["completed_rounds"]
        rows = [dict(row) for row in db.execute(
            "SELECT agent_id, status, decision, observation_refs FROM simulation_actions "
            "WHERE run_id=? AND round_number=? ORDER BY agent_id",
            (run_id, selected_round))]
        agent_state = {row["agent_id"]: json.loads(row["state"]).get("emotion")
                       for row in db.execute(
                           "SELECT agent_id, state FROM simulation_agents WHERE run_id=?",
                           (run_id,))}
        rounds = [dict(row) for row in db.execute(
            "SELECT round_number, stats FROM simulation_rounds WHERE run_id=? "
            "ORDER BY round_number", (run_id,))]
    if not rows:
        raise DirectionPredictionError("the selected round has no action rows")

    case_id = run["case_id"]
    snapshot = store.snapshot(case_id)
    if snapshot["cutoff"] != config.get("cutoff"):
        raise DirectionPredictionError("run cutoff does not match the case snapshot")
    if snapshot["version"] != config.get("source_version"):
        raise DirectionPredictionError("run source version does not match the case snapshot")
    records = snapshot["records"]
    if not records:
        raise DirectionPredictionError("the case has no cutoff-eligible source record")

    counts = classify_round(rows, negative_emotion_threshold=method["parameters"][
        "negative_emotion_threshold"], agent_state=agent_state)
    label, share = decide_label(counts, method)
    undecided_rounds = sum(1 for row in rounds
                           if json.loads(row["stats"]).get("round_abandoned"))

    # The unit is the case's own primary source record: one event, one unit, one prediction.
    target = sorted(records, key=lambda record: record["record_id"])[0]
    event_id = case_id
    unit_id = target["record_id"]
    text_sha256 = sha256_bytes(target[text_source].encode("utf-8"))

    evidence = {
        "schema": EVIDENCE_SCHEMA,
        "run_id": run_id,
        "event_id": event_id,
        "unit_id": unit_id,
        "cutoff": snapshot["cutoff"],
        "source_version": snapshot["version"],
        "source_record_id": unit_id,
        "source_record_available_at": target["available_at"],
        # The prediction and, separately, the simulation's own direction value. The
        # evaluator checks a prediction's evidence against the *predicted* label, so the
        # prediction reference points here; an observed outcome must point at a different
        # value, which is why the label documents below name their own document.
        "observed_direction": label,
        "text_sha256": text_sha256,
        "note": ("Values the evaluation inputs and labels point at. The direction value here "
                 "is the simulation-derived prediction, not an observed outcome; it is "
                 "published under this path so a reviewer can resolve every reference."),
    }

    def ref(pointer: str) -> dict:
        return {"path": evidence_relative, "pointer": pointer}

    sources = []
    for record in records:
        sources.append({
            "record_id": record["record_id"],
            "source_version": snapshot["version"],
            "available_at": record["available_at"],
            # Each reference points at a scalar value the case claims, so a mismatch is a
            # real mismatch rather than a shape difference.
            "source_record_ref": ref(f"/source_record_id/{record['record_id']}"),
            "source_version_ref": ref("/source_version"),
            "source_available_at_ref": ref(
                f"/source_available_at/{record['record_id']}"),
            "visibility_evidence_ref": ref(
                f"/source_available_at/{record['record_id']}"),
        })
    evidence["source_records"] = {record["record_id"]: record for record in records}
    evidence["source_record_id"] = {record["record_id"]: record["record_id"]
                                    for record in records}
    evidence["source_available_at"] = {record["record_id"]: record["available_at"]
                                       for record in records}

    case = {
        "evaluation_id": f"{event_id}__{unit_id}",
        "event_id": event_id,
        "unit_id": unit_id,
        "unit_kind": unit_kind,
        "data_mode": snapshot["data_mode"],
        "source_version": snapshot["version"],
        "cutoff": snapshot["cutoff"],
        "cutoff_evidence_ref": ref("/cutoff"),
        "target_record_id": unit_id,
        "text_sha256": text_sha256,
        "title_sha256_ref": ref("/text_sha256"),
        "sources": sources,
        "predictions": {
            "direction": {
                "execution_mode": "multi_agent_simulation",
                "label": label,
                "method_version": method["method_version"],
                "prediction_source": PREDICTION_SOURCE,
                "run_id": run_id,
                "result_evidence_refs": [ref("/observed_direction")],
            }
        },
    }
    inputs = {
        "schema": EVALUATION_INPUT_SCHEMA,
        "dataset_version": dataset_version or f"simulation-scale-{run_id}",
        "cases": [case],
    }
    inputs_raw = json.dumps(inputs, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")).encode("utf-8")
    freeze_record = {
        "schema": FREEZE_SCHEMA,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "case_id": case_id,
        "graph_id": run["graph_id"],
        "event_id": event_id,
        "unit_id": unit_id,
        "prediction_raw": {"direction": label},
        "prediction_source": PREDICTION_SOURCE,
        "method_version": method["method_version"],
        "method_document_sha256": method_sha,
        "inputs_sha256": sha256_bytes(inputs_raw),
        "inputs_ref": predictions_ref_path,
        "evidence_ref": evidence_relative,
        "cutoff": snapshot["cutoff"],
        "source_version": snapshot["version"],
        "run_config": {key: config.get(key) for key in
                       ("agent_count", "rounds", "seed", "concurrency", "cutoff",
                        "source_version", "data_mode")},
        "outcomes_accessed": False,
        "labels_accessed": False,
        "cutoff_evidence_only": True,
        "direction_predictions_for_this_event": 1,
        "declaration": ("Frozen before any outcome or label was read. Only cutoff-eligible "
                        "material and this run's own records were used."),
    }
    return {
        "inputs": inputs,
        "evidence": evidence,
        "freeze_record": freeze_record,
        "summary": {
            "run_id": run_id,
            "case_id": case_id,
            "selected_round": selected_round,
            "recorded_rounds": len(rounds),
            "abandoned_rounds": undecided_rounds,
            "valid_decisions_in_selected_round": len(counts["escalating"]) +
            len(counts["calming"]) + len(counts["undecided"]),
            "escalating_agents": len(counts["escalating"]),
            "calming_agents": len(counts["calming"]),
            "undecided_agents": len(counts["undecided"]),
            "escalation_share": share,
            "threshold": method["parameters"]["escalation_share_threshold"],
            "label": label,
            "method_version": method["method_version"],
            "prediction_source": PREDICTION_SOURCE,
            "direction_predictions_for_this_event": 1,
            "cases": 1,
        },
    }


def write_documents(store: Store, run_id: str, outdir: Path, *,
                    method_path: Path | None = None,
                    dataset_version: str | None = None,
                    unit_kind: str = "original_title",
                    text_source: str = "summary",
                    base: Path | None = None) -> dict:
    """Write inputs, evidence and the freeze record under ``outdir``.

    ``base`` is the directory the evaluator will be run from; recorded evidence paths are
    relative to it. It defaults to the output directory's parent, which is the arrangement
    the offline checks use, and the caller states it explicitly when it differs.

    The inputs document is written once and named explicitly: an existing one is never
    overwritten, because a prediction document that has already been frozen must not change
    underneath its recorded hash.
    """
    outdir = Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    base = Path(base).resolve() if base is not None else outdir.parent
    inputs_target = outdir / "simulation-prediction-inputs-v1.json"
    if inputs_target.exists():
        raise DirectionPredictionError(
            f"{inputs_target} already exists; refusing to overwrite a frozen prediction "
            f"document (move it or choose another output directory)")
    evidence_target = outdir / "simulation-prediction-evidence-v1.json"
    built = build_documents(store, run_id, outdir=outdir, base=base,
                           method_path=method_path,
                           dataset_version=dataset_version,
                           predictions_ref_path=str(inputs_target),
                           unit_kind=unit_kind, text_source=text_source)
    inputs_target.write_text(json.dumps(built["inputs"], ensure_ascii=False, indent=1),
                             encoding="utf-8")
    evidence_target.write_text(json.dumps(built["evidence"], ensure_ascii=False, indent=1),
                               encoding="utf-8")
    # The recorded hash must cover the artifact exactly as written. Hashing a differently
    # serialised copy would let a reviewer recompute a different value from the same file.
    built["freeze_record"]["inputs_sha256"] = sha256_bytes(inputs_target.read_bytes())
    built["freeze_record"]["evidence_sha256"] = sha256_bytes(evidence_target.read_bytes())
    (outdir / "simulation-prediction-freeze-v1.json").write_text(
        json.dumps(built["freeze_record"], ensure_ascii=False, indent=1), encoding="utf-8")
    (outdir / "simulation-prediction-summary-v1.json").write_text(
        json.dumps(built["summary"], ensure_ascii=False, indent=1), encoding="utf-8")
    return {"inputs": inputs_target, "evidence": evidence_target,
            "summary": built["summary"]}
