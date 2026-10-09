import json
import time
from decimal import Decimal
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from riskshield.api import create_app
from riskshield.evidence import Day2Pipeline
from riskshield.simulation import Day3Simulation, DecisionResult
from riskshield.schemas import AgentDecision, CaseImport
from riskshield.store import Store
from tests.fixtures.day3_synthetic_case import CASE_ID, GRAPH_ID, prepare_synthetic_case


def set_page(app, page):
    next(item for item in app.radio if item.key == "main_navigation").set_value(page).run(timeout=20)


def test_ui_import_snapshot_and_evaluation_toggle(tmp_path, monkeypatch):
    """Exercise the real UI script against an isolated real API/store, not canned data."""
    with TestClient(create_app(tmp_path / "ui.db")) as api:
        def local_request(method, url, **kwargs):
            path = url.removeprefix("http://127.0.0.1:8000")
            kwargs.pop("timeout", None)
            return api.request(method, path, **kwargs)

        monkeypatch.setenv("RISKSHIELD_API_URL", "http://127.0.0.1:8000")
        monkeypatch.setattr(httpx, "request", local_request)
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "ui/app.py"))
        app.run(timeout=20)
        assert not app.exception
        opening_copy = " ".join(item.value for item in (*app.caption, *app.markdown))
        assert "风险研判与预警" in opening_copy
        assert all(label not in opening_copy for label in ("Day 1", "Day 2", "Day 3", "Day 4", "DAY 4"))
        set_page(app, "今日概览")
        assert not app.exception
        assert app.metric[1].value == "0"
        set_page(app, "事件证据")
        assert not app.exception
        next(button for button in app.button if button.label == "导入多来源历史事件").click()
        app.run(timeout=20)
        assert not app.exception
        assert app.checkbox[0].value is False
        before = " ".join(item.value for item in app.markdown)
        assert "Form 8-K: Change Healthcare cybersecurity incident" in before
        assert "Form 8-K/A: later recovery update" not in before
        app.checkbox[0].check().run(timeout=20)
        assert not app.exception
        assert "Form 8-K/A: later recovery update" in " ".join(item.value for item in app.markdown)
        next(button for button in app.button if button.label == "导入多来源历史事件").click()
        app.run(timeout=20)
        assert len(api.get("/cases").json()) == 1
        assert any("未重复入库" in message.value for message in app.success)


def test_ui_creates_local_configuration_without_running_or_reporting(tmp_path, monkeypatch):
    db_path = tmp_path / "ui.db"
    store = Store(db_path)
    Day2Pipeline(store)
    prepare_synthetic_case(store)
    with TestClient(create_app(db_path)) as api:
        def local_request(method, url, **kwargs):
            kwargs.pop("timeout", None)
            return api.request(method, url.removeprefix("http://127.0.0.1:8000"), **kwargs)

        monkeypatch.setenv("RISKSHIELD_API_URL", "http://127.0.0.1:8000")
        monkeypatch.setattr(httpx, "request", local_request)
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "ui/app.py"))
        app.run(timeout=20)
        assert not app.exception
        next(item for item in app.text_input if item.label == "证据图 ID").set_value(GRAPH_ID)
        next(item for item in app.button if item.label == "创建本地任务配置").click()
        app.run(timeout=20)
        assert not app.exception
        runs = api.get("/simulations").json()["runs"]
        assert len(runs) == 1
        assert runs[0]["status"] == "created"
        assert runs[0]["completed_rounds"] == 0
        assert api.get(f"/simulations/{runs[0]['run_id']}/trajectory").json()["actions"] == []
        app.session_state["report_id"] = "report_from_another_task"
        set_page(app, "处置队列")
        next(item for item in app.button if item.label == "打开关联图谱").click()
        app.run(timeout=20)
        assert not app.exception
        assert next(item for item in app.radio if item.key == "main_navigation").value == "五模块报告"
        assert any("来源证据图" in item.value for item in app.caption)
        assert app.session_state["report_id"] == ""
        assert not app.exception
        next(item for item in app.button if item.label == "生成离线报告").click()
        app.run(timeout=20)
        assert not app.exception
        assert any("422" in item.value for item in app.error)


