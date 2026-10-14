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
from riskshield.tasks import Day4Tasks, OfflineDynamicSubstitute
from riskshield.model_gateway import ReportQuestionModel
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
        imported_case = api.get("/cases").json()[0]
        assert imported_case["case_id"] == "unh_change_20240222_day2_multisource"
        snapshot = api.get(f"/cases/{imported_case['case_id']}/snapshot").json()
        assert len(snapshot["records"]) == 2
        assert snapshot["excluded_counts"]["not_input"] == 1
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
        assert next(item for item in app.metric if item.label == "参与智能体").value == "0"
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
        assert "自然语言追问" in " ".join(item.value for item in app.markdown)
        assert next(item for item in app.button if item.label == "提交追问").disabled
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


def test_ui_switches_tasks_and_keeps_report_graph_and_alert_run_bound(tmp_path, monkeypatch):
    store = Store(tmp_path / "bindings.db")
    tasks = Day4Tasks(store)
    demo = tasks.prepare_demo_run(agent_count=2, rounds=1, concurrency=2)
    demo_job = tasks.run_sync(demo["run_id"])
    package = json.loads((Path(__file__).resolve().parents[1] / "data/public/"
                          "unh_change_20240222_day2_multisource_event.json").read_text(encoding="utf-8"))
    case = CaseImport.model_validate(package["case_import_projection"])
    store.import_case(case)
    historical = tasks.start_historical_case(case.case_id, agent_count=2, rounds=1, concurrency=2)
    historical_job = tasks.wait(historical["job_id"], timeout=10)
    assert demo_job["status"] == historical_job["status"] == "complete"

    with TestClient(create_app(store.path)) as api:
        calls = []

        def local_request(method, url, **kwargs):
            kwargs.pop("timeout", None)
            path = url.removeprefix("http://127.0.0.1:8000")
            calls.append((method, path))
            return api.request(method, path, **kwargs)

        monkeypatch.setenv("RISKSHIELD_API_URL", "http://127.0.0.1:8000")
        monkeypatch.setattr(httpx, "request", local_request)
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "ui/app.py"))
        app.run(timeout=20)
        next(item for item in app.radio if item.key == "queue_selected_run_widget").set_value(
            demo["run_id"]).run(timeout=20)
        assert app.session_state["selected_run_id"] == demo["run_id"]
        next(item for item in app.button if item.label == "打开关联报告").click().run(timeout=20)
        assert not app.exception
        assert next(item for item in app.selectbox if item.key == "report_run").value == demo["run_id"]
        assert app.session_state["report_graph_id"] == demo["graph_id"]

        calls.clear()
        next(item for item in app.text_input if item.label == "查看报告 ID").set_value(
            historical_job["report_id"]).run(timeout=20)
        assert not app.exception
        assert ("GET", f"/graphs/{historical_job['graph_id']}") in calls
        assert ("GET", f"/graphs/{demo['graph_id']}") not in calls
        next(item for item in app.text_input if item.label == "来源证据检索").set_value(
            "Change Healthcare").run(timeout=20)
        next(item for item in app.button if item.label == "检索带引用证据").click().run(timeout=20)
        assert app.session_state["evidence_query_result"]["graph_id"] == historical_job["graph_id"]

        set_page(app, "处置队列")
        next(item for item in app.button if item.label == "打开关联预警").click().run(timeout=20)
        assert next(item for item in app.selectbox if item.key == "alert_run_widget").value == demo["run_id"]
        next(item for item in app.selectbox if item.key == "alert_run_widget").set_value(
            historical_job["run_id"]).run(timeout=20)
        assert app.session_state["alert_run_id"] == historical_job["run_id"]
        next(item for item in app.button if item.label == "按当前规则判级").click().run(timeout=20)
        assert not app.exception
        alert = api.get(f"/alerts/{app.session_state['alert_id']}").json()
        assert alert["run_id"] == historical_job["run_id"]


