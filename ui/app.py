import json
import os
from pathlib import Path

import httpx
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
API = os.getenv("RISKSHIELD_API_URL", "http://127.0.0.1:8000").rstrip("/")
DOCS_API = "http://127.0.0.1:8000" if API == "http://api:8000" else API
st.set_page_config(page_title="风控盾 · Day 4", page_icon="🛡️", layout="wide")
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
    st.caption(f"{len(nodes)} 个证据节点、{len(edges)} 条关系；这是截止快照的证据图，不是实际社交传播图。")


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
    st.markdown("<div class='sidebar-brand'><span class='brand-mark'>盾</span><span>风控盾</span></div>", unsafe_allow_html=True)
    st.caption("DAY 4 · 本机风险工作台")
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
    st.caption("仅限本机离线工作流；不发送真实通知。Day 5 未授权。")
    st.markdown("<div class='sidebar-section-label'>验收关口</div>", unsafe_allow_html=True)
    st.markdown("**G1 / G2**　内部受限通过")
    st.markdown("**G3**　尚未通过")
    st.caption("G4 待实际运行证据核验。")

header_title, header_action = st.columns([5, 1], vertical_alignment="center")
with header_title:
    st.title("风控盾")
    st.caption(f"DAY 4 V2 · {active_page} · 证据、仿真、离线报告与预警工作台")
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
        ("导入 Day 1 时间证据事件", "unh_change_20240222_event_v2.json", "g1_import_result",
         "case_import_projection", "Day 1 事件包导入失败，请查看 API 状态和事件包字段映射。"),
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

    queue_panel, detail_panel = st.columns([1.08, 0.92], gap="large")
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
                    format_func=run_label, key="selected_run_id",
                    label_visibility="collapsed",
                )
                st.caption(f"{len(visible_run_ids):02d} 项 · 默认按最近创建顺序显示")
                selected_run = next(row for row in filtered_runs if row["run_id"] == selected_run_id)
            else:
                st.info("这个状态下暂时没有任务。切换筛选可查看其他事项。")
        else:
            st.info("队列暂无任务。可准备一条纯合成离线演示，或在下方填写本地任务配置。")
            st.caption("历史 500×30 纯合成规模记录保存在独立研究数据中，不会自动导入此队列。")

        with st.expander("准备或创建任务", expanded=not run_rows):
            st.markdown("**纯合成离线演示**")
            st.write("10 Agent × 3 轮 · 动态离线测试替身 · 云模型调用 0 · 外部网络请求 0")
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
                        agent_count = config_cols[0].number_input("Agent 数量", min_value=1, max_value=100, value=10)
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
                        agent_count = config_cols[0].number_input("Agent 数量", min_value=1, max_value=500, value=10)
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
            with st.container(border=True):
                st.markdown(f"##### {case_title}")
                st.caption(f"{case_id}　/　运行 `{selected_run_id}`")
                st.markdown(f'<span class="status-pill">{status_label}</span>', unsafe_allow_html=True)
                st.progress(
                    min(completed / planned, 1.0) if planned else 0.0,
                    text=f"已完成 {completed}/{planned} 轮",
                )
                metric_cols = st.columns(3)
                metric_cols[0].metric("Agent", selected_run.get("actual_participants") or config.get("agent_count") or 0)
                metric_cols[1].metric("轮数", f"{completed}/{planned}")
                metric_cols[2].metric("费用估算", selected_run.get("cost_cny") or "—")
                st.caption(
                    f"证据图：{selected_run.get('graph_id') or '未关联'}　·　"
                    f"模式：{config.get('data_mode') or '未记录'}"
                )
                if selected_run.get("rounds"):
                    with st.expander("查看逐轮账本"):
                        st.dataframe(selected_run["rounds"], hide_index=True, use_container_width=True)
                st.caption("轮次结束不等于所有决策有效，也不表示 G3 通过。费用为本地估算。")

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
                    job_cols = st.columns(3)
                    job_cols[0].metric("执行模式", selected_job.get("mode") or "未记录")
                    job_cols[1].metric("云模型调用", selected_job.get("model_calls", 0))
                    job_cols[2].metric("外部网络请求", selected_job.get("network_requests", 0))
                    st.caption(
                        f"数据：{selected_job.get('data_mode') or '未记录'} · "
                        "离线动态替身；Agent 使用独立状态、记忆和运行时消息。页面不会自动轮询。"
                    )
                    if selected_job.get("input_received_at") and selected_job.get("report_generated_at"):
                        st.caption(
                            f"任务输入：{selected_job['input_received_at']} · "
                            f"报告生成：{selected_job['report_generated_at']} · "
                            f"输入至报告：{selected_job.get('elapsed_seconds')} 秒"
                        )
                    if selected_job.get("status") == "failed":
                        st.error("任务执行失败：" + str(selected_job.get("error") or "未记录错误详情"))
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
        else:
            if job_list_error:
                show_api_error(job_list_error)
            st.info("从左侧选择一项任务，查看运行配置、轮次进度及其报告和预警。")

