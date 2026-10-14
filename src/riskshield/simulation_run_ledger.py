"""Export a run ledger in the shape the evaluation gate requires.

Why this module exists
----------------------
The evaluator already refuses any live-simulation direction prediction whose
production evidence cannot be reconciled against the engine's own records: it
wants a ``riskshield.simulation_scale.run_ledger.v1`` document carrying the run's
summary and every persisted action row, bound by a digest, plus the digests of
the ledger and of the run configuration. Nothing in the engine wrote that
document, so the gate could never be satisfied -- the contract was specified on
one side and absent on the other.

This is deliberately a thin translation, not a second source of truth:

* ``summary`` is ``Day3Simulation.summary()`` verbatim;
* ``trajectory`` is ``Day3Simulation.trajectory()`` verbatim;
* the digests are computed over those documents with the same canonical-JSON
  routine the evaluator uses, so a re-export of an unchanged run reproduces them.

It never invents a field. If the engine did not record a model, a price version
or a usage row, the ledger carries what the engine has and the gate rejects the
run on its own terms.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

RUN_LEDGER_SCHEMA = "riskshield.simulation_scale.run_ledger.v1"


def canonical_digest(value: Any) -> str:
    """Digest a JSON value exactly as the evaluation import does.

    The evaluator compares ``run_ledger_sha256`` and ``run_config_sha256`` against
    its own ``canonical_digest``, which is canonical JSON with sorted keys and
    compact separators. Computing it any other way would make the two sides
    disagree over an unchanged document.
    """
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_run_ledger(summary: dict, trajectory: list) -> dict:
    """Assemble the ledger document from the engine's own two exports.

    Raises ``ValueError`` when either argument is not the shape the engine
    produces, because a malformed ledger should fail here rather than surface
    later as an unexplained rejection reason.
    """
    if not isinstance(summary, dict):
        raise ValueError("summary must be the object Day3Simulation.summary() returns")
    if not isinstance(trajectory, list):
        raise ValueError("trajectory must be the list Day3Simulation.trajectory() returns")
    for field in ("run_id", "case_id", "config", "status", "completed_rounds", "rounds"):
        if field not in summary:
            raise ValueError(f"summary is missing {field!r}")
    if not isinstance(summary["config"], dict):
        raise ValueError("summary['config'] must be an object")

    return {
        "schema": RUN_LEDGER_SCHEMA,
        "run_id": summary["run_id"],
        "case_id": summary["case_id"],
        "summary": summary,
        "trajectory": trajectory,
        "trajectory_action_count": len(trajectory),
        "note": ("the engine's own summary and every persisted action row, exported "
                 "without addition. The evaluator recomputes the per-model call counts, "
                 "token totals, cost and round completeness from the rows below."),
    }


def ledger_digests(ledger: dict) -> dict:
    """The two digests the evaluator checks, plus the action count for convenience."""
    config = ledger["summary"]["config"]
    return {
        "run_ledger_sha256": canonical_digest(ledger),
        "run_config_sha256": canonical_digest(config),
        "trajectory_action_count": ledger.get("trajectory_action_count"),
    }


def export_run_ledger(simulation, run_id: str, out_path: Path) -> dict:
    """Read one run through the engine and write its ledger to ``out_path``.

    ``simulation`` is a ``Day3Simulation`` (or anything exposing ``summary`` and
    ``trajectory`` with the same signatures). Returns the ledger document that
    was written.
    """
    ledger = build_run_ledger(simulation.summary(run_id), simulation.trajectory(run_id))
    digests = ledger_digests(ledger)
    document = {**ledger, **digests}
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        raise FileExistsError(
            f"{out_path} already exists; a frozen ledger is never overwritten")
    out_path.write_text(json.dumps(document, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8", newline="\n")
    return document


def reconciliation_report(ledger: dict) -> dict:
    """State plainly what the ledger proves and what it does not.

    Reported alongside every export so that a ledger which cannot pass the
    evaluator says so here, in the producer's own terms, instead of failing later
    as an opaque rejection from the consumer.
    """
    summary = ledger["summary"]
    config = summary["config"]
    actions = ledger["trajectory"]
    by_model: dict[str, int] = {}
    tokens = {field: 0 for field in ("prompt_tokens", "completion_tokens",
                                     "cached_input_tokens")}
    valid_rows = 0
    for action in actions:
        if not isinstance(action, dict):
            continue
        if action.get("status") == "valid":
            valid_rows += 1
        model = action.get("requested_model")
        if isinstance(model, str) and model:
            by_model[model] = by_model.get(model, 0) + 1
        usage = action.get("usage")
        if isinstance(usage, dict):
            for field in tokens:
                value = usage.get(field)
                if isinstance(value, int):
                    tokens[field] += value

    agent_count = config.get("agent_count")
    rounds = config.get("rounds")
    expected_actions = (agent_count * rounds
                        if isinstance(agent_count, int) and isinstance(rounds, int) else None)
    round_rows = summary.get("rounds") or []
    return {
        "run_id": summary.get("run_id"),
        "case_id": summary.get("case_id"),
        "backend_mode": config.get("backend_mode"),
        "data_mode": config.get("data_mode"),
        "cutoff": config.get("cutoff"),
        "source_version": config.get("source_version"),
        "status": summary.get("status"),
        "configured_rounds": rounds,
        "completed_rounds": summary.get("completed_rounds"),
        "recorded_round_rows": len(round_rows),
        "abandoned_round_rows": [row.get("round") for row in round_rows
                                 if isinstance(row, dict) and row.get("round_abandoned")],
        "agent_count": agent_count,
        "expected_actions": expected_actions,
        "trajectory_action_count": len(actions),
        "valid_action_rows": valid_rows,
        "model_calls_by_model": by_model,
        "tokens": tokens,
        "cost_cny": summary.get("cost_cny"),
        "gate_preconditions": {
            "real_historical_and_real_model": (config.get("backend_mode") == "real_model"
                                               and config.get("data_mode") == "real_historical"),
            "status_complete": summary.get("status") == "complete",
            "all_configured_rounds_recorded": summary.get("completed_rounds") == rounds,
            "no_abandoned_rounds": not any(
                isinstance(row, dict) and row.get("round_abandoned") is True
                for row in round_rows),
            "action_count_matches_agents_times_rounds": (
                expected_actions is not None and len(actions) == expected_actions),
            "every_action_row_valid": valid_rows == len(actions) and bool(actions),
        },
    }
