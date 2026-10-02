import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from riskshield.api import create_app


ROOT = Path(__file__).resolve().parents[1]
EVENT = ROOT / "data/public/unh_change_20240222_event_v2.json"


def test_real_case_collect_deduplicate_label_and_retrieve_without_future(tmp_path, monkeypatch):
    case = json.loads(EVENT.read_text(encoding="utf-8"))["case_import_projection"]
    calls = []

    def fetch(url, **kwargs):
        assert kwargs["follow_redirects"] is False
        calls.append(url)
        if "arctic-shift" in url:
            return httpx.Response(200, json={"data": [{
                "id": "1axi1g0", "retrieved_on": 1708636719,
                "title": "United Health Group hacked by nation-state associated cyber security threat actor",
                "url": "https://www.sec.gov/ixviewer/ix.html?doc=/Archives/edgar/data/0000731766/000073176624000045/unh-20240221.htm",
                "author": "must not be persisted",
            }]})
        assert "000073176624000045" in url
        return httpx.Response(200, text="<html>Original SEC accession document</html>")

    monkeypatch.setattr(httpx, "get", fetch)
    with TestClient(create_app(tmp_path / "day2.db")) as client:
        assert client.post("/cases", json=case).status_code == 200
        assert client.post(f"/cases/{case['case_id']}/graph").status_code == 422
        source = "unh-sec-20240222-initial"
        sec = {"record_id": source, "adapter": "sec_filing"}
        archive = {"record_id": source, "adapter": "arctic_shift_post", "post_id": "1axi1g0"}
        first = client.post(f"/cases/{case['case_id']}/observations/collect", json=sec)
        assert first.status_code == 200, first.text
        assert first.json()["fetch_count"] == 1
        assert first.json()["realtime_latency_seconds"] is None
        again = client.post(f"/cases/{case['case_id']}/observations/collect", json=sec)
        assert again.json()["observation_id"] == first.json()["observation_id"]
        assert again.json()["fetch_count"] == 2
        assert client.post(f"/cases/{case['case_id']}/graph").status_code == 422
        witness = client.post(f"/cases/{case['case_id']}/observations/collect", json=archive)
        assert witness.status_code == 200, witness.text
        observations = client.get(f"/cases/{case['case_id']}/observations").json()
        assert len(observations) == 2
        assert "must not be persisted" not in json.dumps(observations)

        label = client.post(f"/cases/{case['case_id']}/labels", json={
            "record_id": source, "label": "neutral", "reviewer": "day2_manual_review",
            "rationale": "原始公司申报叙述不利事件，但文本语气为事实陈述。",
        })
        assert label.status_code == 200
        assert client.get(f"/cases/{case['case_id']}/labels").json()[0]["label"] == "neutral"
        assert client.post(f"/cases/{case['case_id']}/labels", json={
            "record_id": source, "label": "negative", "reviewer": "day2_manual_review",
            "rationale": "Conflicting revision",
        }).status_code == 422

        graph_response = client.post(f"/cases/{case['case_id']}/graph")
        assert graph_response.status_code == 200, graph_response.text
        graph = graph_response.json()
        assert len(graph["claims"]) == 1
        assert graph["claims"][0]["record_id"] == source
        assert len(graph["claims"][0]["observation_ids"]) == 2
        graph_id = graph["graph_id"]
        result = client.post(f"/graphs/{graph_id}/query", json={"question": "Change Healthcare systems"}).json()
        assert [hit["record_id"] for hit in result["hits"]] == [source]
        assert result["hits"][0]["source_url"].startswith("https://www.sec.gov/")
        assert result["edges"]
        assert client.post(f"/graphs/{graph_id}/query", json={"question": "unrelated banana"}).json()["hits"] == []
        assert "资金支持" not in json.dumps(graph, ensure_ascii=False)
        assert "资金支持" not in json.dumps(result, ensure_ascii=False)
        assert client.post(f"/cases/{case['case_id']}/observations/collect", json={
            "record_id": "unh-sec-20240308-update", "adapter": "sec_filing"}).status_code == 422
        assert len(calls) == 3


def test_archive_must_link_to_the_same_source(tmp_path, monkeypatch):
    case = json.loads(EVENT.read_text(encoding="utf-8"))["case_import_projection"]
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(200, json={"data": [{
        "id": "1axi1g0", "retrieved_on": 1708636719, "title": "Different filing",
        "url": "https://www.sec.gov/Archives/edgar/data/731766/other.htm",
    }]}))
    with TestClient(create_app(tmp_path / "day2.db")) as client:
        client.post("/cases", json=case)
        response = client.post(f"/cases/{case['case_id']}/observations/collect", json={
            "record_id": "unh-sec-20240222-initial", "adapter": "arctic_shift_post", "post_id": "1axi1g0"})
        assert response.status_code == 422
        assert client.get(f"/cases/{case['case_id']}/observations").json() == []
