"""Isolated v13 cutoff-only engineering baseline.

Reads only the designated v13 input file; it never dereferences provenance URLs
or embedded score-side references. The fixed output is not a model forecast.
"""

from __future__ import annotations

import hashlib
import json
import platform
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "data/evaluation/final-benchmark/inputs/qualified-inputs-v13.json"
OUT = ROOT / "artifacts/final-integration-review/v13-pilot"
EXPECTED_INPUT_SHA256 = "01193167bc59ec4e6d59c12922ad7a9e09d9886deb520c255a5e963aaa9b772c"
HANDOFF_PATH = "data/evaluation/final-benchmark/manifests/e-handoff-v13.json"
HANDOFF_SHA256 = "cc5a4d46d088dcb5bf0c46c9ab9bfcfe65bd34386dcc31d769c513c88db977b9"
MODE = "offline_constant_escalation_baseline_v13"
CONFIG = {
    "policy": "constant escalation for every event; independent of event text",
    "sentiment": "abstain; this event-level input is not an original-title sentiment unit",
    "model_calls": 0,
    "network_requests": 0,
    "provenance_urls_dereferenced": 0,
    "future_evidence_or_labels_read": 0,
    "domain_scope": "candidate only; pending main-agent decision",
}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_exclusive(path: Path, value: dict) -> None:
    body = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(body)


def main() -> None:
    raw = INPUT.read_bytes()
    input_hash = sha256_bytes(raw)
    if input_hash != EXPECTED_INPUT_SHA256:
        raise SystemExit(f"Input hash mismatch: {input_hash}")
    document = json.loads(raw.decode("utf-8"))
    if document.get("schema") != "riskshield.final_benchmark.inputs.v13":
        raise SystemExit("Unexpected input schema")
    events = document.get("events")
    if not isinstance(events, list) or not events:
        raise SystemExit("Input contains no events")

    OUT.mkdir(parents=True, exist_ok=True)
    prediction_path = OUT / "predictions-v13.json"
    config_path = OUT / "config-v13.json"
    seal_path = OUT / "seal-v13.json"
    if any(path.exists() for path in (prediction_path, config_path, seal_path)):
        raise SystemExit("v13 pilot output already exists; refusing to overwrite")

    config_body = json.dumps(CONFIG, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with config_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(config_body)
    predictions = []
    seen = set()
    for event in events:
        if not isinstance(event, dict):
            raise SystemExit("Malformed event row")
        event_id, case_id = event.get("event_id"), event.get("case_id")
        if not isinstance(event_id, str) or not event_id or event_id in seen:
            raise SystemExit("Missing or duplicate event_id")
        if not isinstance(case_id, str) or not case_id:
            raise SystemExit("Missing case_id")
        seen.add(event_id)
        predictions.append({
            "event_id": event_id,
            "case_id": case_id,
            "split": event.get("split"),
            "direction": "escalation",
            "sentiment": None,
            "execution_mode": MODE,
            "qualification": "engineering_baseline_only_not_formal_forecast",
        })

    prediction_document = {
        "schema": "riskshield.final_benchmark.predictions.v13",
        "dataset_version": document.get("dataset_version"),
        "input_path": INPUT.relative_to(ROOT).as_posix(),
        "input_sha256": input_hash,
        "execution_mode": MODE,
        "predictions": predictions,
    }
    write_exclusive(prediction_path, prediction_document)
    seal = {
        "schema": "riskshield.final_benchmark.seal.v13",
        "sealed_at_utc": datetime.now(timezone.utc).isoformat(),
        "handoff_path": HANDOFF_PATH,
        "handoff_sha256": HANDOFF_SHA256,
        "input_path": INPUT.relative_to(ROOT).as_posix(),
        "input_sha256": input_hash,
        "input_expected_sha256": EXPECTED_INPUT_SHA256,
        "predictor_path": Path(__file__).resolve().relative_to(ROOT).as_posix(),
        "predictor_sha256": sha256_bytes(Path(__file__).read_bytes()),
        "config_path": config_path.relative_to(ROOT).as_posix(),
        "config_sha256": sha256_bytes(config_path.read_bytes()),
        "prediction_path": prediction_path.relative_to(ROOT).as_posix(),
        "prediction_sha256": sha256_bytes(prediction_path.read_bytes()),
        "execution_mode": MODE,
        "runtime": platform.python_version(),
        "event_count": len(predictions),
        "model_calls": 0,
        "network_requests": 0,
        "provenance_urls_dereferenced": 0,
        "future_evidence_or_labels_read": 0,
        "embedded_refs_followed": 0,
        "formal_score_ready": False,
        "domain_scope_pending_main_agent": True,
    }
    write_exclusive(seal_path, seal)
    print(json.dumps({"seal": seal_path.relative_to(ROOT).as_posix(),
                      "input_sha256": input_hash,
                      "event_count": len(predictions),
                      "execution_mode": MODE}, ensure_ascii=False))


if __name__ == "__main__":
    main()