def test_ui_reads_five_module_report_and_offline_preview(tmp_path, monkeypatch):
    class OfflineBackend:
        mode = "test_substitute"
        reservation_cny = Decimal("0")
        input_cny_per_million = Decimal("0")
        output_cny_per_million = Decimal("0")
        price_version = "offline-ui-test"

        def decide(self, observation):
            return DecisionResult(AgentDecision(
                agent_id=observation["agent_id"], action="share",
                evidence_ids=[observation["evidence"][0]["evidence_id"]],
                reason="Share the fictional notice in the offline UI test.",
            ), usage={"prompt_tokens": 1, "completion_tokens": 1}, model="offline-ui-test")

    db_path = tmp_path / "ui.db"
    store = Store(db_path)
    simulation = Day3Simulation(store)
    prepare_synthetic_case(store)
    run_id = simulation.create_run(CASE_ID, GRAPH_ID, agent_count=2, rounds=2)
    simulation.advance(run_id, OfflineBackend())
    with TestClient(create_app(db_path)) as api:
        report = api.post("/reports", json={"run_id": run_id}).json()
        assert report["modules"]["emotion_evolution"]["reconstruction_status"] == \
            "validated_against_final_agent_state"
        assert len(report["modules"]["emotion_evolution"]["series"]) == 2
        alert_id = report["modules"]["risk"]["alert_id"]

        def local_request(method, url, **kwargs):
            kwargs.pop("timeout", None)
            return api.request(method, url.removeprefix("http://127.0.0.1:8000"), **kwargs)

        monkeypatch.setenv("RISKSHIELD_API_URL", "http://127.0.0.1:8000")
        monkeypatch.setattr(httpx, "request", local_request)
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "ui/app.py"))
        app.run(timeout=20)
        assert not app.exception
        set_page(app, "五模块报告")
        assert not app.exception
        next(item for item in app.text_input if item.label == "查看报告 ID").set_value(report["report_id"])
        app.run(timeout=20)
        assert not app.exception
        headings = " ".join(item.value for item in app.markdown)
        assert "模拟传播图" in headings
        assert "业务处置建议" in headings
        assert "来源证据图" in headings
        assert "主要依据" in headings
        assert "完整自然语言追问尚未实现" in " ".join(item.value for item in app.caption)
        query = next(item for item in app.text_input if item.label == "来源证据检索")
        query.set_value("Fictional test service").run(timeout=20)
        next(item for item in app.button if item.label == "检索带引用证据").click()
        app.run(timeout=20)
        assert not app.exception
        query_result = app.session_state["evidence_query_result"]
        assert query_result["hits"][0]["record_id"] == "fictional-service-notice"
        assert query_result["hits"][0]["source_url"] == "https://example.com/fictional-northstar-status"
        assert any("fictional-service-notice" in item.value for item in app.caption)
        assert any("打开引用来源" in item.value for item in app.markdown)
        set_page(app, "预警中心")
        assert not app.exception
        next(item for item in app.text_input if item.label == "查看预警 ID").set_value(alert_id)
        app.run(timeout=20)
        assert not app.exception
        next(item for item in app.button if item.label == "生成通知预览").click()
        app.run(timeout=20)
        assert not app.exception


