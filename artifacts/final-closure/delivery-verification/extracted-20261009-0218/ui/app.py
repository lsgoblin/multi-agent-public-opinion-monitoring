import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
API = os.getenv("RISKSHIELD_API_URL", "http://127.0.0.1:8000").rstrip("/")
DOCS_API = "http://127.0.0.1:8000" if API == "http://api:8000" else API
st.set_page_config(page_title="风控盾 · 风险研判与预警", page_icon="🛡️", layout="wide")
st.markdown("""
<style>
:root { --paper:#f2f1eb; --surface:#fffefa; --ink:#20262b; --muted:#6d767a; --line:#deded6; --blue:#2d56cf; --blue-wash:#edf1ff; --green:#39724e; --orange:#bd6128; }
[data-testid="stAppViewContainer"] { background:var(--paper); color:var(--ink); }
[data-testid="stHeader"] { background:transparent; }
[data-testid="stSidebar"] { background:#e9e8e1; border-right:1px solid #d9d8cf; width:248px !important; min-width:248px !important; }
[data-testid="stSidebar"] > div { width:248px !important; }
[data-testid="stSidebar"] [data-testid="stRadio"] [data-testid="stRadioOption"] { background:transparent; border:1px solid transparent; border-radius:5px; padding:.48rem .65rem; transition:background .16s ease,border-color .16s ease; }
[data-testid="stSidebar"] [data-testid="stRadio"] [data-testid="stRadioOption"][data-selected="true"] { background:var(--blue-wash); border-color:#c4cff3; color:var(--blue); }
[data-testid="stSidebar"] [data-testid="stRadio"] [data-testid="stRadioOption"]:hover { background:#f5f4ee; border-color:#d9d8cf; }
[data-testid="stSidebar"] [data-testid="stRadio"] input[type="radio"] { accent-color:var(--blue); }
.sidebar-brand { display:flex; align-items:center; gap:.65rem; margin:.15rem 0 .25rem; color:var(--ink); font-size:1.12rem; font-weight:650; letter-spacing:-.03em; }
.brand-mark { display:inline-flex; width:30px; height:30px; align-items:center; justify-content:center; border-radius:7px; background:var(--blue); color:white; font-size:.9rem; }
.sidebar-section-label { margin:.85rem 0 .4rem; color:var(--muted); font-size:.7rem; font-weight:700; letter-spacing:.09em; }
h1 { font-size:1.75rem !important; font-weight:600 !important; letter-spacing:-.035em; }
h2, h3 { color:var(--ink); letter-spacing:-.02em; }
[data-testid="stMainBlockContainer"] { padding-top:2.5rem; padding-bottom:2rem; max-width:1480px; }
[data-testid="stTabs"] [role="tablist"] { gap:1.2rem; border-bottom:1px solid var(--line); }
[data-testid="stTabs"] [data-testid="stTab"] { color:var(--muted); padding:0 0 .7rem; }
[data-testid="stTabs"] [data-testid="stTab"][aria-selected="true"] { color:var(--ink); border-bottom-color:transparent !important; }
[data-testid="stTabs"] [data-testid="stTab"][aria-selected="true"] .react-aria-SelectionIndicator { background:var(--blue) !important; }
[data-testid="stVerticalBlockBorderWrapper"] { background:var(--surface); border-color:var(--line) !important; border-radius:6px; }
[data-testid="stMetric"] { background:var(--surface); border:1px solid var(--line); border-radius:6px; padding:.8rem 1rem; }
[data-testid="stMetricLabel"] { color:var(--muted); }
[data-testid="stMetricValue"] { color:var(--ink); }
button[kind="primary"] { background:var(--blue); border-color:var(--blue); }
[data-testid="stRadio"] [role="radiogroup"] { gap:.45rem; }
[data-testid="stRadio"] [data-testid="stRadioOption"] { background:var(--surface); border:1px solid var(--line); border-radius:5px; padding:.55rem .75rem; transition:background .16s ease,border-color .16s ease,transform .16s ease; }
[data-testid="stRadio"] [data-testid="stRadioOption"][data-selected="true"] { background:var(--blue-wash); border-color:#9aaeea; color:var(--ink); }
[data-testid="stRadio"] [data-testid="stRadioOption"][data-selected="true"] > div > div:first-child { background:var(--blue) !important; }
[data-testid="stRadio"] [data-testid="stRadioOption"]:hover { border-color:#aeb5c2; transform:translateX(2px); }
[data-testid="stRadio"] input[type="radio"] { accent-color:var(--blue); }
[data-testid="stProgressBar"] > div > div { background:var(--blue); }
.status-pill { display:inline-flex; align-items:center; border-radius:4px; padding:.2rem .55rem; background:var(--blue-wash); color:var(--blue); font-size:.78rem; }
@media (prefers-reduced-motion: reduce) { *,*::before,*::after { transition-duration:.01ms !important; animation-duration:.01ms !important; } }
</style>
""", unsafe_allow_html=True)
def request(method, path, **kwargs):
    response = httpx.request(method, API + path, timeout=10, **kwargs)
    response.raise_for_status()
    return response.json()


