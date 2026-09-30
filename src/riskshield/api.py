import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from riskshield.model_gateway import ModelUnavailable, model_status, probe_model
from riskshield.schemas import AgentDecision, CaseImport, DailyComplaint
from riskshield.store import ConflictError, Store

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def create_app(db_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="风控盾 · Day 1", version="0.1.0",
                  description="历史资料与输入契约。尚未实现 NLP、动态仿真或投诉预测。")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])
    store = Store(db_path or os.getenv("RISKSHIELD_DB", str(PROJECT_ROOT / "runtime/riskshield.db")))

    @app.get("/health")
    def health():
        return {"status": "ok", "stage": "day1", "version": "0.1.0"}

    @app.get("/readiness")
    def readiness():
        cases = store.list_cases()
        real = [case for case in cases if case["data_mode"] == "real_historical"]
        return {
            "stage": "day1", "g1_passed": False, "engineering_scaffold": "available",
            "historical_cases": len(real),
            "real_daily_complaints": "missing",
            "model": model_status(),
            "capabilities": {"historical_import": True, "cutoff_snapshot": True,
                             "nlp": False, "dynamic_simulation": False, "forecast": False},
            "blockers": ["尚无真实逐日投诉数据及确认口径", "尚未完成获准模型的真实决策试验",
                         "案例只有回溯获取的公开材料，缺当时快照与完整后续标签", "官方指标口径仍待确认"],
        }

    @app.get("/channels/status")
    def channels():
        return json.loads((PROJECT_ROOT / "data/research/channel_checks.json").read_text(encoding="utf-8"))

    @app.get("/contracts")
    def contracts():
        return {"case_import": CaseImport.model_json_schema(),
                "daily_complaint": DailyComplaint.model_json_schema(),
                "agent_decision": AgentDecision.model_json_schema()}

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

    return app
