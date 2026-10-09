import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from riskshield.api import create_app


ROOT = Path(__file__).resolve().parents[1]
EVENT = ROOT / "data/public/unh_change_20240222_event_v2.json"
EVIDENCE = ROOT / "data/research/unh_change_20240222_visibility.json"


def test_archived_day1_event_import_and_cutoff_isolation(tmp_path):
    event = json.loads(EVENT.read_text(encoding="utf-8"))
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    case = event["case_import_projection"]
    witness = evidence["independent_visibility_witness"]
    visible_at = datetime.fromtimestamp(witness["retrieved_on_utc_epoch"], timezone.utc)
    cutoff = datetime.fromisoformat(case["cutoff"])

    assert cutoff == visible_at + timedelta(minutes=30)
    assert datetime.fromisoformat(case["records"][0]["available_at"]) == visible_at
    assert witness["archived_target_url"].endswith(
        "/000073176624000045/unh-20240221.htm"
    )

    with TestClient(create_app(tmp_path / "app.db")) as client:
        imported = client.post("/cases", json=case)
        assert imported.status_code == 200, imported.text
        assert imported.json() == {
            "case_id": case["case_id"], "status": "imported", "record_count": 2
        }
        snapshot = client.get(f"/cases/{case['case_id']}/snapshot").json()
        assert [row["record_id"] for row in snapshot["records"]] == [
            "unh-sec-20240222-initial"
        ]
        assert snapshot["excluded_counts"] == {"not_input": 1}
        assert snapshot["backtest_integrity"] == "archived_inputs"
        assert "unh-sec-20240308-update" not in json.dumps(snapshot)
        assert "资金支持" not in json.dumps(snapshot, ensure_ascii=False)

        later = client.get(f"/evaluations/cases/{case['case_id']}/evidence").json()
        assert later["not_for_agent_context"] is True
        assert [row["record_id"] for row in later["records"]] == [
            "unh-sec-20240308-update"
        ]
