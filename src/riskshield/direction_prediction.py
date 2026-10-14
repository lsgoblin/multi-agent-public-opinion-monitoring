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
# The data-evaluation branch's formal admission point uses this schema. It is named here so
# the mapping is explicit; whether a multi-agent simulation mode is admitted is that
# branch's policy decision, and this adapter does not pretend otherwise by renaming a mode.
PREDICTION_IMPORT_SCHEMA = "riskshield.data_evaluation.prediction_import.v1"
IMPORT_FREEZE_SCHEMA = "riskshield.data_evaluation.freeze_record.v1"
LEDGER_EVIDENCE_SCHEMA = "riskshield.simulation_scale.ledger_evidence.v1"
# Execution mode for a run of the real model over real historical material. The data branch
# is expected to admit this name and to keep refusing the generic simulation mode; until that
# policy is frozen, documents are produced with the mode the run actually used and are not
# presented as admitted.
LIVE_SIMULATION_MODE = "live_multi_agent_simulation_v1"
SIMULATION_MODE = "multi_agent_simulation"
PREDICTION_SOURCE = "simulation_derived"
FORMAL_QUALIFICATION = "formal_candidate"
TENTATIVE_QUALIFICATION = "tentative_partial_run"

METHOD_PATH = Path(__file__).with_name("direction_method.json")

ESCALATING_ACTIONS = frozenset({"share", "comment", "express_complaint_intent"})
CALMING_ACTIONS = frozenset({"observe", "seek_clarification"})


class DirectionPredictionError(ValueError):
    """The run cannot be turned into a defensible prediction."""


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def preflight_event_level_uniqueness(predictions: list[dict], *,
                                     formal: bool = False,
                                     declared_events: dict | None = None,
                                     allow_tentative: bool = False) -> dict:
    """Decide, before any formal scoring, whether each event has exactly one prediction.

    Two distinct questions are answered, because they have different answers:

    * ``formal=False`` checks one source on its own. Each source is a separate report, so a
      source is expected to hold exactly one direction prediction per event it covers. This
      is what a per-source review runs.
    * ``formal=True`` checks the set that would actually go to one formal evaluation. The
      requirement there is **exactly one prediction per event in total**, so a simulation
      prediction and a baseline prediction for the same event do not coexist: they collide,
      and both are named. That is the check that keeps a baseline from being scored as a
      simulation result, or the two from being pooled.

    ``declared_events`` maps each source label to the events it claims to cover, so a missing
    or undeclared prediction is a refusal rather than an omission.
    """
    if not isinstance(predictions, list):
        raise DirectionPredictionError("predictions must be a list of entries")
    formal = formal or bool(declared_events)

    by_event: dict[str, list[dict]] = {}
    for entry in predictions:
        event_id = entry.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            return {"ok": False, "checked": False, "formal": formal,
                    "reason": "a prediction carries no event_id",
                    "collisions": [], "refusals": [entry]}
        by_event.setdefault(event_id, []).append(entry)

    collisions, refusals = [], []
    for event_id, entries in sorted(by_event.items()):
        # Formal: one prediction for the event in total. Per source: one per source, so two
        # sources covering the same event is expected and not a collision.
        if formal:
            grouped = {event_id: entries}
        else:
            grouped = {}
            for entry in entries:
                grouped.setdefault(entry.get("source_label") or "unspecified", []).append(entry)
        for key, group in sorted(grouped.items()):
            if len(group) > 1:
                collisions.append({
                    "event_id": event_id,
                    "source_label": key if not formal else None,
                    "count": len(group),
                    "reason": "more_than_one_direction_prediction_for_one_event",
                    "entries": [{"unit_id": e.get("unit_id"), "run_id": e.get("run_id"),
                                 "label": e.get("label"),
                                 "source_label": e.get("source_label"),
                                 "prediction_source": e.get("prediction_source"),
                                 "qualification": e.get("qualification")}
                                for e in group],
                })
        for entry in entries:
            if (entry.get("qualification") == TENTATIVE_QUALIFICATION
                    and not allow_tentative):
                refusals.append({
                    "event_id": event_id,
                    "source_label": entry.get("source_label"),
                    "unit_id": entry.get("unit_id"), "run_id": entry.get("run_id"),
                    "reason": "tentative_partial_run_not_admitted_for_a_formal_prediction"})

    checked = True
    if declared_events:
        for source, event_ids in sorted(declared_events.items()):
            for event_id in sorted(event_ids):
                covering = [entry for entry in by_event.get(event_id, [])
                            if (entry.get("source_label") or "unspecified") == source]
                if not covering:
                    refusals.append({"event_id": event_id, "source_label": source,
                                     "reason": "no_direction_prediction"})
                    checked = False
        for event_id, entries in sorted(by_event.items()):
            for entry in entries:
                source = entry.get("source_label") or "unspecified"
                if event_id not in declared_events.get(source, set()):
                    refusals.append({"event_id": event_id, "source_label": source,
                                     "reason": "event_not_declared_for_source"})
                    checked = False

    return {
        "ok": not collisions and not refusals and checked,
        "checked": checked,
        "formal": formal,
        "events_covered": len(by_event),
        "predictions_checked": len(predictions),
        "sources": sorted({e.get("source_label") or "unspecified" for e in predictions}),
        "collisions": collisions,
        "refusals": refusals,
        "note": ("a per-source check expects one prediction per event inside each source; a "
                 "formal check expects exactly one for the event in total, so a simulation "
                 "prediction and a baseline for one event collide instead of being pooled"),
    }


