import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from riskshield.api import create_app


PACKAGE = Path(__file__).resolve().parents[1] / "data/public/unh_change_20240222_day2_multisource_event.json"
SEC_TARGET = "https://www.sec.gov/ixviewer/ix.html?doc=/Archives/edgar/data/0000731766/000073176624000045/unh-20240221.htm"
STATUS_TARGET = "https://status.changehealthcare.com/incidents/hqpjz25fn3n7"


def test_two_sources_aggregate_with_cutoff_safe_status_update(tmp_path, monkeypatch):
    case = json.loads(PACKAGE.read_text(encoding="utf-8"))["case_import_projection"]

    def fetch(url, **_kwargs):
        if "arctic-shift" in url:
            post_id = url.rsplit("=", 1)[-1]
            return httpx.Response(200, json={"data": [{
                "id": post_id,
                "url": SEC_TARGET if post_id == "1axi1g0" else STATUS_TARGET,
                "title": "Original SEC filing" if post_id == "1axi1g0" else "Optum / Change Healthcare Breach",
                "retrieved_on": 1708636719 if post_id == "1axi1g0" else 1708573941,
            }]})
        if "solution-status" in url:
            return httpx.Response(200, json={"incident": {
                "id": "hqpjz25fn3n7", "incident_updates": [
                    {"id": "9j1b644m46kx", "body": "Change Healthcare cybersecurity interruption.",
                     "created_at": "2024-02-21T16:27:23-05:00",
                     "updated_at": "2024-02-21T16:40:49-05:00",
                     "display_at": "2024-02-21T16:27:23-05:00"},
                    {"id": "laterupdate1", "body": "Later restoration result.",
                     "created_at": "2024-03-08T12:00:00-05:00",
                     "updated_at": "2024-03-08T12:00:00-05:00",
                     "display_at": "2024-03-08T12:00:00-05:00"},
                    {"id": "editedlater1", "body": "Edited after the cutoff.",
                     "created_at": "2024-02-21T16:27:23-05:00",
                     "updated_at": "2024-03-08T12:00:00-05:00",
                     "display_at": "2024-02-21T16:27:23-05:00"},
                    {"id": "otherbefore1", "body": "Another early update.",
                     "created_at": "2024-02-21T16:27:23-05:00",
                     "updated_at": "2024-02-21T16:40:49-05:00",
                     "display_at": "2024-02-21T16:27:23-05:00"},
                ]}})
        return httpx.Response(200, text="<html>SEC original filing</html>")

    monkeypatch.setattr(httpx, "get", fetch)
    with TestClient(create_app(tmp_path / "app.db")) as client:
        assert client.post("/cases", json=case).status_code == 200
        snapshot = client.get(f"/cases/{case['case_id']}/snapshot").json()
        assert len(snapshot["records"]) == 2
        endpoint = f"/cases/{case['case_id']}/observations/collect"
        for payload in [
            {"record_id": "unh-sec-20240222-initial", "adapter": "sec_filing"},
            {"record_id": "unh-sec-20240222-initial", "adapter": "arctic_shift_post", "post_id": "1axi1g0"},
            {"record_id": "optum-status-20240221-cyber-update", "adapter": "optum_status_update",
             "update_id": "9j1b644m46kx"},
            {"record_id": "optum-status-20240221-cyber-update", "adapter": "arctic_shift_post",
             "post_id": "1awwr9j"},
        ]:
            response = client.post(endpoint, json=payload)
            assert response.status_code == 200, response.text
        assert client.post(endpoint, json={
            "record_id": "optum-status-20240221-cyber-update", "adapter": "optum_status_update",
            "update_id": "laterupdate1"}).status_code == 422
        assert client.post(endpoint, json={
            "record_id": "optum-status-20240221-cyber-update", "adapter": "optum_status_update",
            "update_id": "editedlater1"}).status_code == 422
        assert client.post(endpoint, json={
            "record_id": "optum-status-20240221-cyber-update", "adapter": "optum_status_update",
            "update_id": "otherbefore1"}).status_code == 422
        graph = client.post(f"/cases/{case['case_id']}/graph").json()
        assert {claim["record_id"] for claim in graph["claims"]} == {
            "unh-sec-20240222-initial", "optum-status-20240221-cyber-update"
        }
        assert len(graph["claims"]) == 2
        assert len([node for node in graph["nodes"] if node["type"] == "observation"]) == 4
        assert "Later restoration result" not in json.dumps(graph)
        result = client.post(f"/graphs/{graph['graph_id']}/query",
                             json={"question": "Change Healthcare"}).json()
        assert len(result["hits"]) == 2
        assert {hit["source_url"] for hit in result["hits"]} == {
            case["records"][0]["source_url"], STATUS_TARGET
        }


def test_status_page_archive_must_link_exact_incident(tmp_path, monkeypatch):
    case = json.loads(PACKAGE.read_text(encoding="utf-8"))["case_import_projection"]
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(200, json={
        "data": [{"id": "1awwr9j", "url": "https://status.changehealthcare.com/incidents/other",
                  "title": "Other incident", "retrieved_on": 1708573941}]}))
    with TestClient(create_app(tmp_path / "app.db")) as client:
        client.post("/cases", json=case)
        response = client.post(f"/cases/{case['case_id']}/observations/collect", json={
            "record_id": "optum-status-20240221-cyber-update", "adapter": "arctic_shift_post",
            "post_id": "1awwr9j"})
        assert response.status_code == 422
        assert client.get(f"/cases/{case['case_id']}/observations").json() == []


def test_selected_status_version_edited_after_cutoff_is_rejected(tmp_path, monkeypatch):
    case = json.loads(PACKAGE.read_text(encoding="utf-8"))["case_import_projection"]
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(200, json={
        "incident": {"id": "hqpjz25fn3n7", "incident_updates": [{
            "id": "9j1b644m46kx", "body": "Later edited content",
            "created_at": "2024-02-21T16:27:23-05:00",
            "updated_at": "2024-03-08T12:00:00-05:00",
            "display_at": "2024-02-21T16:27:23-05:00",
        }]}}))
    with TestClient(create_app(tmp_path / "app.db")) as client:
        client.post("/cases", json=case)
        response = client.post(f"/cases/{case['case_id']}/observations/collect", json={
            "record_id": "optum-status-20240221-cyber-update", "adapter": "optum_status_update",
            "update_id": "9j1b644m46kx"})
        assert response.status_code == 422
        assert client.get(f"/cases/{case['case_id']}/observations").json() == []