def test_ui_completed_rounds_with_failed_decisions_are_partial(tmp_path, monkeypatch):
    """A controlled offline failure must stay visible even when all rounds end."""
    tasks = Day4Tasks(Store(tmp_path / "partial.db"))
    run = tasks.prepare_demo_run(agent_count=2, rounds=1, concurrency=2)

    def fail_decision(self, observation):
        raise RuntimeError("controlled offline UI test failure")

    monkeypatch.setattr(OfflineDynamicSubstitute, "decide", fail_decision)
    job = tasks.run_sync(run["run_id"])
    assert job["result_state"] == "partial"
    assert job["simulation_status"] == "complete"
    with TestClient(create_app(tasks.store.path)) as api:
        def local_request(method, url, **kwargs):
            kwargs.pop("timeout", None)
            return api.request(method, url.removeprefix("http://127.0.0.1:8000"), **kwargs)

        monkeypatch.setenv("RISKSHIELD_API_URL", "http://127.0.0.1:8000")
        monkeypatch.setattr(httpx, "request", local_request)
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "ui/app.py"))
        app.run(timeout=20)
        next(item for item in app.radio if item.key == "queue_filter").set_value(
            "部分完成").run(timeout=20)
        assert not app.exception
        metrics = {item.label: str(item.value) for item in app.metric}
        assert metrics["有效决策"] == metrics["参与智能体"] == "0"
        assert metrics["失败决策"] == "2"
        assert any("任务部分完成" in item.value for item in app.error)
        assert next(item for item in app.button if item.label == "打开关联报告").disabled
        assert next(item for item in app.button if item.label == "打开关联预警").disabled

        # A synthetic reservation larger than the budget stops before any decision dispatch.
        monkeypatch.setattr(OfflineDynamicSubstitute, "reservation_cny", Decimal("100"))
        stopped_run = tasks.prepare_demo_run(agent_count=2, rounds=2, concurrency=2)
        stopped_job = tasks.run_sync(stopped_run["run_id"])
        assert stopped_job["result_state"] == "stopped"
        assert stopped_job["action_counts"]["not_dispatched"] == 2
        app.run(timeout=20)
        next(item for item in app.radio if item.key == "queue_selected_run_widget").set_value(
            stopped_run["run_id"]).run(timeout=20)
        assert not app.exception
        assert any("任务已停止" in item.value for item in app.error)
        assert next(item for item in app.metric if item.label == "参与智能体").value == "0"
        assert next(item for item in app.button if item.label == "打开关联报告").disabled


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
        next(item for item in app.radio if item.key == "queue_filter").set_value("失败").run(timeout=20)
        assert not app.exception
        assert next(item for item in app.radio if item.key == "queue_selected_run_widget").value == run["run_id"]


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


def test_ui_multiturn_questions_and_report_switch_use_separate_conversations(tmp_path, monkeypatch):
    tasks = Day4Tasks(Store(tmp_path / "ui-questions.db"))
    jobs = [tasks.run_sync(tasks.prepare_demo_run(agent_count=2, rounds=1, concurrency=2)["run_id"])
            for _ in range(2)]
    monkeypatch.setenv("RISKSHIELD_ENABLE_REPORT_QA", "1")
    monkeypatch.setenv("RISKSHIELD_REPORT_QA_GRANT_ID", "offline-ui-question-test")
    monkeypatch.setenv("RISKSHIELD_REPORT_QA_ALLOWED_CASE_IDS", jobs[0]["case_id"])
    monkeypatch.setenv("DASHSCOPE_API_KEY", "offline-ui-fixture-key")
    prompts = []

    def model(self, messages):
        prompts.append(messages)
        card = json.loads(messages[0]["content"].split("冻结资料：", 1)[1])["evidence_cards"][0]
        answer = ({"answer_status": "insufficient_evidence", "answer": "该未来信息不在截止前资料中，保留弃权。", "citations": []}
                  if len(prompts) > 1 else
                  {"answer_status": "supported", "answer": "问答组件受控测试回答，带原文引用。",
                   "citations": [{"evidence_id": card["evidence_id"], "quote": card["text"][:30]}]})
        return {"content": json.dumps(answer), "model": "qwen-turbo-offline-ui-fixture",
                "usage": {"prompt_tokens": 20, "completion_tokens": 10}, "cost_cny": "0.000012"}

    monkeypatch.setattr(ReportQuestionModel, "complete", model)
    with TestClient(create_app(tasks.store.path)) as api:
        def local_request(method, url, **kwargs):
            kwargs.pop("timeout", None)
            return api.request(method, url.removeprefix("http://127.0.0.1:8000"), **kwargs)

        monkeypatch.setenv("RISKSHIELD_API_URL", "http://127.0.0.1:8000")
        monkeypatch.setattr(httpx, "request", local_request)
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "ui/app.py")).run(timeout=20)
        set_page(app, "五模块报告")
        next(item for item in app.text_input if item.label == "查看报告 ID").set_value(jobs[0]["report_id"]).run(timeout=20)
        next(item for item in app.text_input if item.label == "自然语言问题").set_value("截至截止时间有什么来源？").run(timeout=20)
        next(item for item in app.button if item.label == "提交追问").click().run(timeout=20)
        assert not app.exception
        assert any("问答组件受控测试回答" in item.value for item in app.text)
        assert any("核对追问引用来源" in item.value for item in app.markdown)
        app.run(timeout=20)
        assert len(prompts) == 1  # A rerender must not launch another charged call.
        next(item for item in app.text_input if item.label == "自然语言问题").set_value("它们后来恢复了吗？").run(timeout=20)
        next(item for item in app.button if item.label == "提交追问").click().run(timeout=20)
        assert not app.exception and len(prompts[1]) == 4
        assert any("保留弃权" in item.value for item in app.info)
        next(item for item in app.text_input if item.label == "查看报告 ID").set_value(jobs[1]["report_id"]).run(timeout=20)
        assert not app.exception
        assert not any("问答组件受控测试回答" in item.value for item in app.text)
        next(item for item in app.text_input if item.label == "查看报告 ID").set_value(jobs[0]["report_id"]).run(timeout=20)
        assert any("问答组件受控测试回答" in item.value for item in app.text)