def preflight_import_references(document: dict, material_root: Path) -> dict:
    """Check every reference the import document makes, before a formal admission attempt.

    A reference that escapes the material root or does not resolve is reported here, so the
    failure is attributable to this adapter rather than surfacing later as an unexplained
    rejection reason from the other branch's reader.
    """
    root = resolve_material_root(material_root)
    problems = []
    predictions = document.get("predictions") or {}
    entries = (predictions.values() if isinstance(predictions, dict) else predictions)
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append({"unit_id": None, "reference": "prediction_row",
                             "reason": "prediction_row_not_an_object"})
            continue
        refs = [("input_package_ref", (entry.get("input_binding") or {}).get(
            "input_package_ref")),
            ("production_evidence_ref", entry.get("production_evidence_ref")),
            ("freeze_record_ref", entry.get("freeze_record_ref"))]
        for name, ref in refs:
            path = (ref or {}).get("path") if isinstance(ref, dict) else None
            if not isinstance(path, str) or not path.strip():
                problems.append({"unit_id": entry.get("unit_id"), "reference": name,
                                 "reason": "reference_missing_path"})
                continue
            target = (root / path)
            try:
                resolved = target.resolve()
                resolved.relative_to(root)
            except (OSError, ValueError):
                problems.append({"unit_id": entry.get("unit_id"), "reference": name,
                                 "reason": "reference_escapes_material_root",
                                 "path": path})
                continue
            if not resolved.is_file():
                problems.append({"unit_id": entry.get("unit_id"), "reference": name,
                                 "reason": "reference_file_missing", "path": path})
    return {"ok": not problems, "material_root": str(root), "problems": problems}


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


def resolve_material_root(raw: str | Path) -> Path:
    """Validate the declared material root.

    Every reference this adapter writes is resolved against one declared directory. The root
    is supplied by the caller rather than inferred from the process working directory, so a
    document produced under one CWD cannot silently mean something different under another.
    """
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise DirectionPredictionError(
            f"material root must be an absolute path, got {str(raw)!r}")
    root = root.resolve()
    if not root.is_dir():
        raise DirectionPredictionError(f"material root is not a directory: {root}")
    return root


def material_relative(path: Path, root: Path, *, what: str) -> str:
    """Express ``path`` relative to the material root, refusing to escape it.

    A reference that leaves the root cannot be resolved by a reader that is confined to it,
    so this is an error at generation time rather than a broken pointer discovered later.
    """
    target = Path(path).resolve()
    root = Path(root).resolve()
    if target == root or root not in target.parents:
        raise DirectionPredictionError(
            f"{what} {target} is outside the material root {root}; a reference to it would "
            f"escape the root, so the document was not written")
    return str(target.relative_to(root)).replace("\\", "/")