def test_ui_prepares_and_runs_offline_demo_with_job_evidence(tmp_path, monkeypatch):
    db_path = tmp_path / "ui.db"
    with TestClient(create_app(db_path)) as api:
        def local_request(method, url, **kwargs):
            kwargs.pop("timeout", None)
            return api.request(method, url.removeprefix("http://127.0.0.1:8000"), **kwargs)

        monkeypatch.setenv("RISKSHIELD_API_URL", "http://127.0.0.1:8000")
        monkeypatch.setattr(httpx, "request", local_request)
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "ui/app.py"))
        app.run(timeout=20)
        assert not app.exception
        next(item for item in app.button if item.label == "准备离线演示任务").click()
        app.run(timeout=20)
        assert not app.exception
        runs = api.get("/simulations").json()["runs"]
        assert len(runs) == 1
        assert runs[0]["config"]["agent_count"] == 10
        assert runs[0]["config"]["rounds"] == 3
        assert runs[0]["config"]["concurrency"] == 4
        assert runs[0]["config"]["data_mode"] == "synthetic"

        next(item for item in app.button if item.label == "启动本机离线任务").click()
        app.run(timeout=20)
        assert not app.exception
        jobs = api.get("/jobs").json()["jobs"]
        assert len(jobs) == 1
        job_id = jobs[0]["job_id"]
        for _ in range(200):
            job = api.get(f"/jobs/{job_id}").json()
            if job["status"] in {"complete", "failed"}:
                break
            time.sleep(0.01)
        assert job["status"] == "complete"
        assert job["run_id"] == runs[0]["run_id"]
        assert job["progress"] == 1.0
        assert job["model_calls"] == 0
        assert job["network_requests"] == 0
        assert job["report_id"]
        assert job["alert_id"]
        app.run(timeout=20)
        assert not app.exception
        visible = " ".join(
            str(item.value)
            for collection in (app.markdown, app.caption, app.success)
            for item in collection
        )
        metrics = {item.label: str(item.value) for item in app.metric}
        assert metrics["云模型调用"] == "0"
        assert metrics["外部网络请求"] == "0"
        assert "页面不会自动轮询" in visible
        assert job["report_id"] in visible
        assert job["alert_id"] in visible
        next(item for item in app.button if item.label == "打开关联图谱").click()
        app.run(timeout=20)
        assert not app.exception
        assert next(item for item in app.radio if item.key == "main_navigation").value == "五模块报告"
        assert any("来源证据图" in item.value for item in app.caption)
        set_page(app, "处置队列")
        assert not app.exception
        next(item for item in app.button if item.label == "打开关联报告").click()
        app.run(timeout=20)
        assert not app.exception
        assert next(item for item in app.radio if item.key == "main_navigation").value == "五模块报告"
        assert "业务处置建议" in " ".join(item.value for item in app.markdown)
        set_page(app, "处置队列")
        assert not app.exception
        next(item for item in app.button if item.label == "打开关联预警").click()
        app.run(timeout=20)
        assert not app.exception
        assert next(item for item in app.radio if item.key == "main_navigation").value == "预警中心"
        assert next(item for item in app.selectbox if item.key == "alert_run_widget").value == job["run_id"]
        assert any(item.label == "风险等级" and item.value == "蓝色" for item in app.metric)
        assert "合成数据" in " ".join(item.value for item in app.caption)


def test_ui_shows_offline_job_failure(tmp_path, monkeypatch):
    db_path = tmp_path / "ui.db"
    with TestClient(create_app(db_path)) as api:
        def local_request(method, url, **kwargs):
            kwargs.pop("timeout", None)
            return api.request(method, url.removeprefix("http://127.0.0.1:8000"), **kwargs)

        run = api.post("/demos/day4/run", json={
            "agent_count": 2, "rounds": 2, "concurrency": 2,
        }).json()
        with Store(db_path).connect() as db:
            db.execute("UPDATE graphs SET body='{' WHERE graph_id=?", (run["graph_id"],))
        started = api.post("/jobs", json={"run_id": run["run_id"]}).json()
        for _ in range(200):
            failed_job = api.get(f"/jobs/{started['job_id']}").json()
            if failed_job["status"] in {"complete", "failed"}:
                break
            time.sleep(0.01)
        assert failed_job["status"] == "failed"
        assert failed_job["error"] == "data_integrity_error"

        monkeypatch.setenv("RISKSHIELD_API_URL", "http://127.0.0.1:8000")
        monkeypatch.setattr(httpx, "request", local_request)
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "ui/app.py"))
        app.run(timeout=20)
        assert not app.exception
        assert any("data_integrity_error" in item.value for item in app.error)


