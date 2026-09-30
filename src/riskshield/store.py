import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from riskshield.schemas import CaseImport, SourceRecord


class ConflictError(ValueError):
    pass


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cases (
                    case_id TEXT PRIMARY KEY, metadata TEXT NOT NULL, digest TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS records (
                    case_id TEXT NOT NULL REFERENCES cases(case_id),
                    record_id TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(case_id, record_id)
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_case(self, case: CaseImport):
        content = case.model_dump(mode="json")
        # Record ordering is not a version change.
        content["records"] = sorted(content["records"], key=lambda item: item["record_id"])
        digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        metadata = case.model_dump(mode="json", exclude={"records"})
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT digest FROM cases WHERE case_id=?", (case.case_id,)).fetchone()
            if existing:
                if existing["digest"] != digest:
                    raise ConflictError("案例已存在且内容不同；请使用新的 case_id 保留历史版本。")
                return {"case_id": case.case_id, "status": "unchanged", "record_count": len(case.records)}
            db.execute("INSERT INTO cases VALUES (?, ?, ?)",
                       (case.case_id, json.dumps(metadata, ensure_ascii=False), digest))
            db.executemany("INSERT INTO records VALUES (?, ?, ?)",
                           [(case.case_id, item.record_id, item.model_dump_json()) for item in case.records])
        return {"case_id": case.case_id, "status": "imported", "record_count": len(case.records)}

    def list_cases(self):
        with self.connect() as db:
            return [json.loads(row["metadata"]) for row in db.execute("SELECT metadata FROM cases ORDER BY case_id")]

    def get_case(self, case_id: str):
        with self.connect() as db:
            row = db.execute("SELECT metadata FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            raise KeyError(case_id)
        return json.loads(row["metadata"])

    def records(self, case_id: str):
        self.get_case(case_id)
        with self.connect() as db:
            return [SourceRecord.model_validate_json(row["body"]) for row in
                    db.execute("SELECT body FROM records WHERE case_id=? ORDER BY record_id", (case_id,))]

    def snapshot(self, case_id: str):
        metadata = self.get_case(case_id)
        # Parse the cutoff through the same timezone-aware contract used at import.
        case = CaseImport(**metadata, records=self.records(case_id))
        included, exclusions = [], {}
        for record in case.records:
            reason = None
            if record.role != "input_candidate":
                reason = "not_input"
            elif record.available_at is None:
                reason = "availability_unknown"
            elif record.available_at > case.cutoff or record.published_at > case.cutoff:
                reason = "after_cutoff"
            if reason:
                exclusions[reason] = exclusions.get(reason, 0) + 1
            else:
                included.append(record.model_dump(mode="json"))
        return {
            "case_id": case_id, "cutoff": case.cutoff.isoformat(),
            "data_mode": case.data_mode, "version": case.version,
            "records": included, "excluded_counts": exclusions,
            "backtest_integrity": (
                "no_eligible_inputs" if not included else
                "synthetic_only" if case.data_mode == "synthetic" else
                "approximate" if any(r["historical_integrity"] != "archived" for r in included) else
                "archived_inputs"
            ),
            "simulation_status": "not_implemented_day1",
        }