class LedgerEvidenceError(DirectionPredictionError):
    """The run's ledger cannot support a production-evidence claim.

    Raised instead of emitting a placeholder. A ``null`` here would let a structurally
    complete document carry an unverifiable claim, which is exactly what an admission check
    must not be able to accept.
    """


def ledger_evidence(store: Store, run_id: str) -> dict:
    """Derive the production-evidence facts from the run's own ledger.

    Every value here is recomputed from persisted rows, so a reader can re-derive the same
    numbers rather than trusting a restated figure. A value that cannot be derived raises
    rather than being written as null.
    """
    with store.connect() as db:
        run = db.execute("SELECT * FROM simulation_runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(run_id)
        run = dict(run)
        config = json.loads(run["config"])
        usage = json.loads(run["usage"]) if run["usage"] else {}
        actions = db.execute(
            "SELECT COUNT(*) AS rows, "
            "SUM(CASE WHEN status IN ('valid','failed','deadline_exceeded') THEN 1 ELSE 0 END) "
            "  AS dispatched, "
            "SUM(CASE WHEN status='not_dispatched' THEN 1 ELSE 0 END) AS not_dispatched, "
            "SUM(CASE WHEN usage IS NOT NULL THEN 1 ELSE 0 END) AS with_usage, "
            "SUM(COALESCE(json_extract(usage,'$.prompt_tokens'),0)) AS prompt_tokens, "
            "SUM(COALESCE(json_extract(usage,'$.completion_tokens'),0)) AS completion_tokens, "
            "SUM(CASE WHEN status='valid' THEN 1 ELSE 0 END) AS valid_decisions "
            "FROM simulation_actions WHERE run_id=?", (run_id,)).fetchone()
        rounds = db.execute("SELECT COUNT(*) AS n FROM simulation_rounds WHERE run_id=?",
                            (run_id,)).fetchone()["n"]
        invocation = db.execute(
            "SELECT dispatched_requests, valid_decisions, wall_seconds, stop_reason "
            "FROM simulation_performance_calls WHERE run_id=? ORDER BY call_id LIMIT 1",
            (run_id,)).fetchone()
        # Whether any observation referenced a source outside the run's cutoff inputs. The
        # engine refuses such a run, so this is a re-check of a persisted fact, not a promise.
        out_of_scope = 0
        for row in db.execute("SELECT observation_refs FROM simulation_actions "
                              "WHERE run_id=? AND observation_refs IS NOT NULL", (run_id,)):
            refs = json.loads(row["observation_refs"])
            if not refs.get("evidence"):
                out_of_scope += 1

    dispatched = int(actions["dispatched"] or 0)
    not_dispatched = int(actions["not_dispatched"] or 0)
    prompt_tokens = int(actions["prompt_tokens"] or 0)
    completion_tokens = int(actions["completion_tokens"] or 0)
    # A partial run legitimately holds rows that were never sent: the opportunities of an
    # abandoned or refused round. They are counted and reported separately, and only the
    # dispatched rows can carry usage.
    if dispatched + not_dispatched != int(actions["rows"] or 0):
        raise LedgerEvidenceError(
            "the ledger holds rows in an unrecognised status, so the request counts would "
            "not add up")
    if int(actions["with_usage"] or 0) > dispatched:
        raise LedgerEvidenceError(
            "more rows carry usage than were dispatched; the accounting is inconsistent")
    if prompt_tokens != int(usage.get("prompt_tokens", -1)) or \
            completion_tokens != int(usage.get("completion_tokens", -1)):
        raise LedgerEvidenceError(
            "the per-request token totals disagree with the run's usage record, so neither "
            "can be reported as the run's accounting")
    by_model = usage.get("by_model") or {}
    model_calls_by_model = {model: int(entry.get("calls", 0))
                            for model, entry in by_model.items()}
    # Every dispatched call is attributed to a model; a run that dispatched nothing has no
    # per-model counts, which is a fact rather than a gap.
    if dispatched and sum(model_calls_by_model.values()) != dispatched:
        raise LedgerEvidenceError(
            "the per-model call counts do not add up to the dispatched request count, so the "
            "by-model accounting cannot be reported")
    configured = (config.get("agent_count"), config.get("rounds"))
    return {
        "schema": LEDGER_EVIDENCE_SCHEMA,
        "run_id": run_id,
        "case_id": run["case_id"],
        "graph_id": run["graph_id"],
        "run_status": run["status"],
        "model_calls": dispatched,
        "not_dispatched_requests": not_dispatched,
        "model_calls_by_model": model_calls_by_model,
        "requests_with_usage": int(actions["with_usage"] or 0),
        "unknown_usage_calls": int(usage.get("unknown_usage_calls", 0)),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_input_tokens": int(usage.get("cached_input_tokens", 0)),
        "valid_decisions": int(actions["valid_decisions"] or 0),
        "configured_agent_count": configured[0],
        "configured_rounds": configured[1],
        "completed_rounds": int(run["completed_rounds"]),
        "recorded_round_rows": int(rounds),
        "scale": (f"{configured[0]}x{configured[1]}"
                  if configured[0] is not None and configured[1] is not None else None),
        "cost_cny": run["cost_cny"],
        "price_version": next(iter(by_model.values()), {}).get("price_version"),
        "wall_seconds": invocation["wall_seconds"] if invocation else None,
        "stop_reason": invocation["stop_reason"] if invocation else None,
        "rows_without_eligible_evidence": out_of_scope,
        "config_cutoff": config.get("cutoff"),
        "config_source_version": config.get("source_version"),
        "data_mode": config.get("data_mode"),
        "provenance": ("every value above is recomputed from this run's ledger rows "
                       "(simulation_actions, simulation_runs, simulation_rounds, "
                       "simulation_performance_calls); none is restated from another "
                       "document, and a value that could not be derived raises instead of "
                       "being written as null"),
    }


def verify_import_evidence(document: dict, material_root: Path) -> dict:
    """Check that a ``prediction_import.v1`` row carries no unverifiable claim.

    The point is to make a placeholder structurally unable to pass: a null count, a missing
    production-evidence field, or a token total that disagrees with the ledger is a failure
    here, before another branch's admission point ever sees the document.
    """
    root = resolve_material_root(material_root)
    problems: list[dict] = []
    predictions = document.get("predictions") or {}
    entries = list(predictions.values()) if isinstance(predictions, dict) else list(predictions)
    required_counts = ("model_calls", "prompt_tokens", "completion_tokens", "valid_decisions")
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append({"unit_id": None, "reason": "prediction_row_not_an_object"})
            continue
        unit_id = entry.get("unit_id")
        ref = (entry.get("production_evidence_ref") or {})
        path = ref.get("path") if isinstance(ref, dict) else None
        pointer = ref.get("pointer", "") if isinstance(ref, dict) else ""
        if not isinstance(path, str) or not path.strip():
            problems.append({"unit_id": unit_id,
                             "reason": "missing_production_evidence_ref"})
            continue
        target = (root / path)
        try:
            resolved = target.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            problems.append({"unit_id": unit_id,
                             "reason": "reference_escapes_material_root", "path": path})
            continue
        if not resolved.is_file():
            problems.append({"unit_id": unit_id, "reason": "reference_file_missing",
                             "path": path})
            continue
        document_body = json.loads(resolved.read_text(encoding="utf-8"))
        try:
            value = document_body
            for part in [p for p in pointer.split("/") if p]:
                value = value[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, IndexError, TypeError):
            problems.append({"unit_id": unit_id, "reason": "reference_pointer_missing",
                             "path": path, "pointer": pointer})
            continue
        if not isinstance(value, dict):
            problems.append({"unit_id": unit_id, "reason": "production_evidence_not_an_object"})
            continue
        for field in required_counts:
            if field not in value:
                problems.append({"unit_id": unit_id, "field": field,
                                 "reason": "production_evidence_field_missing"})
            elif not isinstance(value[field], int) or isinstance(value[field], bool):
                problems.append({"unit_id": unit_id, "field": field,
                                 "value": value[field],
                                 "reason": "production_evidence_count_not_an_integer"})
        if value.get("run_id") != entry.get("run_id"):
            problems.append({"unit_id": unit_id,
                             "reason": "production_evidence_run_id_mismatch"})
        if value.get("run_status") != "complete":
            problems.append({"unit_id": unit_id, "value": value.get("run_status"),
                             "reason": "production_run_not_complete"})
        # Tokens must be present even for a run that legitimately made no call: the point is
        # that the accounting exists and is checkable, not that it is non-zero.
        if value.get("model_calls") == 0 and value.get("prompt_tokens") != 0:
            problems.append({"unit_id": unit_id,
                             "reason": "zero_calls_with_nonzero_tokens"})
    return {"ok": not problems, "material_root": str(root), "problems": problems}


def _evidence_relative(outdir: Path, material_root: Path) -> str:
    return material_relative(outdir / "simulation-prediction-evidence-v1.json",
                             material_root, what="the evidence document")


def build_documents(store: Store, run_id: str, *, outdir: Path | None = None,
                    material_root: Path,
                    method_path: Path | None = None,
                    dataset_version: str | None = None,
                    predictions_ref_path: str | None = None,
                    unit_kind: str = "original_title",
                    text_source: str = "summary",
                    evidence_relative: str | None = None,
                    execution_mode: str | None = None) -> dict:
    """Build inputs, freeze record, evidence and summary for one run.

    ``material_root`` is required and is the single directory every reference is resolved
    against; it is stated by the caller instead of being taken from the working directory.

    ``predictions_ref_path`` is where the caller will write the inputs document; the freeze
    record stores it so a reviewer can find the document the hash belongs to.
    ``unit_kind`` and ``text_source`` describe the unit being predicted: a case whose
    original text is a title uses ``original_title`` with the record's ``title``, while a
    post-shaped source uses ``original_post`` with its ``summary``.
    ``evidence_relative`` may be given directly, otherwise it is derived from ``outdir``.
    """
    if unit_kind not in {"original_title", "original_post"}:
        raise DirectionPredictionError(f"unsupported unit_kind {unit_kind!r}")
    if text_source not in {"title", "summary"}:
        raise DirectionPredictionError(f"unsupported text_source {text_source!r}")
    material_root = resolve_material_root(material_root)
    if evidence_relative is None:
        if outdir is None:
            raise DirectionPredictionError(
                "either outdir or evidence_relative is required, because every reference "
                "must be expressed relative to the material root")
        evidence_relative = _evidence_relative(Path(outdir), material_root)
    input_package_relative = material_relative(
        Path(outdir) / "simulation-prediction-input-package-v1.json", material_root,
        what="the frozen input package") if outdir is not None else None
    import_freeze_relative = material_relative(
        Path(outdir) / "simulation-prediction-import-freeze-v1.json", material_root,
        what="the import freeze record") if outdir is not None else None
    method, method_sha = load_method(method_path)
    with store.connect() as db:
        run = db.execute("SELECT * FROM simulation_runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(run_id)
        run = dict(run)
        config = json.loads(run["config"])
        if run["completed_rounds"] < 1:
            raise DirectionPredictionError("the run has no completed round to predict from")
        configured_rounds = int(config["rounds"])
        # The prediction reads the run's persisted final state, so it is only a
        # final-state-consistent prediction when the round being predicted is the run's last
        # completed round. The selected round is therefore fixed to that round, and the
        # relation is recorded rather than assumed.
        selected_round = int(run["completed_rounds"])
        run_complete = (run["status"] == "complete"
                        and selected_round == configured_rounds)
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
    # The frozen input package the prediction binds to: exactly the cutoff-eligible snapshot
    # the run was created against. Its canonical digest is what the import contract checks,
    # so it is derived here rather than claimed.
    input_package = {
        "case_id": case_id,
        "cutoff": snapshot["cutoff"],
        "source_version": snapshot["version"],
        "data_mode": snapshot["data_mode"],
        "records": records,
    }
    input_package_sha256 = canonical_digest(input_package)
    evidence["input_package"] = input_package
    # Production evidence is derived from this run's ledger, never restated: a value that
    # cannot be derived raises rather than being written as a placeholder.
    derived = ledger_evidence(store, run_id)
    if derived["config_cutoff"] != snapshot["cutoff"] or \
            derived["config_source_version"] != snapshot["version"]:
        raise LedgerEvidenceError(
            "the run's ledger configuration does not match the case snapshot it claims")
    evidence["production_evidence"] = derived
    # The mode names what actually produced the prediction. A real historical run that did
    # not use the real model is an engineering run, and saying otherwise would misstate it.
    run_config = json.loads(run["config"])
    # ``advance`` persists the backend mode it actually ran with; before that there is none to
    # read, so the caller may state the mode it knows from the run it just drove.
    config_backend = run_config.get("backend_mode")
    if execution_mode is None:
        if snapshot["data_mode"] == "real_historical" and config_backend == "real_model":
            execution_mode = LIVE_SIMULATION_MODE
        else:
            execution_mode = SIMULATION_MODE
    elif execution_mode not in {LIVE_SIMULATION_MODE, SIMULATION_MODE}:
        raise DirectionPredictionError(
            f"unsupported execution mode {execution_mode!r}; a mode must name how the run was "
            f"actually produced")
    elif execution_mode == LIVE_SIMULATION_MODE:
        # A live mode is a claim about how the prediction was produced, so it is checked
        # rather than trusted: real historical material, and a run the engine recorded as
        # having used the real model.
        if snapshot["data_mode"] != "real_historical":
            raise DirectionPredictionError(
                "the live simulation mode requires real historical material")
        if config_backend != "real_model":
            raise DirectionPredictionError(
                f"the live simulation mode requires a run that used the real model, but this "
                f"run recorded backend_mode={config_backend!r}")
    evidence["production_evidence"]["execution_mode"] = execution_mode
    evidence["production_evidence"]["selected_round"] = selected_round
    evidence["production_evidence"]["configured_rounds"] = configured_rounds
    evidence["production_evidence"]["recorded_rounds"] = len(rounds)

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
                "execution_mode": execution_mode,
                "label": label,
                "method_version": method["method_version"],
                "prediction_source": PREDICTION_SOURCE,
                "run_id": run_id,
                "result_evidence_refs": [ref("/observed_direction")],
            }
        },
        # The frozen relation between the prediction and the state it was read from.
        # A formal direction prediction is only admitted for a complete run whose last
        # completed round is the configured last round, so the persisted final state the
        # rule reads really is the state at the end of the predicted round. A partial run is
        # reported as tentative and is refused by the formal preflight.
        "run_completeness": {
            "run_status": run["status"],
            "selected_round": selected_round,
            "configured_rounds": configured_rounds,
            "recorded_rounds": len(rounds),
            "abandoned_rounds": undecided_rounds,
            "state_basis": "run_final_state",
            "state_is_at_selected_round": bool(run_complete),
            "qualification": ("formal_candidate" if run_complete else "tentative_partial_run"),
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
        "run_status": run["status"],
        "selected_round": selected_round,
        "configured_rounds": configured_rounds,
        "state_is_at_selected_round": bool(run_complete),
        "qualification": ("formal_candidate" if run_complete else "tentative_partial_run"),
        "declaration": ("Frozen before any outcome or label was read. Only cutoff-eligible "
                        "material and this run's own records were used."),
    }
    return {
        "inputs": inputs,
        "evidence": evidence,
        "freeze_record": freeze_record,
        "input_package": input_package,
        "input_package_sha256": input_package_sha256,
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
            "unit_id": unit_id,
            "event_id": event_id,
            "cutoff": snapshot["cutoff"],
            "source_version": snapshot["version"],
            "text_sha256": text_sha256,
            "data_mode": snapshot["data_mode"],
            "run_completeness": case["run_completeness"],
            "qualification": case["run_completeness"]["qualification"],
            "material_root": str(material_root),
            "execution_mode": execution_mode,
            "ledger_evidence": {
                "model_calls": evidence["production_evidence"]["model_calls"],
                "prompt_tokens": evidence["production_evidence"]["prompt_tokens"],
                "completion_tokens": evidence["production_evidence"]["completion_tokens"],
                "valid_decisions": evidence["production_evidence"]["valid_decisions"],
                "scale": evidence["production_evidence"]["scale"],
                "run_status": evidence["production_evidence"]["run_status"],
            },
            "evidence_ref": evidence_relative,
            "input_package_ref": input_package_relative,
            "input_package_sha256": input_package_sha256,
            "import_freeze_ref": import_freeze_relative,
        },
        "unit": {
            "unit_id": unit_id,
            "event_id": event_id,
            "cutoff": snapshot["cutoff"],
            "source_version": snapshot["version"],
            "text_sha256": text_sha256,
            "data_mode": snapshot["data_mode"],
        },
    }


