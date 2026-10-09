"""Isolated cutoff-only engineering baseline for a frozen C handoff.

This process never opens label or post-cutoff files and makes no network calls.
Its constant direction is a plumbing control, not a substantive forecast.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path


MODE = "offline_constant_escalation_baseline_v1"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def write_new(path: Path, value: dict) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(payload)


def aware_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("A source or cutoff time lacks a timezone")
    return parsed


def run(root: Path, handoff_path: Path, output_dir: Path) -> dict:
    root = root.resolve()
    handoff_path = handoff_path.resolve()
    output_dir = output_dir.resolve()
    if handoff_path.parent != (root / "data/evaluation/final-benchmark/manifests").resolve():
        raise ValueError("Handoff must be inside the benchmark manifests directory")
    handoff = read_json(handoff_path)
    allowlist = handoff.get("predictor_execution_read_allowlist")
    if not isinstance(allowlist, list) or len(allowlist) != 1:
        raise ValueError("This baseline requires exactly one cutoff input file")
    relative_input = allowlist[0]
    input_path = (root / relative_input).resolve()
    if (not input_path.is_relative_to((root / "data/evaluation/final-benchmark/inputs").resolve())
            or not input_path.name.startswith("qualified-inputs-v")):
        raise ValueError("Handoff input is outside the cutoff input directory")
    input_hash = digest(input_path)
    if input_hash != handoff.get("input_sha256"):
        raise ValueError("Handoff input SHA-256 mismatch")
    inputs = read_json(input_path)
    events = inputs.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError("No cutoff events in handoff input")
    expected = handoff.get("input_events")
    if not isinstance(expected, list) or len(expected) != len(events):
        raise ValueError("Handoff event inventory mismatch")
    seen_ids = set()
    predictions = []
    cases = []
    output_dir.mkdir(parents=True, exist_ok=True)
    prediction_path = output_dir / f"pilot-predictions-{handoff_path.stem}.json"
    evaluation_path = output_dir / f"pilot-evaluation-inputs-{handoff_path.stem}.json"
    seal_path = output_dir / f"pilot-seal-{handoff_path.stem}.json"
    if any(path.exists() for path in (prediction_path, evaluation_path, seal_path)):
        raise FileExistsError("This handoff already has sealed output; use a new output directory")
    input_ref_path = input_path.relative_to(root).as_posix()
    prediction_ref_path = prediction_path.relative_to(root).as_posix()
    for index, (event, identity) in enumerate(zip(events, expected)):
        if not isinstance(event, dict) or not isinstance(identity, dict):
            raise ValueError("Malformed event inventory")
        event_id, case_id = event.get("event_id"), event.get("case_id")
        if (not isinstance(event_id, str) or not event_id or event_id in seen_ids
                or identity.get("event_id") != event_id
                or identity.get("case_id") != case_id
                or identity.get("split") != event.get("split")):
            raise ValueError("Duplicate or mismatched event identity")
        seen_ids.add(event_id)
        cutoff = aware_time(event["cutoff"])
        sources = event.get("cutoff_sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError(f"No cutoff source for {event_id}")
        source_rows = []
        seen_sources = set()
        for source_index, source in enumerate(sources):
            record_id = source.get("record_id")
            visible = source.get("available_at")
            if (not isinstance(record_id, str) or not record_id
                    or record_id in seen_sources or not isinstance(visible, str)
                    or aware_time(visible) > cutoff or source.get("future_result") is True):
                raise ValueError(f"Ineligible cutoff source for {event_id}")
            seen_sources.add(record_id)
            pointer = f"/events/{index}/cutoff_sources/{source_index}"
            reference = lambda field: {"path": input_ref_path, "pointer": f"{pointer}/{field}"}
            source_rows.append({
                "record_id": record_id,
                "publisher": source.get("publisher"),
                "source_url": source.get("source_url"),
                "source_version": source.get("source_version"),
                "available_at": visible,
                "available_at_evidence_url": source.get("available_at_evidence_url"),
                "source_record_ref": reference("record_id"),
                "source_version_ref": reference("source_version"),
                "source_available_at_ref": reference("available_at"),
                "visibility_evidence_ref": reference("available_at"),
            })
        # Fixed before reading any score-side file; intentionally independent of event text.
        direction = "escalation"
        predictions.append({
            "event_id": event_id, "case_id": case_id,
            "split": event["split"], "direction": direction,
            "sentiment": None, "execution_mode": MODE,
            "model_calls": 0, "network_requests": 0,
            "qualification": "engineering_baseline_only_not_formal_forecast",
        })
        cases.append({
            "evaluation_id": f"{case_id}__direction__{handoff_path.stem}",
            "event_id": event_id, "unit_id": "event_cutoff_summary",
            "target_record_id": source_rows[0]["record_id"],
            "data_mode": "real_historical",
            "source_version": inputs["dataset_version"],
            "cutoff": event["cutoff"],
            "cutoff_evidence_ref": {"path": input_ref_path,
                                    "pointer": f"/events/{index}/cutoff"},
            "sources": source_rows,
            "split": event["split"],
            "predictions": {"direction": {
                "label": direction, "execution_mode": MODE,
                "result_evidence_refs": [{"path": prediction_ref_path,
                                          "pointer": f"/predictions/{index}/direction"}],
            }},
        })
    prediction_document = {
        "schema": "riskshield.final_benchmark.engineering_predictions.v1",
        "handoff": handoff_path.relative_to(root).as_posix(),
        "input_sha256": input_hash,
        "execution_mode": MODE,
        "prediction_policy": "constant escalation; no fitting, model, labels or future evidence",
        "predictions": predictions,
    }
    evaluation_document = {
        "schema": "riskshield.day5.evaluation-inputs.v1",
        "dataset_version": inputs["dataset_version"] + "__" + MODE,
        "cases": cases,
    }
    write_new(prediction_path, prediction_document)
    write_new(evaluation_path, evaluation_document)
    protocol_path = root / "docs/records/final-scoring-protocol.md"
    write_new(seal_path, {
        "schema": "riskshield.final_benchmark.prediction_seal.v1",
        "handoff_path": handoff_path.relative_to(root).as_posix(),
        "handoff_sha256": digest(handoff_path),
        "input_path": input_ref_path, "input_sha256": input_hash,
        "predictor_sha256": digest(Path(__file__)),
        "protocol_sha256": digest(protocol_path),
        "prediction_path": prediction_ref_path,
        "prediction_sha256": digest(prediction_path),
        "evaluation_input_path": evaluation_path.relative_to(root).as_posix(),
        "evaluation_input_sha256": digest(evaluation_path),
        "execution_mode": MODE, "event_count": len(predictions),
        "score_side_files_read": 0, "model_calls": 0, "network_requests": 0,
    })
    return {"seal": str(seal_path), "events": len(predictions), "input_sha256": input_hash}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--handoff", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.handoff, args.out_dir), ensure_ascii=False))


if __name__ == "__main__":
    main()
