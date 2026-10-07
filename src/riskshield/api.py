import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from riskshield.day2 import (CollectRequest, CollectionError, Day2Pipeline,
                             GraphQuery, LabelRequest)
from riskshield.day3 import Day3Simulation, SimulationCreate, SimulationError
from riskshield.day4_alerts import Day4Alerts
from riskshield.day4_report import Day4Reports
from riskshield.day4_tasks import Day4Tasks, TaskError
from riskshield.model_gateway import ModelUnavailable, model_status, probe_model
from riskshield.schemas import AgentDecision, CaseImport, DailyComplaint
from riskshield.store import ConflictError, Store

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RunReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(pattern=r"^run_[a-f0-9]{20}$")


class DeliveryPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Literal["wecom", "email"]


class DemoRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_count: int = Field(default=10, ge=1, le=100)
    rounds: int = Field(default=3, ge=1, le=10)
    concurrency: int = Field(default=4, ge=1, le=32)


def create_app(db_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="风控盾 · Day 4 V2", version="0.5.0",
                  description="公开事件、截止证据图、仿真和离线报告预警工程工作台。")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver", "api"])
    store = Store(db_path or os.getenv("RISKSHIELD_DB", str(PROJECT_ROOT / "runtime/riskshield.db")))
    day2 = Day2Pipeline(store)
    day3 = Day3Simulation(store)
    reports = Day4Reports(store)
    alerts = Day4Alerts(store)
    tasks = Day4Tasks(store)

    @app.get("/health")
    def health():
        return {"status": "ok", "stage": "day4_v2", "version": "0.5.0"}

    @app.get("/readiness")
    def readiness():
        cases = store.list_cases()
        real = [case for case in cases if case["data_mode"] == "real_historical"]
        event_packages = list((PROJECT_ROOT / "data/public").glob("*_event_v2.json"))
        # Project-level Day 1 evidence gate: see docs/records/06-Day1执行与验收记录.md.
        # This does not imply that this database has imported a case or that later capabilities work.
        return {
            "stage": "day4_v2", "g1_passed": True, "g1_v2_passed": True,
            "g2_v2_passed": True, "g2_v2_status": "internal_restricted_passed",
            "g3_v2_passed": False, "g4_v2_passed": False,
            "engineering_scaffold": "available",
            "historical_cases": len(real), "available_event_packages": len(event_packages),
            "legacy_daily_complaint_contract": {"available": True, "required_for_v2": False},
            "historical_probe_evidence": {"status": "passed_once_synthetic", "current_configuration": model_status()},
            "capabilities": {"historical_import": True, "cutoff_snapshot": True,
                             "restricted_source_collection": True, "sentiment_label_submission": True,
                             "evidence_graph_retrieval": True, "sentiment_analysis": False,
                             "graph_rag": False, "long_term_memory": True,
                             "dynamic_simulation_engine": True,
                             "real_model_simulation_verified": False,
                             "synthetic_500x30_real_model_attempt_recorded": True,
                             "offline_five_module_report": True,
                             "provisional_four_level_alert": True,
                             "offline_notification_preview": True,
                             "offline_dynamic_task_execution": True,
                             "direction_evaluation": False,
                             "realtime_collection": False, "forecast": False},
            "blockers": ["尚无五类来源的在线采集、覆盖与延迟证据",
                         "当前仅有 SEC 与同期帖子存档的受限采集，未建立生产级多平台采集",
                         "情感三分类模型及独立复核标签尚未完成",
                         "新版情感/走势正式评测集及观察窗口径尚未冻结",
                         "真实来源与完整规模仿真尚未在同一案例贯通；消息动作因果尚未证实",
                         "真实通知发送、送达与红警时效尚未验证",
                         "域内模型、嵌入和图服务资源尚未核实"],
        }

    @app.get("/channels/status")
    def channels():
        path = PROJECT_ROOT / "data/research/channel_checks_v2.json"
        return json.loads(path.read_text(encoding="utf-8"))

    @app.get("/contracts")
    def contracts():
        return {"case_import": CaseImport.model_json_schema(),
                "daily_complaint": DailyComplaint.model_json_schema(),
                "agent_decision": AgentDecision.model_json_schema(),
                "simulation_create": SimulationCreate.model_json_schema()}

    @app.post("/cases")
    def import_case(case: CaseImport):
        try:
            return store.import_case(case)
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.get("/cases")
    def cases():
        return store.list_cases()

    @app.get("/cases/{case_id}")
    def case_metadata(case_id: str):
        try:
            return store.get_case(case_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="案例不存在") from None

    @app.get("/cases/{case_id}/snapshot")
    def snapshot(case_id: str):
        try:
            return store.snapshot(case_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="案例不存在") from None

    @app.get("/evaluations/cases/{case_id}/evidence")
    def evaluation_evidence(case_id: str):
        try:
            rows = store.records(case_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="案例不存在") from None
        return {"purpose": "evaluation_only", "not_for_agent_context": True,
                "records": [row.model_dump(mode="json") for row in rows if row.role == "evaluation_only"],
                "note": "公开报道不等于完整真值，不能据此计算投诉准确率。"}

    @app.get("/models/status")
    def models():
        return model_status()

    @app.post("/models/probe")
    def model_probe():
        try:
            return probe_model()
        except ModelUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

    @app.post("/cases/{case_id}/observations/collect")
    def collect_observation(case_id: str, request: CollectRequest):
        try:
            return day2.collect(case_id, request)
        except KeyError:
            raise HTTPException(status_code=404, detail="案例或来源记录不存在") from None
        except CollectionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get("/cases/{case_id}/observations")
    def observations(case_id: str):
        try:
            return day2.observations(case_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="案例不存在") from None

    @app.post("/cases/{case_id}/labels")
    def add_label(case_id: str, request: LabelRequest):
        try:
            return day2.label(case_id, request)
        except KeyError:
            raise HTTPException(status_code=404, detail="案例或来源记录不存在") from None
        except CollectionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get("/cases/{case_id}/labels")
    def labels(case_id: str):
        try:
            return day2.labels(case_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="案例不存在") from None

    @app.post("/cases/{case_id}/graph")
    def build_graph(case_id: str):
        try:
            return day2.build_graph(case_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="案例不存在") from None
        except CollectionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get("/graphs/{graph_id}")
    def graph(graph_id: str):
        try:
            return day2.graph(graph_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="图谱不存在") from None

    @app.post("/graphs/{graph_id}/query")
    def graph_query(graph_id: str, request: GraphQuery):
        try:
            return day2.query_graph(graph_id, request.question)
        except KeyError:
            raise HTTPException(status_code=404, detail="图谱不存在") from None

    @app.post("/simulations", status_code=201)
    def create_simulation(request: SimulationCreate):
        try:
            run_id = day3.create_run(
                request.case_id, request.graph_id, agent_count=request.agent_count,
                rounds=request.rounds, seed=request.seed, concurrency=request.concurrency,
                budget_cny=request.budget_cny, role_offset=request.role_offset,
            )
            return day3.summary(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="案例或证据图不存在") from None
        except SimulationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get("/simulations")
    def simulations():
        with store.connect() as db:
            run_ids = [row["run_id"] for row in db.execute(
                "SELECT run_id FROM simulation_runs ORDER BY created_at DESC")]
        return {"runs": [day3.summary(run_id) for run_id in run_ids]}

    @app.get("/simulations/{run_id}")
    def simulation(run_id: str):
        try:
            return day3.summary(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="仿真运行不存在") from None

    @app.get("/simulations/{run_id}/trajectory")
    def simulation_trajectory(run_id: str):
        try:
            return {"run_id": run_id, "actions": day3.trajectory(run_id)}
        except KeyError:
            raise HTTPException(status_code=404, detail="仿真运行不存在") from None

    @app.post("/demos/day4/run", status_code=201)
    def prepare_day4_demo(request: DemoRunRequest):
        try:
            return tasks.prepare_demo_run(
                agent_count=request.agent_count, rounds=request.rounds,
                concurrency=request.concurrency,
            )
        except (TaskError, SimulationError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.post("/jobs", status_code=202)
    def start_job(request: RunReference):
        try:
            return tasks.start(request.run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="仿真运行不存在") from None
        except TaskError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get("/jobs")
    def jobs():
        return {"jobs": tasks.list()}

    @app.get("/jobs/{job_id}")
    def job(job_id: str):
        try:
            return tasks.get(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="本机任务不存在") from None

    @app.post("/reports", status_code=201)
    def build_report(request: RunReference):
        try:
            assessment = alerts.assess(request.run_id)
            return reports.build(request.run_id, risk_assessment=assessment)
        except KeyError:
            raise HTTPException(status_code=404, detail="仿真运行不存在") from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get("/reports/{report_id}")
    def report(report_id: str):
        try:
            return reports.get(report_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="报告不存在") from None

    @app.post("/alerts/assess", status_code=201)
    def assess_alert(request: RunReference):
        try:
            return alerts.assess(request.run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="仿真运行不存在") from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get("/alerts/{alert_id}")
    def alert(alert_id: str):
        try:
            return alerts.get(alert_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="预警不存在") from None

    @app.post("/alerts/{alert_id}/previews", status_code=201)
    def preview_delivery(alert_id: str, request: DeliveryPreviewRequest):
        try:
            return alerts.preview_delivery(alert_id, request.channel)
        except KeyError:
            raise HTTPException(status_code=404, detail="预警不存在") from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    return app