def canonical_digest(value) -> str:
    """Digest a JSON value the way the import contract does.

    The import contract digests canonical JSON (sorted keys, compact separators), so a value
    written with indentation still digests to the same string. Computing it any other way
    would make the recorded binding unverifiable by that reader.
    """
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return sha256_bytes(payload.encode("utf-8"))


def build_prediction_import_document(built: dict, *, source_label: str,
                                     material_class: str = "real_frozen_material") -> dict:
    """Shape the built documents as a ``prediction_import.v1`` document.

    This is the structure the data-evaluation branch's admission point expects. It is
    produced so the mapping can be reviewed and tested offline. The execution mode is the
    truthful ``multi_agent_simulation``: choosing a mode string that happens to be on that
    branch's allow-list, when the run was not that kind of run, would be a false claim.
    """
    unit = built["unit"]
    summary = built["summary"]
    return {
        "schema": PREDICTION_IMPORT_SCHEMA,
        "task_type": "event_direction",
        "document_version": summary["method_version"],
        "material_class": material_class,
        "formal_score_eligible": summary["qualification"] == "formal_candidate",
        "policy": ("simulation-derived direction prediction from one frozen run; no human "
                   "labels and no post-cutoff material were read"),
        "predictions": {
            unit["unit_id"]: {
                "unit_id": unit["unit_id"],
                "event_id": unit["event_id"],
                "task_type": "event_direction",
                "label": summary["label"],
                "execution_mode": summary["execution_mode"],
                "method_version": summary["method_version"],
                "run_id": summary["run_id"],
                "cutoff": unit["cutoff"],
                "input_binding": {
                    "input_package_ref": {"path": summary["input_package_ref"],
                                          "pointer": ""},
                    "input_package_sha256": summary["input_package_sha256"],
                    "cutoff": unit["cutoff"],
                    "cutoff_before_label_read": True,
                    "text_sha256": unit["text_sha256"],
                    "source_version": unit["source_version"],
                },
                "production_evidence_ref": {"path": summary["evidence_ref"],
                                            "pointer": "/production_evidence"},
                "freeze_record_ref": {"path": summary["import_freeze_ref"], "pointer": ""},
                "labels_accessed": False,
                "outcomes_accessed": False,
                "prediction_source": summary["prediction_source"],
                "qualification": summary["qualification"],
                "run_completeness": summary["run_completeness"],
            }
        },
    }