def show_api_error(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            detail = exc.response.json().get("detail", "请求失败")
        except ValueError:
            detail = "请求失败"
        st.error(f"API 返回 {exc.response.status_code}：{detail}")
    else:
        st.error("本地 API 请求失败，请检查服务状态。")


def local_time(value):
    try:
        return datetime.fromisoformat(value).astimezone(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m-%d %H:%M:%S %z"
        )
    except (TypeError, ValueError):
        return str(value or "未记录")


def show_graph(graph):
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if not nodes:
        st.write("当前证据图没有可显示的节点。")
        return
    lines = ["digraph evidence {", 'rankdir="LR";', 'node [shape="box"];']
    for node in nodes:
        node_id = json.dumps(str(node.get("id", "")), ensure_ascii=False)
        label = json.dumps(str(node.get("label") or node.get("id", ""))[:80], ensure_ascii=False)
        lines.append(f"{node_id} [label={label}];")
    for edge in edges:
        source = json.dumps(str(edge.get("from", "")), ensure_ascii=False)
        target = json.dumps(str(edge.get("to", "")), ensure_ascii=False)
        relation = json.dumps(str(edge.get("relation", "")), ensure_ascii=False)
        lines.append(f"{source} -> {target} [label={relation}];")
    lines.append("}")
    st.graphviz_chart("\n".join(lines), use_container_width=True)
    st.caption(f"来源证据图：{len(nodes)} 个证据节点、{len(edges)} 条关系；这是截止快照的来源结构，不是实际社交传播图。")


def show_propagation_graph(edges, report_id):
    """Render the run's simulated messages separately from the source evidence graph."""
    if not edges:
        st.info("本次运行没有模拟消息关系。")
        return
    nodes = sorted({edge.get("sender_id") for edge in edges} |
                   {edge.get("recipient_id") for edge in edges})
    lines = ["digraph simulated_propagation {", 'rankdir="LR";', 'node [shape="box"];']
    for node_id in nodes:
        label = json.dumps(str(node_id), ensure_ascii=False)
        lines.append(f"{label} [label={label}];")
    for edge in edges:
        sender = json.dumps(str(edge.get("sender_id", "")), ensure_ascii=False)
        recipient = json.dumps(str(edge.get("recipient_id", "")), ensure_ascii=False)
        label = json.dumps(f"{edge.get('message_count', 0)} 条消息", ensure_ascii=False)
        lines.append(f"{sender} -> {recipient} [label={label}];")
    lines.append("}")
    st.graphviz_chart("\n".join(lines), use_container_width=True)
    st.caption(f"模拟传播图：{len(nodes)} 个智能体、{len(edges)} 条发送关系。每条关系保留消息和发送动作引用；活动量不证明因果影响。")


def queue_navigation(page, **values):
    for key, value in values.items():
        st.session_state[key] = value
        if key == "report_id":
            st.session_state["report_id_input"] = value or ""
        if key == "alert_id":
            st.session_state["alert_id_input"] = value or ""
    st.session_state["pending_navigation"] = page
    st.rerun()


try:
    health = request("GET", "/health")
    readiness = request("GET", "/readiness")
    cases = request("GET", "/cases")
    channels = request("GET", "/channels/status")
except (httpx.HTTPError, ValueError):
    st.error("后端尚未就绪。请先按 README 启动本地 API，再刷新页面。")
    st.code("uv run uvicorn riskshield.api:create_app --factory --host 127.0.0.1 --port 8000")
    st.stop()

pending_navigation = st.session_state.pop("pending_navigation", None)
if pending_navigation:
    st.session_state["main_navigation"] = pending_navigation

with st.sidebar:
    st.markdown("<div class='sidebar-brand'><span class='brand-mark'>盾</span><span>风控盾</span></div>", unsafe_allow_html=True)
    st.caption("风险研判与预警工作台 · 证据核查 · 仿真研判")
    st.markdown("<div class='sidebar-section-label'>工作区</div>", unsafe_allow_html=True)
    active_page = st.radio(
        "页面导航",
        ["处置队列", "今日概览", "事件证据", "五模块报告", "预警中心", "关口状态"],
        index=0,
        label_visibility="collapsed",
        key="main_navigation",
    )
    st.divider()
    st.markdown("<div class='sidebar-section-label'>运行边界</div>", unsafe_allow_html=True)
    st.markdown("<span class='status-pill'>LOCAL · OFFLINE</span>", unsafe_allow_html=True)
    st.caption("仅限获准的本机工作流；不发送真实通知、不外发材料。")
    st.markdown("<div class='sidebar-section-label'>验收关口</div>", unsafe_allow_html=True)
    st.markdown("**G1 / G2**　内部受限通过")
    st.markdown("**G3**　尚未通过")
    st.caption("G4 尚未通过；本机离线证据不替代真实运行验收。")

header_title, header_action = st.columns([5, 1], vertical_alignment="center")
with header_title:
    st.title("风控盾")
    st.caption(f"{active_page} · 证据、仿真、离线报告与预警工作台")
with header_action:
    if st.button("刷新状态", use_container_width=True):
        st.rerun()

if active_page == "今日概览":
    cols = st.columns(4)
    cols[0].metric("后端状态", "可用")
    cols[1].metric("真实历史案例", readiness["historical_cases"])
    cols[2].metric("事件包文件", readiness["available_event_packages"])
    cols[3].metric("G1-V2", "未通过" if not readiness["g1_v2_passed"] else "通过")
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

if active_page == "事件证据":
    st.subheader("公开历史案例")
    st.write("候选：2024 年 3 月众安保险营销骚扰相关报道。资料为人工摘要，历史可见时间未知，截止快照没有合格输入。")
    imports = (
        ("导入随附公开案例", "za_marketing_2024.json", "import_result", None,
         "导入失败，请查看 API 状态和案例版本；不同内容不会覆盖原版本。"),
        ("导入首个黑猫事件包", "blackcat_17374386012_event_v2.json", "blackcat_import_result",
         "case_import_projection", "黑猫事件包导入失败，请查看 API 状态和事件包字段映射。"),
        ("导入多来源历史事件", "unh_change_20240222_event_v2.json", "g1_import_result",
         "case_import_projection", "多来源历史事件导入失败，请查看 API 状态和事件包字段映射。"),
    )
    for label, filename, result_key, projection, error in imports:
        if st.button(label, type="primary" if projection is None else "secondary"):
            package = json.loads((ROOT / "data/public" / filename).read_text(encoding="utf-8"))
            try:
                result = request("POST", "/cases", json=package[projection] if projection else package)
                st.session_state[result_key] = "已导入" if result["status"] == "imported" else "资料已存在，未重复入库"
                st.rerun()
            except httpx.HTTPError:
                st.error(error)
    for _, _, result_key, _, _ in imports:
        if result_key in st.session_state:
            st.success(st.session_state[result_key])
    if cases:
        selected = st.selectbox("选择案例", options=[item["case_id"] for item in cases])
        metadata = next(item for item in cases if item["case_id"] == selected)
        st.write(metadata["title"])
        st.caption(f"截止时间：{metadata['cutoff']} · 数据模式：{metadata['data_mode']} · 版本：{metadata['version']}")
        st.write(metadata["cutoff_basis"])
        snap = request("GET", f"/cases/{selected}/snapshot")
        st.warning("当前资料支持历史输入与受限证据检索，不能据此证明情绪、走势或仿真指标达标。")
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
        if st.checkbox("查看后续证据（仅供评测，不输入模拟智能体）", key=f"labels_{selected}"):
            labels = request("GET", f"/evaluations/cases/{selected}/evidence")
            for row in labels["records"]:
                st.markdown(f"**{row['published_at']} · {row['title']}**")
                st.write(row["summary"])
                st.markdown(f"[评测侧来源]({row['source_url']})")
            st.caption(labels["note"])
    else:
        st.write("尚未导入案例。可导入随附资料开始检查时间快照。")


try:
    run_rows = request("GET", "/simulations").get("runs", [])
except (httpx.HTTPError, ValueError) as exc:
    run_rows = []
    run_list_error = exc
else:
    run_list_error = None

try:
    job_rows = request("GET", "/jobs").get("jobs", [])
except (httpx.HTTPError, ValueError) as exc:
    job_rows = []
    job_list_error = exc
else:
    job_list_error = None

if active_page == "处置队列":
    st.subheader("处置队列")
    st.caption("按状态查看本机任务；选择一项后，在右侧核对轮次、执行状态与关联产物。")

    queue_panel, detail_panel = st.columns([0.9, 1.1], gap="large")
    selected_run = None
    selected_run_id = None
    run_status_labels = {
        "created": "待启动", "running": "运行中", "complete": "已完成", "failed": "失败",
    }
    case_titles = {item["case_id"]: item.get("title", item["case_id"]) for item in cases}
    case_titles["day4_demo_fictional_synthetic_v1"] = "虚构 Harborlight 保险服务演练"

    with queue_panel:
        st.markdown("#### 当前队列")
        if run_list_error:
            show_api_error(run_list_error)
        if run_rows:
            if st.session_state.pop("reset_queue_filter", False):
                st.session_state["queue_filter"] = "全部"
            filter_labels = ["全部", "运行中", "待启动", "已完成", "失败"]
            status_for_filter = {"运行中": "running", "待启动": "created",
                                 "已完成": "complete", "失败": "failed"}
            queue_filter = st.radio(
                "任务状态", filter_labels, horizontal=True, key="queue_filter",
                label_visibility="collapsed",
            )
            filtered_runs = [
                row for row in run_rows
                if queue_filter == "全部" or row.get("status") == status_for_filter[queue_filter]
            ]
            visible_run_ids = [row["run_id"] for row in filtered_runs if row.get("run_id")]
            if visible_run_ids:
                if st.session_state.get("selected_run_id") not in visible_run_ids:
                    st.session_state["selected_run_id"] = visible_run_ids[0]
                st.session_state["queue_selected_run_widget"] = st.session_state["selected_run_id"]

                def run_label(run_id):
                    row = next(item for item in filtered_runs if item["run_id"] == run_id)
                    config = row.get("config") or {}
                    completed = row.get("completed_rounds") or 0
                    planned = config.get("rounds") or 0
                    status = run_status_labels.get(row.get("status"), row.get("status", "未知"))
                    title = case_titles.get(row.get("case_id"), row.get("case_id", "未命名案例"))
                    return f"{title}　·　{status}　·　{completed}/{planned} 轮"

                selected_run_id = st.radio(
                    "选择任务", visible_run_ids,
                    index=visible_run_ids.index(st.session_state["selected_run_id"]),
                    format_func=run_label, key="queue_selected_run_widget",
                    label_visibility="collapsed",
                )
                st.session_state["selected_run_id"] = selected_run_id
                st.caption(f"{len(visible_run_ids):02d} 项 · 默认按最近创建顺序显示")
                selected_run = next(row for row in filtered_runs if row["run_id"] == selected_run_id)
            else:
                st.info("这个状态下暂时没有任务。切换筛选可查看其他事项。")
        else:
            st.info("队列暂无任务。可准备一条纯合成离线演示，或在下方填写本地任务配置。")
            st.caption("历史 500×30 纯合成规模记录保存在独立研究数据中，不会自动导入此队列。")

        with st.expander("准备或创建任务", expanded=not run_rows):
            st.markdown("**纯合成离线演示**")
            st.write("10 个模拟智能体 × 3 轮 · 动态离线替身 · 云模型调用 0 · 外部网络请求 0")
            if st.button("准备离线演示任务", type="primary", use_container_width=True):
                try:
                    demo_run = request("POST", "/demos/day4/run", json={
                        "agent_count": 10, "rounds": 3, "concurrency": 4,
                    })
                    st.session_state["selected_run_id"] = demo_run["run_id"]
                    st.session_state["demo_prepared"] = demo_run["run_id"]
                    st.session_state["reset_queue_filter"] = True
                    st.rerun()
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    show_api_error(exc)
            if st.session_state.get("demo_prepared"):
                st.caption(f"演示任务已准备：{st.session_state['demo_prepared']}。可从队列选择并启动。")

            if cases:
                st.divider()
                st.markdown("**手工配置**")
                case_id = st.selectbox("任务案例", [item["case_id"] for item in cases], key="task_case")
                selected_case = next(item for item in cases if item["case_id"] == case_id)
                if selected_case["data_mode"] == "real_historical":
                    st.caption("真实历史输入仅限本机已有、可核验可见时间的归档来源；执行为 offline_dynamic_substitute，不调用真实模型。")
                    with st.form("create_historical_task"):
                        config_cols = st.columns(3)
                        agent_count = config_cols[0].number_input("智能体数量", min_value=1, max_value=100, value=10)
                        rounds = config_cols[1].number_input("轮数", min_value=1, max_value=10, value=3)
                        concurrency = config_cols[2].number_input("并发配置", min_value=1, max_value=32, value=4)
                        submitted = st.form_submit_button("启动真实历史事件离线任务", type="primary", use_container_width=True)
                    if submitted:
                        try:
                            job = request("POST", "/day4/historical/jobs", json={
                                "case_id": case_id, "agent_count": int(agent_count),
                                "rounds": int(rounds), "concurrency": int(concurrency),
                            })
                            st.session_state["selected_run_id"] = job["run_id"]
                            st.session_state["selected_job_id"] = job["job_id"]
                            st.session_state["reset_queue_filter"] = True
                            st.rerun()
                        except (httpx.HTTPError, ValueError, KeyError) as exc:
                            show_api_error(exc)
                else:
                    if st.button("为所选案例构建截止证据图", key="build_task_graph"):
                        try:
                            graph = request("POST", f"/cases/{case_id}/graph")
                            st.session_state["task_graph_id"] = graph["graph_id"]
                            st.session_state["task_graph_id_input"] = graph["graph_id"]
                            st.success("证据图已构建。请核对图谱及任务参数。")
                        except (httpx.HTTPError, ValueError, KeyError) as exc:
                            show_api_error(exc)
                    graph_id = st.text_input(
                        "证据图 ID", key="task_graph_id_input",
                        help="可填写已有证据图 ID，或先点击上方按钮构建。",
                    )
                    with st.form("create_task"):
                        config_cols = st.columns(3)
                        agent_count = config_cols[0].number_input("智能体数量", min_value=1, max_value=500, value=10)
                        rounds = config_cols[1].number_input("轮数", min_value=1, max_value=30, value=3)
                        concurrency = config_cols[2].number_input("并发配置", min_value=1, max_value=128, value=4)
                        budget = st.number_input(
                            "任务费用上限（元）", min_value=0.01, max_value=30.0, value=5.0,
                            step=0.01, format="%.2f",
                        )
                        submitted = st.form_submit_button("创建本地任务配置", use_container_width=True)
                    if submitted:
                        if not graph_id.strip():
                            st.warning("请先填写与案例匹配的证据图 ID。")
                        else:
                            try:
                                created = request("POST", "/simulations", json={
                                    "case_id": case_id, "graph_id": graph_id.strip(),
                                    "agent_count": int(agent_count), "rounds": int(rounds),
                                    "concurrency": int(concurrency), "budget_cny": str(budget),
                                })
                                st.session_state["selected_run_id"] = created["run_id"]
                                st.session_state["reset_queue_filter"] = True
                                st.rerun()
                            except (httpx.HTTPError, ValueError, KeyError) as exc:
                                show_api_error(exc)
            else:
                st.caption("导入一个案例后，才能手工创建关联证据图的任务。")

    with detail_panel:
        st.markdown("#### 任务详情")
        if selected_run:
            config = selected_run.get("config") or {}
            completed = selected_run.get("completed_rounds") or 0
            planned = config.get("rounds") or 0
            status = selected_run.get("status", "未知")
            status_label = run_status_labels.get(status, status)
            case_id = selected_run.get("case_id", "未知案例")
            case_title = case_titles.get(case_id, case_id)
            round_stats = selected_run.get("rounds") or []
            requested_decisions = sum(row.get("decision_requests", 0) for row in round_stats)
            failed_decisions = sum(row.get("failed_decisions", 0) for row in round_stats)
            valid_decisions = max(0, requested_decisions - failed_decisions)
            with st.container(border=True):
                st.markdown(f"##### {case_title}")
                st.markdown(f'<span class="status-pill">{status_label}</span>', unsafe_allow_html=True)
                st.progress(
                    min(completed / planned, 1.0) if planned else 0.0,
                    text=f"已完成 {completed}/{planned} 轮",
                )
                metric_cols = st.columns(2)
                metric_cols[0].metric("完成轮次", f"{completed}/{planned}")
                metric_cols[1].metric("有效决策", valid_decisions)
                metric_row_two = st.columns(2)
                metric_row_two[0].metric("失败决策", failed_decisions)
                metric_row_two[1].metric("参与智能体", selected_run.get("actual_participants") or config.get("agent_count") or 0)
                st.caption(
                    f"数据模式：{'合成数据' if config.get('data_mode') == 'synthetic' else '真实历史材料' if config.get('data_mode') == 'real_historical' else '未记录'}　·　"
                    f"执行方式：{'本机动态离线替身' if (config.get('backend') or {}).get('type', config.get('backend_mode')) == 'offline_dynamic_substitute' else (config.get('backend') or {}).get('type', config.get('backend_mode', '未记录'))}　·　"
                    f"本机费用估算：¥{selected_run.get('cost_cny') or '0.00'}"
                )
                with st.expander("任务技术详情"):
                    st.write({"运行 ID": selected_run_id, "案例 ID": case_id,
                              "图谱 ID": selected_run.get("graph_id"),
                              "原始数据模式": config.get("data_mode"),
                              "执行器字段": (config.get("backend") or {}).get(
                                  "type", config.get("backend_mode"))})
                if round_stats:
                    with st.expander("查看逐轮账本"):
                        st.dataframe([{"轮次": row.get("round"),
                                       "请求决策": row.get("decision_requests", 0),
                                       "有效决策": row.get("decision_requests", 0) - row.get("failed_decisions", 0),
                                       "失败决策": row.get("failed_decisions", 0),
                                       "模拟消息": row.get("messages", 0)}
                                      for row in round_stats], hide_index=True, use_container_width=True)
                st.caption("运行轮次、决策有效数和失败数均来自本地账本；离线替身输出不代表真实公众走势或 G3 通过。")

            run_jobs = [row for row in job_rows if row.get("run_id") == selected_run_id]
            if status == "created" and not run_jobs:
                st.info("该运行尚未执行。启动后将自动生成五模块报告和暂定四级预警。")
                if st.button("启动本机离线任务", type="primary", use_container_width=True):
                    try:
                        job = request("POST", "/jobs", json={"run_id": selected_run_id})
                        st.session_state["selected_job_id"] = job["job_id"]
                        st.rerun()
                    except (httpx.HTTPError, ValueError, KeyError) as exc:
                        show_api_error(exc)
            elif run_jobs:
                st.caption(f"关联执行记录：{run_jobs[0].get('job_id')}。下方显示任务进度。")
            else:
                st.caption("此运行已有账本结果，无需再次启动。")

            st.divider()
            st.markdown("#### 执行进度")
            if st.button("刷新任务进度", use_container_width=True):
                st.rerun()
            if job_list_error:
                show_api_error(job_list_error)
            if run_jobs:
                job_ids = [row["job_id"] for row in run_jobs if row.get("job_id")]
                preferred_job = st.session_state.get("selected_job_id")
                if preferred_job not in job_ids:
                    preferred_job = job_ids[0]
                    st.session_state["selected_job_id"] = preferred_job
                if len(job_ids) > 1:
                    selected_job_id = st.selectbox("执行任务", job_ids, key="selected_job_id")
                else:
                    selected_job_id = job_ids[0]
                try:
                    selected_job = request("GET", f"/jobs/{selected_job_id}")
                except (httpx.HTTPError, ValueError) as exc:
                    show_api_error(exc)
                else:
                    completed_rounds = selected_job.get("completed_rounds") or 0
                    target_rounds = selected_job.get("target_rounds") or 0
                    raw_progress = selected_job.get("progress")
                    progress = (completed_rounds / target_rounds) if raw_progress is None and target_rounds else raw_progress or 0
                    st.progress(
                        max(0.0, min(float(progress), 1.0)),
                        text=(f"已完成 {completed_rounds}/{target_rounds} 轮 · "
                              f"状态：{run_status_labels.get(selected_job.get('status'), selected_job.get('status', '未知'))}"),
                    )
                    job_cols = st.columns(2)
                    job_cols[0].metric("云模型调用", selected_job.get("model_calls", 0))
                    job_cols[1].metric("外部网络请求", selected_job.get("network_requests", 0))
                    st.caption(
                        f"数据模式：{'合成数据' if selected_job.get('data_mode') == 'synthetic' else '真实历史材料' if selected_job.get('data_mode') == 'real_historical' else '未记录'} · "
                        "运行方式：本机动态离线替身 · 模拟智能体使用独立状态、记忆和运行时消息。页面不会自动轮询。"
                    )
                    with st.expander("执行技术字段"):
                        st.write({"任务 ID": selected_job.get("job_id"),
                                  "执行模式字段": selected_job.get("mode"),
                                  "运行状态字段": selected_job.get("status"),
                                  "完成类型": selected_job.get("result_state"),
                                  "决策完整有效": selected_job.get("all_decisions_valid"),
                                  "决策计数": selected_job.get("action_counts"),
                                  "错误分类字段": selected_job.get("error")})
                    if selected_job.get("input_received_at") and selected_job.get("report_generated_at"):
                        st.caption(
                            f"任务输入：{local_time(selected_job['input_received_at'])} · "
                            f"报告生成：{local_time(selected_job['report_generated_at'])} · "
                            f"输入至报告：{selected_job.get('elapsed_seconds')} 秒"
                        )
                    if selected_job.get("status") == "failed":
                        kind = selected_job.get("result_state")
                        label = {"partial": "部分完成", "stopped": "已停止"}.get(kind, "失败")
                        st.error(f"任务{label}：" + str(selected_job.get("error") or "未记录错误详情"))
                    report_result = selected_job.get("report_id")
                    alert_result = selected_job.get("alert_id")
                    if report_result or alert_result:
                        st.success("仿真后的报告与预警已自动生成。")
                        if report_result:
                            st.write(f"报告 ID：`{report_result}`")
                            st.session_state["report_id"] = report_result
                        if alert_result:
                            st.write(f"预警 ID：`{alert_result}`")
                            st.session_state["alert_id"] = alert_result
            elif status == "created":
                st.caption("任务还未启动。启动后可在此刷新轮次和执行状态。")
            else:
                st.caption("当前运行没有关联执行任务记录。")

            link_cols = st.columns(3)
            graph_id = selected_run.get("graph_id")
            linked_job = run_jobs[0] if run_jobs else {}
            linked_report_id = linked_job.get("report_id")
            linked_alert_id = linked_job.get("alert_id")
            if link_cols[0].button("打开关联图谱", use_container_width=True):
                queue_navigation("五模块报告", report_graph_id=graph_id,
                                 report_id=linked_report_id or "")
            if link_cols[1].button("打开关联报告", use_container_width=True,
                                   disabled=not linked_report_id):
                queue_navigation("五模块报告", report_graph_id=graph_id,
                                 report_id=linked_report_id)
            if link_cols[2].button("打开关联预警", use_container_width=True,
                                   disabled=not linked_alert_id):
                queue_navigation("预警中心", alert_id=linked_alert_id,
                                 alert_run_id=selected_run_id)
        else:
            if job_list_error:
                show_api_error(job_list_error)
            st.info("从左侧选择一项任务，查看运行配置、轮次进度及其报告和预警。")

if active_page == "五模块报告":
    st.subheader("报告与证据")
    st.caption("当前报告、案例、来源图谱和仿真运行一一绑定；模拟传播图与来源证据图分开展示。")
    if run_rows:
        report_run_by_id = {row["run_id"]: row for row in run_rows}
        report_case_titles = {item["case_id"]: item.get("title", item["case_id"]) for item in cases}
        report_run_id = st.selectbox(
            "选择报告运行", list(report_run_by_id),
            format_func=lambda run_id: (
                f"{report_case_titles.get(report_run_by_id[run_id].get('case_id'), '未命名案例')} · "
                f"{report_run_by_id[run_id].get('completed_rounds', 0)}/"
                f"{(report_run_by_id[run_id].get('config') or {}).get('rounds', 0)} 轮"
            ), key="report_run",
        )
        if st.button("生成离线报告"):
            try:
                built = request("POST", "/reports", json={"run_id": report_run_id})
                st.session_state["report_id"] = built["report_id"]
                st.session_state["report_id_input"] = built["report_id"]
                st.success("离线报告已生成。")
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                show_api_error(exc)
    else:
        st.write("没有可生成报告的运行。")
    with st.expander("技术字段：报告 ID"):
        report_id = st.text_input("查看报告 ID", value=st.session_state.get("report_id", ""),
                                  key="report_id_input")
    report_id = report_id or st.session_state.get("report_id", "")
    if report_id.strip():
        try:
            report = request("GET", f"/reports/{report_id.strip()}")
            modules = report.get("modules") or {}
            overview = report.get("overview") or {}
            integrity = overview.get("run_integrity") or {}
            risk = modules.get("risk") or {}
            level_labels = {"red": "红色", "orange": "橙色", "yellow": "黄色", "blue": "蓝色"}
            mode_labels = {"synthetic": "合成数据", "real_historical": "真实历史材料"}
            execution_labels = {"offline_dynamic_substitute": "本机动态离线替身"}
            st.title(overview.get("event_title") or report.get("case_id") or "事件报告")
            st.write(overview.get("event_summary") or "事件概况以报告引用的截止前来源为准。")
            summary_cols = st.columns(4)
            summary_cols[0].metric("风险等级", level_labels.get(risk.get("level"), "未判级"))
            summary_cols[1].metric("完成轮次", f"{integrity.get('completed_rounds', 0)}/{integrity.get('configured_rounds', 0)}")
            summary_cols[2].metric("有效决策", integrity.get("valid_decisions", 0))
            summary_cols[3].metric("失败决策", integrity.get("failed_decisions", 0))
            if integrity.get("failed_decisions", 0) or integrity.get("status") != "complete":
                st.error("运行未完整完成或含失败决策；查看技术核查提示后再解释汇总结果。")
            if risk.get("status") == "provisional_offline_assessment":
                st.warning("暂定离线判级 · 规则未按真实事件校准；不代表实际公众风险或正式预警达标。")
            elif not risk.get("level"):
                st.info("当前报告没有风险等级。")
            st.markdown("#### 主要依据")
            basis = overview.get("main_basis") or {}
            metrics = basis if isinstance(basis, dict) else {}
            if metrics.get("simulated_complaint_intent_share") is not None:
                share = metrics["simulated_complaint_intent_share"]
                st.write(f"末轮模拟投诉意向：{metrics.get('simulated_complaint_intent_decisions', 0)} / "
                         f"{metrics.get('latest_round_valid_decisions', 0)} 个有效决策（{share:.1%}）；"
                         "这是暂定规则的模拟输入，不是实际投诉量。")
            else:
                st.write(metrics.get("note", "未提供可解释的判级依据。"))
            source_basis = overview.get("source_basis") or []
            if source_basis:
                st.caption("截止前来源依据")
                for source in source_basis:
                    with st.container(border=True):
                        st.markdown(f"**{source.get('title') or source.get('record_id')}**")
                        st.write(source.get("summary", ""))
                        st.caption(f"可见时间：{source.get('available_at') or '未知'} · 引用：{source.get('record_id')}")
                        if source.get("source_url"):
                            st.markdown(f"[打开原始来源]({source['source_url']})")
            st.markdown("#### 建议行动")
            st.write(overview.get("recommended_action") or "当前没有报告支持的建议行动。")
            st.caption(f"数据模式：{mode_labels.get(report.get('data_mode'), report.get('data_mode', '未记录'))}　·　"
                       f"执行方式：{execution_labels.get(report.get('execution_mode'), report.get('execution_mode', '未记录'))}　·　"
                       f"运行完整性：{integrity.get('integrity_label', '未记录')}")
            if report.get("data_mode") == "real_historical" and report.get("execution_mode") == "offline_dynamic_substitute":
                st.warning("真实历史材料仅作为本机输入；推演由离线动态替身执行，不是实时采集或真实模型结论。")

            propagation = modules.get("propagation") or {}
            st.markdown("### 模拟传播图")
            st.caption("以下关系来自模拟运行时消息；与来源证据图分开，不代表真实平台传播。")
            st.write(f"模拟消息：{propagation.get('total_messages', 0)} 条")
            edges = propagation.get("edges") or []
            if edges:
                show_propagation_graph(edges, report.get("report_id", report_id))
                with st.expander(f"查看全部 {len(edges)} 条模拟关系及引用"):
                    st.dataframe([{"发送智能体": row.get("sender_id"), "接收智能体": row.get("recipient_id"),
                                   "消息数": row.get("message_count"),
                                   "轮次": ", ".join(map(str, row.get("rounds", []))),
                                   "来源证据引用": ", ".join(row.get("source_record_ids", [])),
                                   "消息引用": ", ".join(row.get("message_ids", [])),
                                   "发送动作引用": ", ".join(row.get("action_ids", []))}
                                  for row in edges], hide_index=True, use_container_width=True)
            else:
                st.info("本次运行没有记录模拟传播边。")
            evolution = modules.get("emotion_evolution") or {}
            st.markdown("#### 情绪演化")
            emotion_series = evolution.get("series") or []
            if (evolution.get("emotion_state_available") and emotion_series
                    and evolution.get("reconstruction_status") == "validated_against_final_agent_state"):
                chart_rows = [{"轮次": row["round"], "智能体平均模拟情绪": row["mean_emotion"],
                               "最低模拟情绪": row["min_emotion"], "最高模拟情绪": row["max_emotion"]}
                              for row in emotion_series]
                st.line_chart(chart_rows, x="轮次")
                st.caption("纵轴为模拟智能体状态情绪值（范围 -1 至 1），横轴为完成轮次；不是现实公众情绪。")
                st.caption("计算依据：按已冻结的状态转移规则逐轮重建，再与持久化的最终智能体状态核对。")
                with st.expander("情绪计算依据与逐轮动作账本"):
                    st.write(evolution.get("calculation_basis") or evolution.get("note"))
                    st.dataframe([{"轮次": row.get("round"), "动作计数": row.get("action_counts"),
                                   "决策数": row.get("decision_count"),
                                   "动作引用": ", ".join(row.get("action_ids", []))}
                                  for row in evolution.get("rounds", [])], hide_index=True, use_container_width=True)
            else:
                distributions = evolution.get("rounds") or []
                chart_rows = [{"轮次": row.get("round"), **(row.get("action_counts") or {})}
                              for row in distributions]
                if chart_rows:
                    st.line_chart(chart_rows, x="轮次")
                st.caption("当前仅有逐轮动作计数；状态重建未验证，不能把该图解释为情绪分数。")
                if evolution.get("reason"):
                    st.write("重建限制：" + str(evolution["reason"]))
            if evolution.get("note"):
                st.caption(evolution["note"])
            nodes = modules.get("key_nodes") or {}
            st.markdown("#### 关键活跃节点")
            st.caption("排序指标：发送的运行时模拟消息数；消息活跃度不代表因果影响或现实影响力。")
            if nodes.get("nodes"):
                st.dataframe([{"排序": index + 1, "智能体编号": row.get("agent_id"),
                               "发送消息数": row.get("outgoing_message_count", 0),
                               "接收消息数": row.get("incoming_message_count", 0),
                               "消息引用": ", ".join(row.get("message_ids", []))}
                              for index, row in enumerate(nodes["nodes"])], hide_index=True, use_container_width=True)
            else:
                st.write("没有可排序的运行时消息节点。")
            st.caption("节点活动与因果影响区分说明见上方排序口径。")
            recommendations = modules.get("recommendations") or {}
            st.markdown("### 业务处置建议")
            for item in recommendations.get("items") or []:
                with st.container(border=True):
                    st.write(item.get("text", ""))
                    st.caption("适用条件：" + str(item.get("applicable_when", "未记录")))
                    st.caption("建议负责角色：" + str(item.get("responsible_role", "未记录")))
                    st.caption("建议时限：" + str(item.get("suggested_deadline", "未记录")))
                    st.caption("后续观察指标：" + "、".join(item.get("follow_up_metrics") or []))
                    st.caption("依据来源：" + "、".join(item.get("source_record_ids") or []))
                    for source in source_basis:
                        if source.get("record_id") in (item.get("source_record_ids") or []) and source.get("source_url"):
                            st.markdown(f"[核对引用：{source.get('title')}]({source['source_url']})")
                    st.caption("建议由流程模板整理，不是模型生成的事实结论。")
            if not recommendations.get("items"):
                st.write("当前没有可展示的建议。")
            st.markdown("### 技术核查提示")
            for item in recommendations.get("technical_checks") or []:
                st.warning(item.get("text", "需核查"))
                citations = (item.get("source_record_ids") or []) + (item.get("action_ids") or []) + (item.get("message_ids") or [])
                if citations:
                    st.caption("技术引用：" + "、".join(citations[:20]))
            for limitation in report.get("limitations") or []:
                st.warning(limitation)
            st.download_button("下载完整报告 JSON", data=json.dumps(report, ensure_ascii=False, indent=2),
                               file_name=f"{report.get('report_id', 'report')}.json", mime="application/json")
            with st.expander("技术字段与运行标识"):
                st.write({"报告 ID": report.get("report_id"), "案例 ID": report.get("case_id"),
                          "图谱 ID": report.get("graph_id"), "运行 ID": report.get("run_id"),
                          "图构建模式": report.get("graph_build_mode"),
                          "预警规则版本": risk.get("rule_version"),
                          "预警状态字段": risk.get("status")})
            st.markdown("### 来源证据图")
            st.caption("该图按截止时间前的来源构建，与模拟智能体消息传播图不同。")
            graph_id = st.session_state.get("report_graph_id") or report.get("graph_id")
            try:
                source_graph = request("GET", f"/graphs/{graph_id}")
                show_graph(source_graph)
                with st.expander("来源图节点与关系明细"):
                    st.dataframe(source_graph.get("nodes", []), hide_index=True, use_container_width=True)
                    st.dataframe(source_graph.get("edges", []), hide_index=True, use_container_width=True)
            except (httpx.HTTPError, ValueError) as exc:
                show_api_error(exc)
            st.markdown("#### 按词项查找引用来源")
            st.caption("检索方式：词项匹配来源标题/摘要，再返回图邻域和来源引用；不生成自然语言答案。完整自然语言追问尚未实现。")
            query = st.text_input("来源证据检索", placeholder="例如：Change Healthcare 网络中断",
                                  key="evidence_query_input")
            if st.button("检索带引用证据"):
                try:
                    result = request("POST", f"/graphs/{graph_id}/query", json={"question": query})
                    st.session_state["evidence_query_result"] = result
                except (httpx.HTTPError, ValueError) as exc:
                    show_api_error(exc)
            query_result = st.session_state.get("evidence_query_result") or {}
            if query_result.get("graph_id") == graph_id:
                st.caption("接口检索类型：词项匹配 + 来源引用的图邻域")
                hits = query_result.get("hits") or []
                if not hits:
                    st.info("没有匹配到截止前来源；请尝试来源标题或摘要中的词项。")
                for hit in hits:
                    with st.container(border=True):
                        st.markdown(f"**{hit.get('title') or hit.get('record_id')}**")
                        st.write(hit.get("text", ""))
                        st.caption(f"来源引用：{hit.get('record_id')} · 匹配分：{hit.get('score')}")
                        if hit.get("source_url"):
                            st.markdown(f"[打开引用来源]({hit['source_url']})")
        except (httpx.HTTPError, ValueError) as exc:
            show_api_error(exc)
    elif st.session_state.get("report_graph_id"):
        st.markdown("### 来源证据图")
        st.caption("该图按截止时间前的来源构建，与模拟智能体消息传播图不同。")
        try:
            source_graph = request("GET", f"/graphs/{st.session_state['report_graph_id']}")
            show_graph(source_graph)
            with st.expander("来源图节点与关系明细"):
                st.dataframe(source_graph.get("nodes", []), hide_index=True, use_container_width=True)
                st.dataframe(source_graph.get("edges", []), hide_index=True, use_container_width=True)
        except (httpx.HTTPError, ValueError) as exc:
            show_api_error(exc)
    elif run_rows:
        st.info("所选运行尚无可查看报告。运行完成后从处置队列打开自动生成报告，或在上方生成离线报告。")
    elif not run_rows:
        st.info("暂无运行或报告。先在处置队列启动一条本机离线任务，完成后可从任务直接打开报告。")

if active_page == "预警中心":
    st.subheader("四级预警与离线通知预览")
    st.caption("红、橙、黄、蓝为工程规则结果；预览不发送企业微信或邮件。发现到推送时效及真实送达尚未验证。")
    if run_rows:
        alert_run_ids = [row["run_id"] for row in run_rows]
        if st.session_state.get("alert_run_id") not in alert_run_ids:
            st.session_state["alert_run_id"] = alert_run_ids[0]
        alert_run_labels = {item["case_id"]: item.get("title", item["case_id"]) for item in cases}
        alert_run_labels["day4_demo_fictional_synthetic_v1"] = "虚构 Harborlight 保险服务演练"

        def alert_run_label(run_id):
            row = next(item for item in run_rows if item["run_id"] == run_id)
            config = row.get("config") or {}
            title = alert_run_labels.get(row.get("case_id"), row.get("case_id", "未命名案例"))
            return f"{title}　·　{row.get('completed_rounds', 0)}/{config.get('rounds', 0)} 轮"

        st.session_state["alert_run_widget"] = st.session_state["alert_run_id"]
        alert_run_id = st.selectbox(
            "选择预警运行", alert_run_ids,
            index=alert_run_ids.index(st.session_state["alert_run_id"]),
            format_func=alert_run_label, key="alert_run_widget",
        )
        st.session_state["alert_run_id"] = alert_run_id
        if st.button("按当前规则判级"):
            try:
                assessed = request("POST", "/alerts/assess", json={"run_id": alert_run_id})
                st.session_state["alert_id"] = assessed["alert_id"]
                st.session_state["alert_id_input"] = assessed["alert_id"]
                st.success("已生成离线判级结果。")
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                show_api_error(exc)
    else:
        st.write("没有可判级的运行。")
    with st.expander("技术字段：预警 ID"):
        alert_id = st.text_input("查看预警 ID", value=st.session_state.get("alert_id", ""),
                                 key="alert_id_input")
    if alert_id.strip():
        try:
            alert = request("GET", f"/alerts/{alert_id.strip()}")
            level_labels = {"red": "红色", "orange": "橙色", "yellow": "黄色", "blue": "蓝色"}
            mode_labels = {"synthetic": "合成数据", "real_historical": "真实历史材料"}
            case_title = next((item.get("title") for item in cases
                               if item.get("case_id") == alert.get("case_id")), alert.get("case_id"))
            st.markdown(f"**关联事件：{case_title or '未记录'}**")
            st.metric("风险等级", level_labels.get(alert.get("level"), "未判定"))
            st.caption(f"数据模式：{mode_labels.get(alert.get('data_mode'), '未记录')}")
            metric_labels = {
                "round": "完成轮次", "run_status": "运行状态",
                "latest_round_valid_decisions": "末轮有效决策数",
                "latest_round_failed_decisions": "末轮失败决策数",
                "run_failed_decisions": "累计失败决策数",
                "simulated_complaint_intent_decisions": "末轮模拟投诉意向数",
                "simulated_complaint_intent_share": "末轮模拟投诉意向占比",
                "latest_round_simulated_messages": "末轮模拟消息数",
            }
            metrics = alert.get("metrics") or {}

            def alert_metric_value(key, value):
                if key == "run_status":
                    return {"complete": "已完成", "partial": "部分完成", "failed": "失败"}.get(value, value)
                if key == "simulated_complaint_intent_share":
                    try:
                        return f"{float(value):.1%}"
                    except (TypeError, ValueError):
                        return str(value)
                return str(value)

            st.markdown("#### 判级依据")
            readable_metrics = [{"指标": metric_labels[key], "数值": alert_metric_value(key, value)}
                                for key, value in metrics.items() if key in metric_labels]
            if readable_metrics:
                st.dataframe(readable_metrics, hide_index=True, use_container_width=True)
            thresholds = metrics.get("thresholds")
            if thresholds:
                st.caption("暂定规则阈值：" + "、".join(
                    f"{level_labels.get(key, key)} {value:.0%}" for key, value in thresholds.items()
                ))
            evidence_refs = alert.get("evidence_refs") or {}
            source_ids = evidence_refs.get("source_record_ids", []) if isinstance(evidence_refs, dict) else []
            if source_ids:
                st.caption(f"判级关联截止前来源记录：{len(source_ids)} 条；可在五模块报告中逐条核对。")
            else:
                st.caption("当前预警未记录来源记录引用。")
            limitation_labels = {
                "Provisional thresholds on simulated complaint-intent decisions; not calibrated to real incidents.":
                    "暂定阈值基于模拟投诉意向决策；未按真实事件校准。",
                "Simulation actions and messages are not real platform activity or actual complaint volume.":
                    "仿真动作和消息不代表真实平台活动或实际投诉量。",
                "Historical-source discovery and actual delivery timestamps are unavailable; the 30-minute red alert SLA is unverified.":
                    "历史来源发现时间和实际送达时间不可用；红色预警 30 分钟 SLA 未验证。",
                "Offline preview only; no WeCom or email notification has been sent.":
                    "仅离线预览；尚未发送企业微信或邮件通知。",
                "Real historical source material was processed by an offline substitute; this does not prove real-model simulation quality.":
                    "真实历史材料由离线替身处理；这不证明真实模型推演质量。",
                "Real historical source material was processed by an offline substitute; this is not real-model reasoning evidence.":
                    "真实历史材料由离线替身处理；这不是基于真实模型推理的证据。",
            }
            for limitation in alert.get("limitations", []):
                st.warning(limitation_labels.get(limitation, limitation))
            st.caption("发现时间：" + local_time(alert.get("discovered_at")) +
                       " · 发送时间：" + (local_time(alert["sent_at"]) if alert.get("sent_at") else "未发送"))
            st.caption("发现时间表示本机规则评估时刻，不是历史事件首次公开或实际送达时间。")
            with st.expander("技术字段：规则版本与关联标识"):
                st.write({"规则版本": alert.get("rule_version"), "预警 ID": alert.get("alert_id"),
                          "运行 ID": alert.get("run_id"), "案例 ID": alert.get("case_id"),
                          "运行状态字段": alert.get("run_status"),
                          "完整判级指标": metrics, "完整证据引用": evidence_refs})
            channel = st.selectbox("离线预览渠道", ["wecom", "email"],
                                   format_func=lambda item: "企业微信" if item == "wecom" else "邮件")
            if st.button("生成通知预览"):
                try:
                    preview = request("POST", f"/alerts/{alert_id.strip()}/previews",
                                      json={"channel": channel})
                    st.json(preview, expanded=False)
                except (httpx.HTTPError, ValueError) as exc:
                    show_api_error(exc)
        except (httpx.HTTPError, ValueError) as exc:
            show_api_error(exc)

if active_page == "关口状态":
    st.subheader("内部关口状态")
    st.success("G1-V2 与 G2-V2 已内部受限通过；可运行 API、持久化、截止快照、受限证据图检索。")
    st.warning("G3 尚未通过：纯合成百炼 500 Agent × 30 轮已实测，14,994 次有效决策、6 次无效决策；消息改变动作的受控因果证据仍缺。")
    st.caption("G4 正在工程实现，尚未以真实任务演示、通知送达、计时或域内运行证据签收。")
    for blocker in readiness["blockers"]:
        st.warning(blocker)
    st.write("DailyComplaint 是遗留可选契约，不参与新版主链验收。")
    st.markdown(f"[查看接口文档]({DOCS_API}/docs)")
