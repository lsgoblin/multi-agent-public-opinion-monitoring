"""Archive the live-run ledgers that still exist under the OS temp area.

Why this exists
---------------
`archive_live_evidence.py` looked for the per-tier ledgers under a single hard-coded
temp root (`%LOCALAPPDATA%\\Temp`). On this machine ``TEMP``/``TMP`` both point at
``D:\\temp``, so the glob matched nothing, the manifest recorded ``"databases": []``
and the ledgers were reported as permanently lost. They were never deleted; the
search root was wrong. This script resolves the temp root the way Python's
``tempfile`` does, falls back to the other well-known roots, and archives what it
finds together with a content hash that does not depend on SQLite page layout.

What is archived
----------------
* one row set per ledger (tier runs and message/memory ablation arms);
* for every ledger: file size, file SHA-256, and a **canonical row hash** computed
  over the ordered rows so that a later re-derivation can prove it read the same
  content even if SQLite rewrote its pages;
* a per-table row count plus a compact integrity summary;
* provenance: source path, mtime, and the resolved temp roots that were searched.

The temp area is machine-local and cleared without notice, so run this before any
other work that touches the repository.
"""
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BEIJING = timezone(timedelta(hours=8))

# Ledgers worth keeping, in the order they should be reported. The prefix is the
# mkdtemp prefix used by the driver that produced the ledger.
LEDGER_PREFIXES = ("live-tier-", "ablate-")

# Order matters: the canonical hash is defined over this table order.
HASH_TABLES = (
    ("simulation_runs", ("run_id",)),
    ("simulation_agents", ("run_id", "agent_id")),
    ("simulation_rounds", ("run_id", "round_number")),
    ("simulation_actions", ("run_id", "round_number", "agent_id")),
    ("simulation_messages", ("run_id", "message_id")),
    ("simulation_memories", ("run_id", "agent_id", "memory_id")),
    ("simulation_request_metrics", ("run_id", "round_number", "agent_id")),
    ("simulation_performance_calls", ("run_id", "call_id")),
)

INTEGRITY_COLUMNS = (
    "decision", "usage", "request_audit", "observation_sha256", "model",
    "observation_refs", "price_version",
)


