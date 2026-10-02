import json
import os
from pathlib import Path

import httpx
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
API = os.getenv("RISKSHIELD_API_URL", "http://127.0.0.1:8000").rstrip("/")
st.set_page_config(page_title="风控盾 · Day 2", page_icon="🛡️", layout="wide")
st.title("风控盾")
st.caption("DAY 2 V2 · 公开事件、来源观察与历史证据工作台")
st.info("Day 1 G1 已通过。Day 2 的受限公开来源采集和证据图检索已初测；G2 尚未签收，五平台在线、语义识别与正式 GraphRAG 仍待验证。")


def request(method, path, **kwargs):
    response = httpx.request(method, API + path, timeout=10, **kwargs)
    response.raise_for_status()
    return response.json()


try:
    health = request("GET", "/health")
    readiness = request("GET", "/readiness")
    cases = request("GET", "/cases")
    channels = request("GET", "/channels/status")
except (httpx.HTTPError, ValueError):
    st.error("后端尚未就绪。请先按 README 启动本地 API，再刷新页面。")
    st.code("uv run uvicorn riskshield.api:create_app --factory --host 127.0.0.1 --port 8000")
    st.stop()

with st.sidebar:
    st.subheader("当前范围")
    st.write("当前执行 Day 2；Day 3—5 未授权")
    st.write("本地 Windows · 历史资料")
    st.caption("导入真实资料不代表已完成实时采集；读取已有证据不代表执行了仿真。")
    if st.button("刷新状态", use_container_width=True):
        st.rerun()

cols = st.columns(4)
cols[0].metric("后端状态", "可用")
cols[1].metric("真实历史案例", readiness["historical_cases"])
cols[2].metric("事件包文件", readiness["available_event_packages"])
cols[3].metric("G1-V2", "未通过" if not readiness["g1_v2_passed"] else "通过")

overview, evidence, gaps = st.tabs(["资源与来源", "历史案例", "Day 1 验收"])
with overview:
    st.subheader("五类来源路线")
    st.caption("此处展示路线/授权核查；只有记录了真实在线获取和覆盖证据，才算平台接入。")
    st.dataframe([
        {"渠道": row["name"], "访问核查": row["status_label"], "案例原帖": row["case_original_records"],
         "下一步": row["next_action"]} for row in channels["channels"]
    ], hide_index=True, use_container_width=True)
    for row in channels["channels"]:
        with st.expander(row["name"] + " · 核查依据"):
            st.write(row["evidence"])
            for url in row["urls"]:
                st.markdown(f"[查看来源]({url})")
    st.subheader("模型资源")
    st.write("已有一次 DeepSeek 纯合成结构化调用通过；当前进程配置状态：" +
             readiness["historical_probe_evidence"]["current_configuration"]["status"] + "。")
    st.caption("历史探测不等于当前凭据可用，也不证明任务决策质量或规模能力。本界面不收集密钥、不自动发送模型请求。")

with evidence:
    st.subheader("公开历史案例")
    st.write("候选：2024 年 3 月众安保险营销骚扰相关报道。资料为人工摘要，历史可见时间未知，截止快照没有合格输入。")
    if st.button("导入随附公开案例", type="primary"):
        payload = json.loads((ROOT / "data/public/za_marketing_2024.json").read_text(encoding="utf-8"))
        try:
            result = request("POST", "/cases", json=payload)
            st.session_state["import_result"] = "已导入" if result["status"] == "imported" else "资料已存在，未重复入库"
            st.rerun()
        except httpx.HTTPError:
            st.error("导入失败，请查看 API 状态和案例版本；不同内容不会覆盖原版本。")
    if st.button("导入首个黑猫事件包"):
        package = json.loads((ROOT / "data/public/blackcat_17374386012_event_v2.json").read_text(encoding="utf-8"))
        try:
            result = request("POST", "/cases", json=package["case_import_projection"])
            st.session_state["blackcat_import_result"] = "已导入" if result["status"] == "imported" else "资料已存在，未重复入库"
            st.rerun()
        except httpx.HTTPError:
            st.error("黑猫事件包导入失败，请查看 API 状态和事件包字段映射。")
    if st.button("导入 Day 1 时间证据事件"):
        package = json.loads((ROOT / "data/public/unh_change_20240222_event_v2.json").read_text(encoding="utf-8"))
        try:
            result = request("POST", "/cases", json=package["case_import_projection"])
            st.session_state["g1_import_result"] = "已导入" if result["status"] == "imported" else "资料已存在，未重复入库"
            st.rerun()
        except httpx.HTTPError:
            st.error("Day 1 事件包导入失败，请查看 API 状态和事件包字段映射。")
    if "import_result" in st.session_state:
        st.success(st.session_state["import_result"])
    if "blackcat_import_result" in st.session_state:
        st.success(st.session_state["blackcat_import_result"])
    if "g1_import_result" in st.session_state:
        st.success(st.session_state["g1_import_result"])
    if cases:
        selected = st.selectbox("选择案例", options=[item["case_id"] for item in cases])
        metadata = next(item for item in cases if item["case_id"] == selected)
        st.write(metadata["title"])
        st.caption(f"截止时间：{metadata['cutoff']} · 数据模式：{metadata['data_mode']} · 版本：{metadata['version']}")
        st.write(metadata["cutoff_basis"])
        snap = request("GET", f"/cases/{selected}/snapshot")
        st.warning("当前资料支持历史输入与 Day 2 受限证据检索，不能据此证明情感、走势或仿真指标达标。")
        st.subheader("截止前输入")
        if not snap["records"]:
            st.write("没有符合截止时间和可知性要求的输入。")
        for row in snap["records"]:
            with st.container(border=True):
                st.markdown(f"**{row['title']}**")
                st.caption(f"{row['publisher']} · {row['published_at']} · {row['content_kind']}")
                st.write(row["summary"])
                st.markdown(f"[原始来源]({row['source_url']})")
                st.caption("；".join(row["limitations"]))
        st.caption(f"排除统计：{snap['excluded_counts']}。排除内容不进入输入快照。")
        if st.checkbox("查看后续证据（仅供评测，不输入 Agent）", key=f"labels_{selected}"):
            labels = request("GET", f"/evaluations/cases/{selected}/evidence")
            for row in labels["records"]:
                st.markdown(f"**{row['published_at']} · {row['title']}**")
                st.write(row["summary"])
                st.markdown(f"[评测侧来源]({row['source_url']})")
            st.caption(labels["note"])
    else:
        st.write("尚未导入案例。可导入随附资料开始检查时间快照。")

with gaps:
    st.subheader("G1-V2 已通过；G2-V2 尚未签收")
    st.success("已具备：可运行 API、SQLite 持久化、案例导入、时间过滤与结构化契约。")
    st.caption("以有同期抓取证据的替代真实事件签收；黑猫原投诉仍无合格截止输入。以下为后续阶段缺口，不代表 Day 1 未通过。")
    for blocker in readiness["blockers"]:
        st.warning(blocker)
    st.write("DailyComplaint 是遗留可选契约，不是新版主链前置；完整仿真与预警仍属于后续工程范围。")
    st.markdown(f"[查看接口文档]({API}/docs)")
