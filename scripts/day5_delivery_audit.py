#!/usr/bin/env python3
"""Create a read-only evidence audit snapshot for the current Day 5 handoff.

The checker reads curated evidence, current documentation links, and Git metadata.
Its only write is artifacts/day5-delivery-audit/snapshot-current.json. It does
not execute application code, tests, models, collectors, notifications, or Docker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


AUDIT_REPORT = "docs/records/day5-delivery-audit.md"
SNAPSHOT_RELATIVE_PATH = "artifacts/day5-delivery-audit/snapshot-current.json"
USAGE_EVIDENCE_PATH = "data/research/day3_synthetic_bailian_500x30_20261007.json"

# Keep this inventory deliberately scoped to the source documents and primary
# evidence cited by the current audit. File contents are never copied to output.
EVIDENCE_PATHS = (
    "AGENTS.md",
    "docs/sources/2026-09-30-新版命题-多智能体仿真舆情监测.md",
    "docs/design/05-赛题指标映射表.md",
    "docs/planning/00-规划总览与待确认事项.md",
    "docs/records/23-Day2受限G2关口复核.md",
    "docs/records/41-Day3百炼500x30真实规模尝试.md",
    "docs/records/43-Day4本机工程与离线验证.md",
    "docs/records/44-Day4Web前端本机闭环实测.md",
    "docs/records/45-Day4Docker本机运行验收.md",
    "docs/records/46-Day4任务报告与预警补验.md",
    "data/research/unh_change_20240222_day2_multisource_probe.json",
    "data/research/unh_change_20240222_human_label_leo.json",
    "data/research/unh_change_20240222_human_label_li.json",
    "data/research/unh_change_20240222_optum_human_label_nike.json",
    "data/research/unh_change_20240222_deepseek_sentiment_probe.json",
    USAGE_EVIDENCE_PATH,
    "data/research/day3_synthetic_bailian_500x30_20261007.sqlite3",
    "artifacts/day4-task-acceptance/real-historical-task.json",
    "artifacts/day4-task-acceptance/real-historical-report.json",
    "artifacts/day4-task-acceptance/real-historical-interaction-ledger.json",
    "artifacts/day4-task-acceptance/synthetic-alert-matrix.json",
    "artifacts/day4-web-closure/01-task-complete.png",
    "artifacts/day4-web-closure/02-report-overview.png",
    "artifacts/day4-web-closure/03-report-emotion.png",
    "artifacts/day4-web-closure/04-report-risk-nodes.png",
    "artifacts/day4-web-closure/05-report-recommendations.png",
    "artifacts/day4-web-closure/06-wecom-preview.png",
    "artifacts/day4-web-closure/07-email-preview.png",
    "artifacts/day4-web-closure/08-alert-blue.png",
    "artifacts/day4-docker-acceptance/cli-evidence.txt",
    AUDIT_REPORT,
    "scripts/day5_delivery_audit.py",
    "tests/test_day5_delivery_audit.py",
    "README.md",
    "Dockerfile",
    "compose.yaml",
    "docs/README.md",
    "docs/planning/03-五日施工计划.md",
    "docs/planning/24-Day3工程决策.md",
    "docs/design/02-系统架构设计.md",
    "docs/design/04-MVP范围说明.md",
    "docs/records/day5-evaluation.md",
    "docs/records/47-Day5工程文档同步与复核.md",
    "docs/delivery/README.md",
    "docs/delivery/deployment.md",
    "docs/delivery/api.md",
    "docs/delivery/user-guide.md",
    "docs/delivery/test-report.md",
    "src/riskshield/day5_evaluation.py",
    "tests/test_day5_evaluation.py",
    "data/evaluation/day5/README.md",
    "data/evaluation/day5/inputs-v1.json",
    "data/evaluation/day5/future-labels-v1.json",
    "data/evaluation/day5/inputs-template.json",
    "data/evaluation/day5/future-labels-template.json",
    "artifacts/day5-evaluation/local-run-v1.json",
    "artifacts/day5-doc-sync/verification.json",
    "artifacts/day4-docker-acceptance/corrected-run/build-final.log",
    "artifacts/day4-docker-acceptance/corrected-run/runtime-cli.txt",
    "artifacts/day4-docker-acceptance/corrected-run/container-logs.txt",
    "artifacts/day4-docker-acceptance/corrected-run/run-summary.json",
    "artifacts/day4-docker-acceptance/corrected-run/restart-summary.json",
    "artifacts/day4-docker-acceptance/corrected-run/completed-job.json",
    "artifacts/day4-docker-acceptance/corrected-run/report.json",
    "artifacts/day4-docker-acceptance/corrected-run/alert.json",
    "artifacts/day4-docker-acceptance/corrected-run/post-restart-job.json",
    "artifacts/day4-docker-acceptance/corrected-run/post-restart-report.json",
    "artifacts/day4-docker-acceptance/corrected-run/post-restart-alert.json",
    "artifacts/day4-docker-acceptance/corrected-run/web-task-overview.png",
    "artifacts/day4-docker-acceptance/corrected-run/web-task-metrics.png",
    "artifacts/day4-docker-acceptance/corrected-run/web-task-ids.png",
    "artifacts/day4-docker-acceptance/corrected-run/web-after-restart.png",
)

MARKDOWN_PATHS = (
    "AGENTS.md", "README.md", "docs/README.md",
    "docs/planning/00-规划总览与待确认事项.md",
    "docs/planning/03-五日施工计划.md",
    "docs/planning/24-Day3工程决策.md",
    "docs/design/02-系统架构设计.md",
    "docs/design/04-MVP范围说明.md",
    "docs/records/45-Day4Docker本机运行验收.md",
    "docs/records/46-Day4任务报告与预警补验.md",
    "docs/records/47-Day5工程文档同步与复核.md",
    AUDIT_REPORT, "docs/records/day5-evaluation.md",
    "docs/delivery/README.md", "docs/delivery/deployment.md",
    "docs/delivery/api.md", "docs/delivery/user-guide.md",
    "docs/delivery/test-report.md", "data/evaluation/day5/README.md",
)

LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\((<[^>]+>|[^)]*)\)")
REMOTE_SCHEMES = {"http", "https", "mailto", "data", "ftp", "codex"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_evidence_files(root: Path, paths: tuple[str, ...] = EVIDENCE_PATHS) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for relative in paths:
        path = root / relative
        record: dict[str, Any] = {"path": relative, "exists": path.is_file()}
        if path.is_file():
            record["size_bytes"] = path.stat().st_size
            record["sha256"] = sha256_file(path)
        files.append(record)
    missing = [item["path"] for item in files if not item["exists"]]
    return {
        "checked_count": len(files),
        "present_count": len(files) - len(missing),
        "missing_paths": missing,
        "files": files,
    }


def _link_target(raw_target: str) -> str:
    target = raw_target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    else:
        # A Markdown title may follow an unquoted local path.
        target = target.split(maxsplit=1)[0] if target else ""
    return target.replace(r"\(", "(").replace(r"\)", ")")


def check_markdown_links(root: Path, markdown_paths: tuple[str, ...] = MARKDOWN_PATHS) -> dict[str, Any]:
    checked: list[dict[str, str]] = []
    broken: list[dict[str, str]] = []
    documents: list[str] = []

    for relative_doc in markdown_paths:
        doc_path = root / relative_doc
        documents.append(relative_doc)
        if not doc_path.is_file():
            broken.append({"document": relative_doc, "target": "", "reason": "document_missing"})
            continue
        source = doc_path.read_text(encoding="utf-8")
        for match in LINK_PATTERN.finditer(source):
            target = _link_target(match.group(1))
            if not target or target.startswith("#"):
                continue
            parsed = urlsplit(target)
            if parsed.scheme.lower() in REMOTE_SCHEMES or target.startswith("//"):
                continue
            local_path = unquote(parsed.path)
            if not local_path:
                continue
            candidate = Path(local_path)
            if not candidate.is_absolute():
                candidate = doc_path.parent / candidate
            candidate = candidate.resolve(strict=False)
            checked.append({"document": relative_doc, "target": target})
            if not candidate.exists():
                broken.append({"document": relative_doc, "target": target, "reason": "target_missing"})

    return {
        "documents": documents,
        "checked_count": len(checked),
        "broken_count": len(broken),
        "broken_links": broken,
        "links": checked,
    }


def summarize_usage_evidence(path: Path) -> dict[str, Any]:
    """Reconcile the recorded run and preserve unknown failed-call usage as unknown."""
    with path.open("r", encoding="utf-8") as stream:
        run = json.load(stream)

    trajectory = run.get("trajectory", [])
    failed = [item for item in trajectory if item.get("status") != "valid"]
    failed_unknown = [item for item in failed if item.get("usage") is None]
    failed_with_usage = [item for item in failed if item.get("usage") is not None]
    usage = run.get("usage", {})
    unknown_calls = usage.get("unknown_usage_calls")

    unknown_reservation = Decimal("0")
    unknown_reservation_missing_rows = 0
    for item in failed_unknown:
        try:
            reservation = Decimal(str(item.get("reserved_cny")))
            if reservation <= 0:
                unknown_reservation_missing_rows += 1
            else:
                unknown_reservation += reservation
        except InvalidOperation:
            unknown_reservation_missing_rows += 1

    ledger_cost = Decimal("0")
    ledger_cost_computable = True
    for item in trajectory:
        try:
            ledger_cost += Decimal(str(item.get("cost_cny", "0")))
        except InvalidOperation:
            ledger_cost_computable = False
            break
    try:
        declared_cost = Decimal(str(run.get("estimated_cost_cny")))
    except InvalidOperation:
        declared_cost = None

    consistency_issues: list[str] = []
    if len(failed) != run.get("failed_decisions"):
        consistency_issues.append("failed_decisions_does_not_match_trajectory")
    if unknown_calls != len(failed_unknown):
        consistency_issues.append("unknown_usage_calls_does_not_match_failed_rows_without_usage")
    if failed_with_usage:
        consistency_issues.append("failed_rows_contain_usage_and_need_manual_review")
    if unknown_reservation_missing_rows:
        consistency_issues.append("unknown_usage_rows_missing_positive_cost_reservation")
    if declared_cost is None or not ledger_cost_computable or declared_cost != ledger_cost:
        consistency_issues.append("declared_estimate_does_not_match_trajectory_cost_sum")

    routes = run.get("backend", {}).get("routes", {})
    price_versions = {
        model: details.get("price_version")
        for model, details in routes.items()
        if isinstance(details, dict) and details.get("price_version") is not None
    }

    return {
        "evidence_path": path.as_posix(),
        "provider": run.get("provider"),
        "execution_mode": "real_model_run_as_recorded_in_local_evidence",
        "data_mode": run.get("data_mode"),
        "configured_agents": run.get("configured_agents"),
        "actual_participants": run.get("actual_participants"),
        "target_rounds": run.get("target_rounds"),
        "completed_rounds": run.get("completed_rounds"),
        "expected_decisions": run.get("expected_decisions"),
        "calls_attempted": run.get("calls_attempted"),
        "valid_decisions": run.get("valid_decisions"),
        "failed_decisions": run.get("failed_decisions"),
        "failed_rows_without_usage": len(failed_unknown),
        "failed_rows_with_usage": len(failed_with_usage),
        "unknown_usage_calls_reported": unknown_calls,
        "unknown_usage_treated_as_zero": False,
        "unknown_usage_conservative_reservation_cny": format(unknown_reservation, "f"),
        "unknown_usage_rows_missing_positive_reservation": unknown_reservation_missing_rows,
        "usage_totals_reported_for_calls_with_receipts": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "cached_input_tokens": usage.get("cached_input_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        },
        "local_estimated_cost_cny": str(run.get("estimated_cost_cny")),
        "trajectory_cost_sum_cny": format(ledger_cost, "f") if ledger_cost_computable else None,
        "estimated_cost_is_provider_invoice": False,
        "provider_invoice_evidence_path": None,
        "price_version_labels": price_versions,
        "consistency_issues": consistency_issues,
    }


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def git_snapshot(root: Path) -> dict[str, Any]:
    head_result = _git(root, "rev-parse", "HEAD")
    status_result = _git(root, "status", "--porcelain=v1", "--branch", "--untracked-files=all")
    lines = status_result.stdout.splitlines()
    branch_line = lines[0] if lines and lines[0].startswith("## ") else None
    entries: list[dict[str, str]] = []
    for line in lines[1:] if branch_line else lines:
        if len(line) >= 3:
            entries.append({"index_status": line[0], "worktree_status": line[1], "path": line[3:]})
        elif line:
            entries.append({"index_status": "?", "worktree_status": "?", "path": line})
    return {
        "head": head_result.stdout.strip() if head_result.returncode == 0 else None,
        "head_command_ok": head_result.returncode == 0,
        "branch_status": branch_line,
        "status_command_ok": status_result.returncode == 0,
        "working_tree_clean": status_result.returncode == 0 and len(entries) == 0,
        "status_entries": entries,
        "errors": [result.stderr.strip() for result in (head_result, status_result) if result.returncode != 0],
    }


def build_snapshot(root: Path) -> dict[str, Any]:
    evidence = check_evidence_files(root)
    usage_path = root / USAGE_EVIDENCE_PATH
    usage_summary: dict[str, Any]
    if usage_path.is_file():
        usage_summary = summarize_usage_evidence(usage_path)
    else:
        usage_summary = {
            "evidence_path": USAGE_EVIDENCE_PATH,
            "consistency_issues": ["usage_evidence_file_missing"],
        }

    return {
        "schema_version": 1,
        "snapshot_type": "current_working_snapshot_nonfinal",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "workspace_root": str(root),
        "git": git_snapshot(root),
        "evidence_inventory": evidence,
        "markdown_links": check_markdown_links(root),
        "usage_and_cost_reconciliation": usage_summary,
        "limits": [
            "Hashes prove which local file bytes were present at snapshot time; they do not prove the evidence content is true or complete.",
            "Usage and estimated cost are read from the local run artifact; no provider billing endpoint or invoice was queried.",
            "The snapshot does not run models, data collection, notifications, tests, application code, or Docker.",
            "Application network counters and static Compose network settings are not operating-system network isolation measurements.",
        ],
    }


def snapshot_has_errors(snapshot: dict[str, Any]) -> bool:
    return bool(
        snapshot["evidence_inventory"]["missing_paths"]
        or snapshot["markdown_links"]["broken_count"]
        or snapshot["usage_and_cost_reconciliation"].get("consistency_issues")
        or not snapshot["git"]["head_command_ok"]
        or not snapshot["git"]["status_command_ok"]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="workspace root (defaults to the parent directory of scripts/)",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    output_path = root / SNAPSHOT_RELATIVE_PATH
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Create the documented target before validating local links to it.
    output_path.touch(exist_ok=True)

    snapshot = build_snapshot(root)
    output_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    evidence = snapshot["evidence_inventory"]
    links = snapshot["markdown_links"]
    usage = snapshot["usage_and_cost_reconciliation"]
    print(f"Snapshot: {output_path.relative_to(root).as_posix()}")
    print(f"Evidence files: {evidence['present_count']}/{evidence['checked_count']} present")
    print(f"Local links: {links['checked_count']} checked, {links['broken_count']} broken")
    print(
        "500x30 usage: "
        f"{usage.get('valid_decisions', 'n/a')}/{usage.get('expected_decisions', 'n/a')} valid; "
        f"{usage.get('unknown_usage_calls_reported', 'n/a')} failed calls have unknown usage; "
        f"local estimate CNY {usage.get('local_estimated_cost_cny', 'n/a')} (not provider invoice)"
    )
    print(f"Git HEAD: {snapshot['git']['head'] or 'unavailable'}; worktree entries: {len(snapshot['git']['status_entries'])}")
    return 1 if snapshot_has_errors(snapshot) else 0


if __name__ == "__main__":
    sys.exit(main())
