"""Verify C freeze manifests and select the newest complete E cutoff handoff.

The verifier hashes frozen files but never parses title, label, trajectory, or
prediction payloads. It reads only each handoff's named cutoff input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _version(path: Path, prefix: str) -> int:
    match = re.fullmatch(re.escape(prefix) + r"v([0-9]+)\.json", path.name)
    return int(match.group(1)) if match else -1


def _aware(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing_visible_time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone_required")
    return parsed


def _freeze_checks(root: Path, manifest_dir: Path) -> tuple[list[dict], bool]:
    manifests = sorted(manifest_dir.glob("freeze-v*.json"),
                       key=lambda path: _version(path, "freeze-"))
    checks: list[dict] = []
    all_ok = bool(manifests)
    previous_version = 0
    for path in manifests:
        version = _version(path, "freeze-")
        doc = _read(path)
        member_errors = []
        for item in doc.get("files", []):
            member = (root / item["path"]).resolve()
            if not member.is_file():
                member_errors.append("member_missing")
            elif member.stat().st_size != item["bytes"]:
                member_errors.append("member_size_mismatch")
            elif _sha(member) != item["sha256"]:
                member_errors.append("member_sha256_mismatch")
        parent_hash = doc.get(f"base_freeze_v{version - 1}_sha256") if version > 1 else None
        parent_path = manifest_dir / f"freeze-v{version - 1}.json"
        parent_ok = (not parent_hash or
                     (parent_path.is_file() and _sha(parent_path) == parent_hash))
        contiguous = previous_version == 0 or version == previous_version + 1
        ok = not member_errors and parent_ok and contiguous
        all_ok &= ok
        checks.append({
            "freeze": path.name,
            "sha256": _sha(path),
            "member_count": len(doc.get("files", [])),
            "members_ok": not member_errors,
            "member_errors": sorted(set(member_errors)),
            "parent_hash_ok": parent_ok,
            "version_sequence_ok": contiguous,
        })
        previous_version = version
    return checks, all_ok


def _handoff_check(root: Path, base: Path, manifest_dir: Path,
                   handoff_path: Path) -> dict[str, Any]:
    handoff = _read(handoff_path)
    errors: list[str] = []
    allowlist = handoff.get("predictor_execution_read_allowlist")
    if not isinstance(allowlist, list) or len(allowlist) != 1:
        return {"handoff": handoff_path.name, "complete_cutoff_handoff": False,
                "errors": ["cutoff_allowlist_must_contain_one_input"],
                "handoff_sha256": _sha(handoff_path)}
    input_rel = allowlist[0]
    input_path = (root / input_rel).resolve()
    input_dir = (base / "inputs").resolve()
    if not input_path.is_relative_to(input_dir):
        errors.append("input_outside_cutoff_directory")
    if not input_path.is_file():
        errors.append("input_missing")
        input = {}
    else:
        input_hash = _sha(input_path)
        if input_hash != handoff.get("input_sha256"):
            errors.append("input_sha256_mismatch")
        input = _read(input_path)

    events, inventory = input.get("events"), handoff.get("input_events")
    if not isinstance(events, list) or not events:
        errors.append("input_events_missing")
        events = []
    if not isinstance(inventory, list) or len(events) != len(inventory):
        errors.append("handoff_event_inventory_mismatch")
        inventory = []
    split_counts: Counter[str] = Counter()
    identities: list[str] = []
    if len(events) == len(inventory):
        for event, identity in zip(events, inventory):
            if not isinstance(event, dict) or not isinstance(identity, dict):
                errors.append("malformed_event_identity")
                continue
            event_id = event.get("event_id")
            if (not isinstance(event_id, str) or not event_id
                    or identity.get("event_id") != event_id
                    or identity.get("case_id") not in (None, event.get("case_id"))
                    or identity.get("split") != event.get("split")):
                errors.append("event_identity_mismatch")
            identities.append(event_id if isinstance(event_id, str) else "")
            split = event.get("split")
            split_counts[split if isinstance(split, str) else "unknown"] += 1
            try:
                cutoff = _aware(event.get("cutoff"))
                sources = event.get("cutoff_sources")
                if not isinstance(sources, list) or not sources:
                    errors.append("cutoff_sources_missing")
                    continue
                seen_records: set[str] = set()
                for source in sources:
                    if not isinstance(source, dict):
                        errors.append("malformed_source")
                        continue
                    record_id = source.get("record_id")
                    if not isinstance(record_id, str) or not record_id or record_id in seen_records:
                        errors.append("source_identity_missing_or_duplicate")
                    if isinstance(record_id, str):
                        seen_records.add(record_id)
                    if _aware(source.get("available_at")) > cutoff:
                        errors.append("source_visible_after_cutoff")
                    if source.get("future_result") is True:
                        errors.append("future_result_flagged")
            except (TypeError, ValueError):
                errors.append("cutoff_or_source_time_invalid")

    version = _version(handoff_path, "e-handoff-")
    corresponding_freeze = manifest_dir / f"freeze-v{version}.json"
    input_frozen = handoff_frozen = False
    if corresponding_freeze.is_file():
        freeze = _read(corresponding_freeze)
        members = {entry.get("path") for entry in freeze.get("files", [])
                   if isinstance(entry, dict)}
        input_frozen = input_rel in members
        handoff_frozen = handoff_path.resolve().relative_to(root).as_posix() in members
        if not input_frozen:
            errors.append("input_not_in_corresponding_freeze")
        if not handoff_frozen:
            errors.append("handoff_not_in_corresponding_freeze")
    else:
        errors.append("corresponding_freeze_missing")
    return {
        "handoff": handoff_path.name,
        "version": version,
        "handoff_sha256": _sha(handoff_path),
        "complete_cutoff_handoff": not errors,
        "errors": sorted(set(errors)),
        "input_path": input_rel,
        "input_sha256": _sha(input_path) if input_path.is_file() else None,
        "event_count": len(events),
        "event_ids": identities,
        "split_counts": dict(split_counts),
        "score_side_count": len(handoff.get("score_side_may_read_after_prediction_freeze", [])),
        "input_in_corresponding_freeze": input_frozen,
        "handoff_in_corresponding_freeze": handoff_frozen,
    }


def verify(root: Path) -> dict[str, Any]:
    root = root.resolve()
    base = root / "data/evaluation/final-benchmark"
    manifest_dir = base / "manifests"
    freeze_checks, freezes_ok = _freeze_checks(root, manifest_dir)
    handoff_paths = sorted(manifest_dir.glob("e-handoff-v*.json"),
                           key=lambda path: _version(path, "e-handoff-"))
    handoffs = [_handoff_check(root, base, manifest_dir, path) for path in handoff_paths]
    valid = [item for item in handoffs if item["complete_cutoff_handoff"]]
    selected = max(valid, key=lambda item: item["version"]) if valid else None
    latest_freeze = max(freeze_checks,
                        key=lambda item: _version(Path(item["freeze"]), "freeze-")) if freeze_checks else None
    formal_ready = None
    latest_status_counts: dict[str, int] = {}
    status_count_error = None
    if latest_freeze:
        try:
            freeze_doc = _read(manifest_dir / latest_freeze["freeze"])
            entries = freeze_doc.get("files", [])
            status_entries = [entry for entry in entries
                              if Path(entry.get("path", "")).name.startswith("candidate-status-v")]
            if not status_entries:
                raise ValueError("status_manifest_missing")
            status_entry = max(
                status_entries,
                key=lambda entry: _version(Path(entry["path"]), "candidate-status-"),
            )
            status_path = (root / status_entry["path"]).resolve()
            if (not status_path.is_relative_to(root)
                    or not status_path.is_file()
                    or status_path.stat().st_size != status_entry["bytes"]
                    or _sha(status_path) != status_entry["sha256"]):
                raise ValueError("status_manifest_hash_mismatch")
            status = _read(status_path)
            legacy = status.get("trajectory_and_formal_scoring")
            reconciliation_ref = status.get("final_reconciliation")
            if isinstance(legacy, dict):
                counts = legacy
                formal_ready = counts.get("formal_score_ready_holdout_total")
            elif isinstance(reconciliation_ref, dict):
                rel = reconciliation_ref.get("path")
                expected_sha = reconciliation_ref.get("sha256")
                frozen_entries = {entry.get("path"): entry for entry in entries}
                reconciliation_entry = frozen_entries.get(rel)
                if not isinstance(rel, str) or not isinstance(expected_sha, str):
                    raise ValueError("reconciliation_reference_invalid")
                if (reconciliation_entry is None
                        or reconciliation_entry.get("sha256") != expected_sha):
                    raise ValueError("reconciliation_not_frozen_or_hash_disagrees")
                reconciliation_path = (root / rel).resolve()
                if (not reconciliation_path.is_relative_to(root)
                        or _sha(reconciliation_path) != expected_sha):
                    raise ValueError("reconciliation_hash_mismatch")
                counts = _read(reconciliation_path).get("counts", {})
                formal_ready = counts.get("formal_score_ready_holdout_events")
            else:
                raise ValueError("scoring_count_schema_unsupported")
            allowed_count_keys = (
                "completed_independent_human_sentiment_double_label_units",
                "same_label_agreement_units",
                "sentiment_units_with_paired_E_prediction",
                "computed_sentiment_score_units",
                "trajectory_evidence_qualified_events",
                "independent_human_trajectory_labels",
                "formal_score_ready_holdout_events",
                "formal_event_target",
                "formal_event_gap",
            )
            latest_status_counts = {
                key: counts[key] for key in allowed_count_keys
                if type(counts.get(key)) is int
            }
        except (OSError, KeyError, TypeError, ValueError):
            status_count_error = "latest_scoring_status_count_unavailable_or_unverified"
    return {
        "schema": "riskshield.final_integration.c_batch_integrity.v1",
        "latest_complete_handoff": selected,
        "handoffs": handoffs,
        "freezes_checked": freeze_checks,
        "all_freeze_members_and_parent_hashes_ok": freezes_ok,
        "latest_freeze": latest_freeze,
        "formal_score_ready_holdout_total": formal_ready,
        "latest_status_counts": latest_status_counts,
        "status_count_error": status_count_error,
        "status_payload_policy": "Only aggregate counts were parsed; C label, title, AI annotation, and trajectory payloads were not opened.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path,
                        default=Path("artifacts/final-integration-review/c-batch-integrity.json"))
    args = parser.parse_args()
    root = args.root.resolve()
    output = (root / args.out).resolve() if not args.out.is_absolute() else args.out.resolve()
    if not output.is_relative_to(root):
        raise ValueError("Audit output must stay inside the workspace")
    result = verify(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8", newline="\n")
    selected = result["latest_complete_handoff"]
    print(json.dumps({"output": str(output), "selected_handoff": selected,
                      "freeze_count": len(result["freezes_checked"]),
                      "freeze_hashes_ok": result["all_freeze_members_and_parent_hashes_ok"],
                      "formal_score_ready_holdout_total": result["formal_score_ready_holdout_total"]},
                     ensure_ascii=False))
    return 0 if selected and result["all_freeze_members_and_parent_hashes_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
