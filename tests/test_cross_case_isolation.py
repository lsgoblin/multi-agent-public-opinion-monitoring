"""A negative control for case-scoped graphs, not semantic event dedup accuracy."""

import json
from copy import deepcopy
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from riskshield.api import create_app


PACKAGE = Path(__file__).resolve().parents[1] / "data/public/unh_change_20240222_day2_multisource_event.json"
OTHER_URL = "https://www.sec.gov/Archives/edgar/data/731766/000073176624999999/distinct-event.htm"


def test_distinct_cases_with_same_publisher_record_id_and_terms_stay_separate(tmp_path, monkeypatch):
    original = json.loads(PACKAGE.read_text(encoding="utf-8"))["case_import_projection"]
    first = deepcopy(original)
    first["case_id"] = "test_unh_network_event"
    first["records"] = [next(record for record in first["records"]
                             if record["record_id"] == "unh-sec-20240222-initial")]
    second = deepcopy(first)
    second["case_id"] = "test_unh_distinct_event"
    second["title"] = "UnitedHealth Group distinct billing event"
    second["data_mode"] = "synthetic"
    second["version"] = "synthetic-negative-control-v1"
    second["records"][0].update({
        "source_url": OTHER_URL,
        "title": "UnitedHealth Group distinct billing event filing",
        "summary": "UnitedHealth Group reports a separate billing event in this synthetic fixture.",
        "data_mode": "synthetic",
        "historical_integrity": "synthetic",
        "acquisition_method": "synthetic_fixture",
        "availability_basis": "Synthetic negative control; no claim about a real filing or event.",
    })

    def fetch(url, **_kwargs):
        if "arctic-shift" in url:
            post_id = url.rsplit("=", 1)[-1]
            target = OTHER_URL if post_id == "other12" else first["records"][0]["source_url"]
            return httpx.Response(200, json={"data": [{
                "id": post_id, "url": target, "title": "UnitedHealth Group distinct event",
                "retrieved_on": 1708636719,
            }]})
        return httpx.Response(200, text="Synthetic distinct filing" if url == OTHER_URL
                              else "Original network filing")

    monkeypatch.setattr(httpx, "get", fetch)
    with TestClient(create_app(tmp_path / "app.db")) as client:
        for case, post_id in ((first, "1axi1g0"), (second, "other12")):
            case_id = case["case_id"]
            record_id = case["records"][0]["record_id"]
            assert client.post("/cases", json=case).status_code == 200
            endpoint = f"/cases/{case_id}/observations/collect"
            assert client.post(endpoint, json={"record_id": record_id,
                                               "adapter": "sec_filing"}).status_code == 200
            assert client.post(endpoint, json={"record_id": record_id,
                                               "adapter": "arctic_shift_post",
                                               "post_id": post_id}).status_code == 200

        first_graph = client.post(f"/cases/{first['case_id']}/graph").json()
        second_graph = client.post(f"/cases/{second['case_id']}/graph").json()
        assert first_graph["graph_id"] != second_graph["graph_id"]
        for graph, own, other in ((first_graph, first, second), (second_graph, second, first)):
            assert graph["case_id"] == own["case_id"]
            assert len(graph["claims"]) == 1
            assert graph["claims"][0]["source_url"] == own["records"][0]["source_url"]
            assert {node["id"] for node in graph["nodes"] if node["type"] == "event"} == {
                "event:" + own["case_id"]}
            result = client.post(f"/graphs/{graph['graph_id']}/query",
                                 json={"question": "UnitedHealth Group"}).json()
            assert len(result["hits"]) == 1
            assert result["hits"][0]["source_url"] == own["records"][0]["source_url"]
            assert other["records"][0]["source_url"] not in json.dumps(result)