def test_ui_shows_empty_propagation_and_no_match_path(tmp_path, monkeypatch):
    class QuietBackend:
        mode = "test_substitute"
        reservation_cny = Decimal("0")
        input_cny_per_million = Decimal("0")
        output_cny_per_million = Decimal("0")
        price_version = "quiet-ui-test"

        def decide(self, observation):
            return DecisionResult(AgentDecision(
                agent_id=observation["agent_id"], action="observe",
                evidence_ids=[observation["evidence"][0]["evidence_id"]],
                reason="No message in this offline empty-path test.",
            ), usage={"prompt_tokens": 1, "completion_tokens": 1}, model="quiet-ui-test")

    db_path = tmp_path / "ui-empty.db"
    store = Store(db_path)
    simulation = Day3Simulation(store)
    prepare_synthetic_case(store)
    run_id = simulation.create_run(CASE_ID, GRAPH_ID, agent_count=2, rounds=1)
    simulation.advance(run_id, QuietBackend())
    with TestClient(create_app(db_path)) as api:
        report = api.post("/reports", json={"run_id": run_id}).json()

        def local_request(method, url, **kwargs):
            kwargs.pop("timeout", None)
            return api.request(method, url.removeprefix("http://127.0.0.1:8000"), **kwargs)

        monkeypatch.setenv("RISKSHIELD_API_URL", "http://127.0.0.1:8000")
        monkeypatch.setattr(httpx, "request", local_request)
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "ui/app.py"))
        app.run(timeout=20)
        assert not app.exception
        set_page(app, "五模块报告")
        assert not app.exception
        next(item for item in app.text_input if item.label == "查看报告 ID").set_value(report["report_id"])
        app.run(timeout=20)
        assert not app.exception
        assert any("本次运行没有记录模拟传播边" in item.value for item in app.info), {
            "info": [item.value for item in app.info],
            "errors": [item.value for item in app.error],
            "markdown_tail": " ".join(item.value for item in app.markdown)[-1500:],
        }
        query = next(item for item in app.text_input if item.label == "来源证据检索")
        query.set_value("no-such-source-term-xyz").run(timeout=20)
        next(item for item in app.button if item.label == "检索带引用证据").click()
        app.run(timeout=20)
        assert not app.exception
        assert any("没有匹配到截止前来源" in item.value for item in app.info)


def test_ui_runs_local_real_historical_case_and_displays_timing(tmp_path, monkeypatch):
    db_path = tmp_path / "ui-historical.db"
    package_path = (Path(__file__).resolve().parents[1] / "data/public/"
                    "unh_change_20240222_day2_multisource_event.json")
    package = json.loads(package_path.read_text(encoding="utf-8"))
    case = CaseImport.model_validate(package["case_import_projection"])
    Store(db_path).import_case(case)
    with TestClient(create_app(db_path)) as api:
        existing = api.post("/day4/historical/jobs", json={
            "case_id": case.case_id, "agent_count": 1, "rounds": 1, "concurrency": 1,
        }).json()
        for _ in range(200):
            existing_job = api.get(f"/jobs/{existing['job_id']}").json()
            if existing_job["status"] in {"complete", "failed"}:
                break
            time.sleep(0.01)
        assert existing_job["status"] == "complete", existing_job

        def local_request(method, url, **kwargs):
            kwargs.pop("timeout", None)
            return api.request(method, url.removeprefix("http://127.0.0.1:8000"), **kwargs)

        monkeypatch.setenv("RISKSHIELD_API_URL", "http://127.0.0.1:8000")
        monkeypatch.setattr(httpx, "request", local_request)
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "ui/app.py"))
        app.run(timeout=20)
        assert not app.exception
        next(item for item in app.button
             if item.label == "启动真实历史事件离线任务").click()
        app.run(timeout=20)
        assert not app.exception
        jobs = api.get("/jobs").json()["jobs"]
        assert len(jobs) == 2
        job = next(row for row in jobs if row["job_id"] != existing["job_id"])
        job_id = job["job_id"]
        for _ in range(200):
            job = api.get(f"/jobs/{job_id}").json()
            if job["status"] in {"complete", "failed"}:
                break
            time.sleep(0.01)
        assert job["status"] == "complete", job
        app.run(timeout=20)
        assert not app.exception
        visible = " ".join(
            str(item.value)
            for collection in (app.markdown, app.caption, app.success)
            for item in collection
        )
        assert "offline_dynamic_substitute" in visible
        assert str(job["elapsed_seconds"]) in visible
        assert job["report_id"] in visible
        assert job["alert_id"] in visible