def finalize_import_freeze_record(document: dict, *, frozen_inputs) -> dict:
    """Build the freeze record that binds the import document's own digest.

    The digest is computed over the canonical JSON of the document, so the record cannot be
    re-pointed at different predictions afterwards, and a caller does not have to serialize
    the document with a particular indentation for verification to succeed.
    """
    return {
        "schema": IMPORT_FREEZE_SCHEMA,
        "material_class": document.get("material_class"),
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "frozen_by": f"simulation-scale {document.get('document_version')}",
        "prediction_document_sha256": canonical_digest(document),
        "frozen_inputs_sha256": canonical_digest(frozen_inputs),
        "labels_read_before_freeze": False,
        "outcomes_read_before_freeze": False,
        "note": ("Frozen before any outcome or label was read; binds the prediction document "
                 "and the frozen input package it was produced from"),
    }


def write_documents(store: Store, run_id: str, outdir: Path, *,
                    material_root: Path,
                    method_path: Path | None = None,
                    dataset_version: str | None = None,
                    unit_kind: str = "original_title",
                    text_source: str = "summary",
                    source_label: str = PREDICTION_SOURCE,
                    execution_mode: str | None = None) -> dict:
    """Write the prediction artifacts under ``outdir``.

    ``material_root`` is required: every reference written into the documents is expressed
    relative to it, and a path that would escape it is refused instead of being recorded as
    a pointer a confined reader could never resolve.

    The prediction documents are written once and named explicitly: an existing one is never
    overwritten, because a document that has already been frozen must not change underneath
    its recorded hash.
    """
    outdir = Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    material_root = resolve_material_root(material_root)
    inputs_target = outdir / "simulation-prediction-inputs-v1.json"
    if inputs_target.exists():
        raise DirectionPredictionError(
            f"{inputs_target} already exists; refusing to overwrite a frozen prediction "
            f"document (move it or choose another output directory)")
    evidence_target = outdir / "simulation-prediction-evidence-v1.json"
    built = build_documents(store, run_id, outdir=outdir, material_root=material_root,
                           method_path=method_path,
                           dataset_version=dataset_version,
                           predictions_ref_path=str(inputs_target),
                           unit_kind=unit_kind, text_source=text_source,
                           execution_mode=execution_mode)
    inputs_target.write_text(json.dumps(built["inputs"], ensure_ascii=False, indent=1),
                             encoding="utf-8")
    evidence_target.write_text(json.dumps(built["evidence"], ensure_ascii=False, indent=1),
                               encoding="utf-8")
    package_target = outdir / "simulation-prediction-input-package-v1.json"
    package_target.write_text(json.dumps(built["input_package"], ensure_ascii=False,
                                         indent=1), encoding="utf-8")
    # The recorded hash must cover the artifact exactly as written. Hashing a differently
    # serialised copy would let a reviewer recompute a different value from the same file.
    built["freeze_record"]["inputs_sha256"] = sha256_bytes(inputs_target.read_bytes())
    built["freeze_record"]["evidence_sha256"] = sha256_bytes(evidence_target.read_bytes())
    (outdir / "simulation-prediction-freeze-v1.json").write_text(
        json.dumps(built["freeze_record"], ensure_ascii=False, indent=1), encoding="utf-8")

    # The import-shaped document and the freeze record that binds its own digest. The order
    # matters: the record digests the document, so the document is finalized first and is not
    # touched afterwards.
    import_document = build_prediction_import_document(built, source_label=source_label,
                                                      material_class=(
                                                          "real_frozen_material"
                                                          if built["unit"]["data_mode"]
                                                          == "real_historical"
                                                          else "synthetic_fixture_control"))
    import_target = outdir / "simulation-prediction-import-v1.json"
    import_target.write_text(json.dumps(import_document, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    import_freeze = finalize_import_freeze_record(import_document,
                                                  frozen_inputs=built["input_package"])
    import_freeze_target = outdir / "simulation-prediction-import-freeze-v1.json"
    import_freeze_target.write_text(json.dumps(import_freeze, ensure_ascii=False, indent=1),
                                    encoding="utf-8")

    built["summary"]["import_package_sha256"] = built["input_package_sha256"]
    (outdir / "simulation-prediction-summary-v1.json").write_text(
        json.dumps(built["summary"], ensure_ascii=False, indent=1), encoding="utf-8")
    return {"inputs": inputs_target, "evidence": evidence_target,
            "input_package": package_target, "import": import_target,
            "import_freeze": import_freeze_target, "summary": built["summary"],
            "import_document": import_document, "material_root": material_root}
