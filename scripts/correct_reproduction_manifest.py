"""Correct the reproduction manifest after the raw ledgers were found and archived.

``reproduction-manifest.json`` (v01) recorded ``"databases": []`` and a
``ledger_loss`` block stating the 15,000-row ledger was permanently gone. That
conclusion came from ``archive_live_evidence.py`` searching one hard-coded temp root
(``%LOCALAPPDATA%\\Temp``) while this machine runs with ``TEMP=D:\\temp``, so the glob
matched nothing. The ledgers were never deleted.

This script never rewrites v01: the received original is kept exactly as it was, and
the corrected document is written as ``reproduction-manifest-v02.json``. Every change
is recorded in ``corrections`` so a reviewer can see what was wrong and why.

Usage:
    python correct_reproduction_manifest.py <v01.json> <v02.json> <ledger-archive-manifest.json>
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BEIJING = timezone(timedelta(hours=8))


def main() -> int:
    v01_path, v02_path, archive_path = (Path(arg) for arg in sys.argv[1:4])
    v01 = json.loads(v01_path.read_text(encoding="utf-8"))
    archive = json.loads(archive_path.read_text(encoding="utf-8"))

    by_run = {}
    for entry in archive["ledgers"]:
        for run in entry["identity"].get("runs", []):
            by_run[run["run_id"]] = {"entry": entry, "run": run}

    # Map each published tier to the run_id of the evidence JSON it points at, then to
    # the archived ledger that recorded that same run_id.
    run_id_by_tier = {}
    for row in v01["runs"]:
        candidate = Path(row["evidence"])
        if not candidate.is_absolute():
            candidate = v01_path.parent.parent / candidate
        if candidate.is_file():
            run_id_by_tier[row["tier"]] = json.loads(
                candidate.read_text(encoding="utf-8")).get("run_id")

    v02 = json.loads(json.dumps(v01))  # deep copy, key order preserved
    v02["supersedes"] = v01_path.name
    v02["version_note"] = ("v02 corrects the ledger-loss conclusion of v01. v01 is kept "
                           "unchanged as the received original.")
    v02["created_at"] = datetime.now(BEIJING).isoformat(timespec="seconds")

    surviving = []
    for tier, run_id in run_id_by_tier.items():
        found = by_run.get(run_id)
        if not found:
            continue
        entry, run = found["entry"], found["run"]
        surviving.append({
            "tier": tier,
            "run_id": run_id,
            "archived_as": entry["archived_as"],
            "bytes": entry["archived_size"],
            "file_sha256": entry["file_sha256"],
            "canonical_row_sha256": entry["canonical_row_sha256"],
            "simulation_actions_rows": entry["row_counts"].get("simulation_actions"),
            "audit_chain_all_present": (entry.get("audit_chain") or {}).get("all_present"),
            "source_at_archive_time": entry["source"],
            "source_modified": entry["source_modified"],
        })
    ablation = [entry for entry in archive["ledgers"]
                if entry["temp_prefix"].startswith("ablate")]
    surviving.append({
        "tier": "ablation-12x5 (3 arms)",
        "archived_as": [entry["archived_as"] for entry in ablation],
        "simulation_actions_rows": [entry["row_counts"].get("simulation_actions")
                                    for entry in ablation],
        "audit_chain_all_present": [(entry.get("audit_chain") or {}).get("all_present")
                                    for entry in ablation],
    })

    v02["databases"] = surviving
    v02["ledger_loss"] = {
        "status": "CORRECTED - RAW PER-REQUEST LEDGER RECOVERED AND ARCHIVED",
        "v01_claim": v01["ledger_loss"]["status"],
        "what_was_wrong": (
            "v01 concluded the ledgers were permanently lost because "
            "archive_live_evidence.py searched a single hard-coded temp root "
            "(%LOCALAPPDATA%\\Temp). This machine runs with TEMP=TMP=D:\\temp, so the "
            "glob found zero directories and the empty result was recorded as a lost "
            "ledger. The run directories were still present under D:\\temp."),
        "evidence": (
            "Under D:\\temp: 7 live-tier-* directories and 3 ablate-* directories, all "
            "with their databases. The 500x30 ledger (run_cd531d7837484973b2a1) holds "
            "15,000 simulation_actions rows, every one with usage, request_audit, "
            "observation_sha256 and model populated, plus 15,000 "
            "simulation_request_metrics rows, 31,710 simulation_messages rows and "
            "45,324 simulation_memories rows."),
        "recovery": (
            "Archived by reproduction/archive_live_ledgers.py, which resolves the temp "
            "root the way tempfile does and records a canonical row hash that does not "
            "depend on SQLite page layout. See ledger-archive-manifest.json."),
        "archive_manifest": archive_path.name,
        "temp_root_used_by_runs": archive["temp_root_used_by_runs"],
        "impact_on_claims": (
            "The aggregate claims are no longer only self-reported: every published "
            "aggregate was re-derived from the archived ledger and compared field by "
            "field (see ledger-reconciliation-*.json). The per-request audit trail an "
            "independent reviewer needs IS available for the 500x30 run, the 100x10 run, "
            "the 10x3 run and all three ablation arms."),
        "what_is_still_lost": (
            "Nothing from these runs. Four additional partial-run ledgers (two 10-agent, "
            "one 100-agent, one 500-agent) were also archived; their audit fields are "
            "sparse because those runs never dispatched a request."),
    }
    v02["corrections"] = [
        {"field": "databases", "v01": [], "v02": f"{len(surviving)} entries"},
        {"field": "ledger_loss.status", "v01": v01["ledger_loss"]["status"],
         "v02": v02["ledger_loss"]["status"],
         "reason": "the ledger was present in a temp root the archiver never searched"},
        {"field": "reproduction_limits",
         "v01": "unchanged", "v02": "unchanged, except the runner now requires --db"},
        {"field": "tools/run_live_tier.py",
         "v01": "wrote the ledger into tempfile.mkdtemp",
         "v02": "requires --db and refuses any path inside a temp root",
         "reason": "a re-run must not be able to leave its ledger in a scratch directory"},
    ]
    v02_path.write_text(json.dumps(v02, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {v02_path}")
    print(f"  surviving ledger entries: {len(surviving)}")
    for row in surviving:
        print(f"    {row['tier']:24s} {row.get('archived_as')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
