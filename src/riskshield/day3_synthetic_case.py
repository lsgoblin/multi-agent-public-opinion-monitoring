"""Purely fictional case and graph used by Day 3 engineering runs."""

import json
from datetime import datetime, timezone

from riskshield.schemas import CaseImport
from riskshield.store import Store


CASE_ID = "fictional_northstar_service_day3"
GRAPH_ID = "fictional_northstar_graph_v1"
RECORD_ID = "fictional-service-notice"


def prepare_synthetic_case(store: Store):
    case = CaseImport.model_validate({
        "case_id": CASE_ID,
        "title": "Fictional Northstar Insurance test service interruption",
        "scope": "Synthetic Day 3 model validation only",
        "cutoff": "2026-01-01T10:30:00+08:00",
        "cutoff_basis": "Synthetic notice available at 10:00 plus 30 minutes.",
        "data_mode": "synthetic",
        "version": "day3-synthetic-v1",
        "records": [{
            "record_id": RECORD_ID, "channel": "official_status",
            "source_url": "https://example.com/fictional-northstar-status",
            "publisher": "Fictional Northstar Insurance",
            "title": "Fictional test service is temporarily unavailable",
            "summary": ("In a fictional exercise, Northstar Insurance reports a temporary "
                        "test-service interruption and says technicians are investigating."),
            "content_kind": "human_summary", "data_mode": "synthetic",
            "published_at": "2026-01-01T10:00:00+08:00",
            "available_at": "2026-01-01T10:00:00+08:00",
            "collected_at": "2026-01-01T10:01:00+08:00",
            "availability_basis": "Synthetic fixture created for authorized validation.",
            "historical_integrity": "synthetic", "acquisition_method": "synthetic_fixture",
            "role": "input_candidate", "limitations": ["Fictional; not a real company or event."],
        }],
    })
    store.import_case(case)
    snapshot = store.snapshot(CASE_ID)
    source = snapshot["records"][0]
    body = {
        "case_id": CASE_ID, "cutoff": snapshot["cutoff"],
        "source_version": snapshot["version"], "nodes": [], "edges": [],
        "claims": [{
            "claim_id": "claim:" + RECORD_ID, "record_id": RECORD_ID,
            "text": source["summary"], "title": source["title"],
            "source_url": source["source_url"], "available_at": source["available_at"],
            "source_node_id": "source:" + RECORD_ID,
            "publisher_node_id": "publisher:fictional-northstar",
            "observation_ids": [], "observation_node_ids": [],
            "evidence_adapters": ["synthetic_fixture"],
        }],
        "excluded_counts": snapshot["excluded_counts"],
    }
    with store.connect() as db:
        db.execute("INSERT OR IGNORE INTO graphs VALUES (?, ?, ?, ?, ?, ?)", (
            GRAPH_ID, CASE_ID, snapshot["cutoff"], snapshot["version"],
            datetime.now(timezone.utc).isoformat(), json.dumps(body, ensure_ascii=False)))