if active_page == "五模块报告":
    st.subheader("五模块离线报告")
    st.caption("报告只关联一个运行及其案例/图谱。纯合成运行不得作为真实历史案例的端到端报告。")
    if run_rows:
        report_run_id = st.selectbox("选择报告运行", [row["run_id"] for row in run_rows], key="report_run")
        if st.button("生成离线报告"):
            try:
                built = request("POST", "/reports", json={"run_id": report_run_id})
                st.session_state["report_id"] = built["report_id"]
                st.success("离线报告已生成。")
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                show_api_error(exc)
    else:
        st.write("没有可生成报告的运行。")
    report_id = st.text_input("查看报告 ID", value=st.session_state.get("report_id", ""))
    if report_id.strip():
        try:
            report = request("GET", f"/reports/{report_id.strip()}")
            st.caption(f"报告 {report.get('report_id', report_id)} · 数据模式：{report.get('data_mode', '未记录')}")
            modules = report.get("modules") or {}
            propagation = modules.get("propagation") or {}
            st.markdown("#### 传播路径图谱（模拟）")
            st.write(f"运行时消息：{propagation.get('total_messages', 0)} 条")
            edges = propagation.get("edges") or []
            if edges:
                st.dataframe([{"发送 Agent": row.get("sender_id"), "接收 Agent": row.get("recipient_id"),
                               "消息数": row.get("message_count"), "消息 ID": ", ".join(row.get("message_ids", [])[:3])}
                              for row in edges[:50]], hide_index=True, use_container_width=True)
                st.caption(f"展示前 {min(len(edges), 50)}/{len(edges)} 条模拟关系；消息数量不能证明因果影响。")
            else:
                st.write("本次运行没有记录模拟传播边。")
            evolution = modules.get("emotion_evolution") or {}
            st.markdown("#### 情绪演化")
            emotion_series = evolution.get("series") or []
            if (evolution.get("emotion_state_available") and emotion_series
                    and evolution.get("reconstruction_status") == "validated_against_final_agent_state"):
                st.line_chart(emotion_series, x="round", y="mean_emotion")
                st.caption("逐轮 Agent 平均情绪：依据仿真状态转移重建并与最终状态校验。"
                           "这是模拟状态，不是观察到的真实公众情绪。")
            else:
                distributions = evolution.get("rounds") or []
                chart_rows = [{"轮次": row.get("round"), **(row.get("action_counts") or {})}
                              for row in distributions]
                if chart_rows:
                    st.line_chart(chart_rows, x="轮次")
                st.caption("逐轮动作计数代理；未得到可校验的情绪状态曲线，不能称为情绪分数。")
                if evolution.get("reason"):
                    st.write("重建限制：" + str(evolution["reason"]))
            if evolution.get("note"):
                st.caption(evolution["note"])
            nodes = modules.get("key_nodes") or {}
            st.markdown("#### 关键活跃节点")
            if nodes.get("nodes"):
                st.dataframe(nodes["nodes"][:20], hide_index=True, use_container_width=True)
            else:
                st.write("没有可排序的运行时消息节点。")
            st.caption(nodes.get("interpretation") or "排序依据是模拟消息活动，不证明实际影响。")
            risk = modules.get("risk") or {}
            st.markdown("#### 风险等级")
            st.write({"等级": risk.get("level") or "未判级", "状态": risk.get("status") or "未知",
                      "规则版本": risk.get("rule_version") or "未记录"})
            recommendations = modules.get("recommendations") or {}
            st.markdown("#### 应对建议")
            for item in recommendations.get("items") or []:
                st.write(item.get("text", ""))
                st.caption("来源：" + ", ".join(item.get("source_record_ids") or []) +
                           " · 动作：" + ", ".join((item.get("action_ids") or [])[:5]) +
                           " · 消息：" + ", ".join((item.get("message_ids") or [])[:5]))
            if not recommendations.get("items"):
                st.write("当前没有可展示的建议。")
            for limitation in report.get("limitations") or []:
                st.warning(limitation)
            st.download_button("下载完整报告 JSON", data=json.dumps(report, ensure_ascii=False, indent=2),
                               file_name=f"{report.get('report_id', 'report')}.json", mime="application/json")
        except (httpx.HTTPError, ValueError) as exc:
            show_api_error(exc)
    graph_lookup = st.text_input("查看证据图 ID", value=st.session_state.get("task_graph_id", ""),
                                 key="report_graph_id")
    if graph_lookup.strip():
        try:
            show_graph(request("GET", f"/graphs/{graph_lookup.strip()}"))
        except (httpx.HTTPError, ValueError) as exc:
            show_api_error(exc)

if active_page == "预警中心":
    st.subheader("四级预警与离线通知预览")
    st.caption("红、橙、黄、蓝为工程规则结果；预览不发送企业微信或邮件。发现到推送时效及真实送达尚未验证。")
    if run_rows:
        alert_run_id = st.selectbox("选择预警运行", [row["run_id"] for row in run_rows], key="alert_run")
        if st.button("按当前规则判级"):
            try:
                assessed = request("POST", "/alerts/assess", json={"run_id": alert_run_id})
                st.session_state["alert_id"] = assessed["alert_id"]
                st.success("已生成离线判级结果。")
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                show_api_error(exc)
    else:
        st.write("没有可判级的运行。")
    alert_id = st.text_input("查看预警 ID", value=st.session_state.get("alert_id", ""))
    if alert_id.strip():
        try:
            alert = request("GET", f"/alerts/{alert_id.strip()}")
            st.metric("风险等级", alert.get("level", "未判定"))
            st.caption(f"数据模式：{alert.get('data_mode', '未记录')} · 规则版本：{alert.get('rule_version', '未记录')}")
            st.write("判级依据", alert.get("metrics", {}))
            st.write("证据引用", alert.get("evidence_refs", []))
            for limitation in alert.get("limitations", []):
                st.warning(limitation)
            st.caption("发现时间：" + str(alert.get("discovered_at") or "未记录") +
                       " · 发送时间：" + str(alert.get("sent_at") or "未发送"))
            st.caption("发现时间表示本机规则评估时刻，不是历史事件首次公开或实际送达时间。")
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
