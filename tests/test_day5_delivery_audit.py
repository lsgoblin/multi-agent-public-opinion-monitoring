from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from scripts.day5_delivery_audit import (
    check_evidence_files,
    check_markdown_links,
    sha256_file,
    summarize_usage_evidence,
)


def test_sha256_and_evidence_inventory(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.txt"
    evidence.write_bytes(b"audit evidence\n")

    inventory = check_evidence_files(tmp_path, ("evidence.txt", "missing.txt"))

    assert inventory["present_count"] == 1
    assert inventory["missing_paths"] == ["missing.txt"]
    assert inventory["files"][0]["sha256"] == sha256_file(evidence)


def test_markdown_link_check_resolves_local_paths_and_skips_remote(tmp_path: Path) -> None:
    docs = tmp_path / "docs" / "records"
    docs.mkdir(parents=True)
    evidence_dir = tmp_path / "evidence folder"
    evidence_dir.mkdir()
    (evidence_dir / "run.json").write_text("{}", encoding="utf-8")
    (docs / "audit.md").write_text(
        "[present](<../../evidence folder/run.json>) "
        "[missing](../missing.md) [remote](https://example.com)\n",
        encoding="utf-8",
    )

    result = check_markdown_links(tmp_path, ("docs/records/audit.md",))

    assert result["checked_count"] == 2
    assert result["broken_links"] == [
        {"document": "docs/records/audit.md", "target": "../missing.md", "reason": "target_missing"}
    ]


def test_usage_reconciliation_preserves_unknown_usage_and_labels_cost_estimate(tmp_path: Path) -> None:
    artifact = tmp_path / "run.json"
    artifact.write_text(
        json.dumps(
            {
                "provider": "example-provider",
                "data_mode": "synthetic",
                "configured_agents": 1,
                "actual_participants": 1,
                "target_rounds": 1,
                "completed_rounds": 1,
                "expected_decisions": 2,
                "calls_attempted": 2,
                "valid_decisions": 1,
                "failed_decisions": 1,
                "estimated_cost_cny": "0.26",
                "usage": {
                    "prompt_tokens": 10,
                    "cached_input_tokens": 0,
                    "completion_tokens": 5,
                    "unknown_usage_calls": 1,
                },
                "backend": {"routes": {}},
                "trajectory": [
                    {"status": "valid", "usage": {"prompt_tokens": 10}, "cost_cny": "0.25"},
                    {"status": "failed", "usage": None, "cost_cny": "0.01", "reserved_cny": "0.01"},
                ],
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_usage_evidence(artifact)

    assert summary["unknown_usage_calls_reported"] == 1
    assert summary["failed_rows_without_usage"] == 1
    assert summary["failed_rows_with_usage"] == 0
    assert summary["unknown_usage_treated_as_zero"] is False
    assert Decimal(summary["unknown_usage_conservative_reservation_cny"]) == Decimal("0.01")
    assert summary["estimated_cost_is_provider_invoice"] is False
    assert summary["provider_invoice_evidence_path"] is None
    assert summary["consistency_issues"] == []