def temp_roots() -> list[Path]:
    """Return the temp roots to search, most likely first, without duplicates.

    ``tempfile.gettempdir()`` is authoritative because it is the value the run
    drivers actually wrote to. ``TEMP``/``TMP`` and the two conventional Windows
    locations are kept as fallbacks so a run started under a different environment
    is still found.
    """
    candidates = [
        Path(tempfile.gettempdir()),
        *(Path(os.environ[name]) for name in ("TEMP", "TMP") if os.environ.get(name)),
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Temp")
    seen, roots = set(), []
    for path in candidates:
        key = str(path).rstrip("\\/").lower()
        if key and key not in seen:
            seen.add(key)
            roots.append(path)
    return roots


def find_ledgers(roots, exclude=None) -> list[Path]:
    """Find ``*.db`` files directly inside a run directory under any temp root.

    ``exclude`` keeps a previous archive's own output out of the result, so running
    this script twice does not archive its copies as if they were fresh ledgers.
    """
    found, seen = [], set()
    excluded = str(exclude.resolve()).lower() if exclude else None
    for root in roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or not entry.name.startswith(LEDGER_PREFIXES):
                continue
            for db in sorted(entry.glob("*.db")):
                key = str(db.resolve()).lower()
                if key in seen or (excluded and key.startswith(excluded)):
                    continue
                seen.add(key)
                found.append(db)
    return found


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def table_names(con) -> set:
    return {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def canonical_row_hash(con, present, tables=HASH_TABLES) -> tuple:
    """Hash the ordered content of ``tables``, independent of SQLite page layout.

    Each row is serialised as JSON with sorted keys and appended as one line, so the
    digest changes when any value changes and stays stable across a VACUUM or a
    journal-mode change.
    """
    digest = hashlib.sha256()
    rows_hashed = 0
    for table, key in tables:
        if table not in present:
            continue
        order = ", ".join(key)
        for row in con.execute(f"SELECT * FROM {table} ORDER BY {order}"):
            payload = {column: row[column] for column in row.keys()}
            digest.update(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     default=str).encode("utf-8"))
            digest.update(b"\n")
            rows_hashed += 1
    return digest.hexdigest(), rows_hashed


def audit_summary(con, present) -> dict:
    """Row counts and the completeness of the per-request audit chain."""
    summary = {"row_counts": {}, "audit_chain": None}
    for table, _ in HASH_TABLES:
        if table in present:
            summary["row_counts"][table] = con.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if "simulation_actions" in present:
        present = [c for c in INTEGRITY_COLUMNS
                   if c in {info[1] for info in
                            con.execute("PRAGMA table_info(simulation_actions)")}]
        selected = ", ".join(f"SUM(CASE WHEN {c} IS NOT NULL THEN 1 ELSE 0 END)" for c in present)
        row = con.execute(
            f"SELECT COUNT(*), {selected} FROM simulation_actions").fetchone()
        summary["audit_chain"] = {
            "rows": row[0],
            "non_null": dict(zip(present, row[1:])),
            "all_present": all(value == row[0] for value in row[1:]) if present else False,
        }
    return summary


def run_identity(con, present) -> dict:
    identity = {}
    if "simulation_runs" in present:
        rows = con.execute(
            "SELECT run_id, case_id, graph_id, status, completed_rounds, cost_cny, "
            "config FROM simulation_runs").fetchall()
        identity["runs"] = [
            {"run_id": row["run_id"], "case_id": row["case_id"],
             "graph_id": row["graph_id"], "status": row["status"],
             "completed_rounds": row["completed_rounds"], "cost_cny": row["cost_cny"],
             "config": json.loads(row["config"]) if row["config"] else None}
            for row in rows]
    if "simulation_performance_calls" in present:
        identity["performance"] = [dict(row) for row in con.execute(
            "SELECT * FROM simulation_performance_calls")]
    return identity


def archive(roots, destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    entries = []
    for path in find_ledgers(roots, exclude=destination):
        con = connect(path)
        present = table_names(con)
        digest, rows_hashed = canonical_row_hash(con, present)
        entry = {
            "source": str(path),
            "source_size": path.stat().st_size,
            "source_modified": datetime.fromtimestamp(
                path.stat().st_mtime, BEIJING).isoformat(timespec="seconds"),
            "temp_prefix": path.parent.name.rsplit("-", 1)[0] + "-",
            "file_sha256": sha256_file(path),
            "canonical_row_sha256": digest,
            "canonical_rows_hashed": rows_hashed,
            **audit_summary(con, present),
            "identity": run_identity(con, present),
        }
        con.close()
        run_ids = [r["run_id"] for r in entry["identity"].get("runs", [])]
        short = run_ids[0].replace("run_", "")[:8] if run_ids else "unknown"
        label = f"{entry['temp_prefix'].rstrip('-')}-{entry['row_counts'].get('simulation_agents', 0)}a-{short}"
        target = destination / f"{label}.db"
        with path.open("rb") as source, target.open("wb") as sink:
            for block in iter(lambda: source.read(1 << 20), b""):
                sink.write(block)
        entry["archived_as"] = target.name
        entry["archived_size"] = target.stat().st_size
        entry["copy_matches_source"] = (
            entry["archived_size"] == entry["source_size"]
            and sha256_file(target) == entry["file_sha256"])
        entries.append(entry)
        print(f"  {target.name:44s} {entry['archived_size']:>10} B  "
              f"rows={entry['canonical_rows_hashed']:>6}  "
              f"copy_ok={entry['copy_matches_source']}")
    return {
        "schema": "simulation-scale-live-ledger-archive-v1",
        "created_at": datetime.now(BEIJING).isoformat(timespec="seconds"),
        "why": ("The per-tier live ledgers were reported lost because the archiving "
                "step searched one hard-coded temp root. They were still present "
                "under the temp root this machine actually uses; this archive "
                "preserves them with layout-independent content hashes."),
        "temp_roots_searched": [str(root) for root in roots],
        "temp_root_used_by_runs": tempfile.gettempdir(),
        "hash_definition": ("canonical_row_sha256 = SHA-256 over "
                            "json.dumps(dict(row), sort_keys=True) + '\\n' for every "
                            "row, tables in HASH_TABLES order, rows ordered by their "
                            "primary key. Independent of SQLite page layout."),
        "hash_tables": [list(t) for t in HASH_TABLES],
        "ledgers": entries,
        "ledger_count": len(entries),
    }


if __name__ == "__main__":
    destination = (Path(sys.argv[1]) if len(sys.argv) > 1 else Path("live-db")).resolve()
    manifest_path = (Path(sys.argv[2]) if len(sys.argv) > 2
                     else destination.parent / "ledger-archive-manifest.json")
    roots = temp_roots()
    print("temp roots searched:")
    for root in roots:
        print(f"  {root}  exists={root.is_dir()}")
    manifest = archive(roots, destination)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    print(f"\narchived ledgers: {manifest['ledger_count']}")
    print(f"destination: {destination}")
    print(f"manifest: {manifest_path}")
    failed = [entry for entry in manifest["ledgers"] if not entry["copy_matches_source"]]
    if failed:
        print(f"COPY VERIFICATION FAILED for {len(failed)} ledger(s)")
        raise SystemExit(1)
    raise SystemExit(0)
