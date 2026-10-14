#!/usr/bin/env python
"""Render the design diagrams of ``docs/design/技术架构与流程图-2026-10-14.md``.

One source of truth, two outputs: every diagram is described once as a list of
primitives and emitted twice -- as SVG (vector, the committed artefact) and as
PNG (raster, for slide decks and documents). Both outputs are therefore always
in step; regenerating after an edit cannot leave a stale PNG behind.

The content is transcribed from the current commit, not from intent:

* the layer list, endpoint list and capability flags come from
  ``src/riskshield/api.py`` and the modules it wires together;
* the alert thresholds and the report module names come from
  ``src/riskshield/alerts.py`` and ``src/riskshield/reporting.py``;
* the "not implemented / not verified" column comes from the ``capabilities``
  and ``blockers`` block of ``/readiness`` plus ``docs/delivery/test-report.md``.

Usage::

    .venv\\Scripts\\python.exe scripts/render_design_diagrams.py

SVG output needs nothing but the standard library. PNG output additionally
needs Pillow (present in the development virtual environment through
Streamlit); without it the script still writes every SVG and says so, so a
missing optional dependency never produces a silently half-written diagram set.

This script is a documentation tool. It is not imported by the application and
touches no runtime data.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "docs" / "design" / "assets"

COMMIT = "main @ a4ac28b"
DATE = "2026-10-14"

# --- palette -----------------------------------------------------------------
INK = "#1F2937"
MUTED = "#4B5563"
FAINT = "#6B7280"
RULE = "#DCE3EA"
BAND = "#F7F9FB"

KINDS = {
    "ok": ("#E8F1FB", "#2E6FB7", False),      # implemented in the current commit
    "part": ("#FFF6E5", "#C97B0A", False),    # offline substitute or restricted scope
    "bad": ("#FDECEC", "#C0392B", True),      # required by the brief, absent or unverified
    "infra": ("#EEF2F5", "#6B7A8A", False),   # storage, runtime, deployment
    "plain": ("#FFFFFF", "#9AA7B4", False),
    "flat": ("#F2F6FA", "#C6D2DE", False),
}
ARROW = "#5B6B7C"
BAD_TEXT = "#9B2C1F"

SVG_FONT = "Microsoft YaHei, Microsoft YaHei UI, SimHei, Segoe UI, sans-serif"
FONT_PAIRS = (
    (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc"),
    (r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simhei.ttf"),
    (r"C:\Windows\Fonts\Deng.ttf", r"C:\Windows\Fonts\Deng.ttf"),
)


_MEASURE_CACHE: dict[bool, object] = {}


def _font_path(bold: bool) -> str | None:
    pairs = [(b, b) for _, b in FONT_PAIRS] if bold else [(p, p) for p, _ in FONT_PAIRS]
    for candidate, _ in pairs:
        if Path(candidate).exists():
            return candidate
    return None


def measure(text: str, size: float, bold: bool = False) -> float:
    """Approximate rendered width in canvas units; falls back to a rough estimate."""
    if not text:
        return 0.0
    try:
        from PIL import ImageFont
    except Exception:  # pragma: no cover - optional dependency
        return len(text) * size * 0.62
    if bold not in _MEASURE_CACHE:
        path = _font_path(bold)
        if path is None:
            return len(text) * size * 0.62
        _MEASURE_CACHE[bold] = ImageFont.truetype(path, 100)
    return _MEASURE_CACHE[bold].getlength(text) * size / 100.0  # type: ignore[attr-defined]


_TOKEN = None


def wrap(text: str, size: float, limit: float) -> list[str]:
    """Greedy wrap that keeps latin/digit runs intact and breaks between CJK characters."""
    import re
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = re.compile(r"[A-Za-z0-9_./+=%:{}\[\]()@-]+|\s+|.", re.S)
    lines: list[str] = []
    current = ""
    for token in _TOKEN.findall(text):
        candidate = current + token
        if current and measure(candidate.rstrip(), size) > limit:
            lines.append(current.rstrip())
            current = token.lstrip() if token.strip() == "" else token
        else:
            current = candidate
    if current.strip():
        lines.append(current.rstrip())
    return lines or [""]


class Canvas:
    """Collects drawing primitives; renderers turn them into SVG or PNG."""

    def __init__(self, width: int, height: int, title: str, subtitle: str | None = None):
        self.width = width
        self.height = height
        self.ops: list[tuple] = []
        self.warnings: list[str] = []
        self.check("标题", [title], 23, width - 80, bold=True)
        if subtitle:
            self.check("副标题", [subtitle], 12.5, width - 80)
            self.text(width // 2, 74, subtitle, size=12.5, color=MUTED, anchor="mt")
        self.text(width // 2, 38, title, size=23, bold=True, anchor="mt")

    def check(self, label: str, items, size: float, limit: float, *, bold: bool = False) -> None:
        for item in items:
            width = measure(item, size, bold)
            if width > limit:
                self.warnings.append(
                    f"[width] {label}: {width:.0f}px > {limit:.0f}px  ::  {item[:58]}")

    # -- primitives ----------------------------------------------------------
    def rect(self, x, y, w, h, *, kind="plain", fill=None, stroke=None,
             dash=False, radius=10, width=1.6):
        base_fill, base_stroke, base_dash = KINDS[kind]
        self.ops.append(("rect", x, y, w, h, fill or base_fill, stroke or base_stroke,
                         dash or base_dash, radius, width))

    def text(self, x, y, s, *, size=12.0, bold=False, color=INK, anchor="lt"):
        self.ops.append(("text", x, y, s, size, bold, color, anchor))

    def lines(self, x, y, items, *, size=12.0, color=MUTED, gap=1.5, bold_first=False):
        step = size * gap
        for index, item in enumerate(items):
            if item:
                self.text(x, y + index * step, item, size=size, color=color,
                          bold=bold_first and index == 0)

    def line(self, x1, y1, x2, y2, *, color=ARROW, width=1.6, dash=False):
        self.ops.append(("line", x1, y1, x2, y2, color, width, dash))

    def arrow(self, x1, y1, x2, y2, *, color=ARROW, width=1.8):
        self.ops.append(("arrow", x1, y1, x2, y2, color, width))

    # -- composites ---------------------------------------------------------
    def legend(self, y, items):
        x = 40
        for label, kind in items:
            fill, stroke, _ = KINDS[kind]
            self.rect(x, y, 16, 16, fill=fill, stroke=stroke, radius=4, width=1.4)
            self.text(x + 24, y + 2, label, size=12, color=MUTED)
            x += 26 + int(len(label) * 12.4) + 26

    def band(self, x, y, w, h, label):
        self.rect(x, y, w, h, fill=BAND, stroke=RULE, radius=12, width=1.2)
        self.text(x + 16, y + 9, label, size=12.5, bold=True, color=FAINT)

    def card(self, x, y, w, h, title, body, *, kind="ok", title_size=13.5,
             body_size=11.8, gap=1.55, padding=14, body_color=MUTED, label=None):
        self.rect(x, y, w, h, kind=kind)
        self.text(x + padding, y + 11, title, size=title_size, bold=True)
        if body:
            self.lines(x + padding, y + 11 + title_size * 1.95, body,
                       size=body_size, color=body_color, gap=gap)
        name = label or title
        limit = w - 2 * padding
        self.check(name, [title], title_size, limit, bold=True)
        self.check(name, body, body_size, limit)
        if body:
            needed = 11 + title_size * 1.95 + (len(body) - 1) * body_size * gap + body_size * 1.4
            if needed > h:
                self.warnings.append(
                    f"[height] {name}: needs {needed:.0f}px, card is {h}px")


# --- diagrams ----------------------------------------------------------------

def architecture() -> Canvas:
    c = Canvas(
        1680, 1360,
        f"风控盾 · 系统架构分层（{COMMIT} · {DATE}）",
        "实线蓝＝当前提交已实现并可在本机运行；橙＝仅离线替身或受限范围；红虚线＝命题要求但当前未实现或未验证",
    )
    c.legend(102, [("当前提交已实现", "ok"), ("仅离线替身 / 受限", "part"),
                   ("未实现或未验证", "bad"), ("存储与基础设施", "infra")])

    # A. 使用与界面
    c.band(40, 140, 1600, 120, "A · 使用与界面")
    c.card(58, 170, 980, 76, "Streamlit Web《风控盾》（ui/app.py）",
           ["六个页面：今日概览 · 事件证据 · 处置队列 · 五模块报告 · 预警中心 · 关口状态",
            "任务/图谱/报告/预警按 ID 互相跳转；进度手动刷新（不自动轮询）；显式显示 partial 与失败类别"])
    c.card(1054, 170, 568, 76, "运行边界提示",
           ["界面须标明 offline_dynamic_substitute、暂定判级与 dry_run",
            "来源证据图与模拟传播图必须分别标识"], kind="part")

    # B. 接口层
    c.band(40, 290, 1600, 196, "B · 接口层")
    c.card(58, 322, 1080, 148, "FastAPI（src/riskshield/api.py）· TrustedHost 限定 localhost / 127.0.0.1",
           ["/health　/readiness　/contracts　/channels/status",
            "/cases　/cases/{id}/snapshot　/cases/{id}/observations　/cases/{id}/labels",
            "/graphs/{id}　/graphs/{id}/query　/simulations　/simulations/{id}/trajectory",
            "/demos/day4/run　/day4/historical/jobs　/jobs　/reports　/alerts/assess　/alerts/{id}/previews"],
           body_size=11.6, gap=1.62)
    c.card(1156, 322, 466, 148, "启动与受理门",
           ["启动时校验 RISKSHIELD_MAX_INFLIGHT_CALLS、",
            "RISKSHIELD_SIMULATION_CALL_TIMEOUT_SECONDS、",
            "RISKSHIELD_TASK_TIMEOUT_SECONDS：写错即拒绝启动",
            "受理任务前经 PROCESS_CAPACITY.try_admit()，",
            "容量不足返回结构化拒绝，不排队掩盖资源不足"],
           kind="infra", body_size=11.4, gap=1.5)
    c.arrow(598, 262, 598, 288)
    c.text(608, 266, "HTTP（REST + JSON）", size=11.4, color=FAINT)

    # C. 业务链路
    c.band(40, 506, 1600, 372, "C · 业务链路（五段，按运行顺序）")
    cards = [
        ("① 来源与案例", ["evidence.py · schemas.py ·", "store.py · source_probe.py",
                          "导入案例、来源版本、许可与可见", "时间、cutoff 快照、观察与标签",
                          "受限来源采集：SEC / 同期帖子存档"], "ok"),
        ("② 截止证据图与检索", ["evidence.py",
                                "build_mode = offline_archived_record_",
                                "projection 或 day2_cutoff_evidence_graph",
                                "/graphs/{id}/query：词项匹配＋来源引用",
                                "× GraphRAG 自然语言问答未实现"], "part"),
        ("③ 多智能体仿真", ["simulation.py · tasks.py · capacity.py",
                            "角色：netizen / customer / media /",
                            "kol / regulator（独立人设与焦点）",
                            "独立情绪与信任状态、长期记忆、", "运行时消息；逐轮落账",
                            "执行模式 offline_dynamic_substitute"], "ok"),
        ("④ 报告与预警", ["reporting.py · alerts.py",
                          "五模块：propagation · emotion_evolution ·",
                          "key_nodes · risk · recommendations",
                          "四级：red ≥40% / orange ≥20% /",
                          "yellow ≥5% / blue；status 为暂定离线判级",
                          "通知：仅 dry_run 预览（不发送）"], "part"),
        ("⑤ 评测与预测准入", ["evaluation.py · evaluation_alignment.py ·",
                              "evaluation_annotation.py ·",
                              "evaluation_benchmark.py ·",
                              "evaluation_prediction_import.py ·",
                              "direction_prediction.py",
                              "冻结输入＋双人盲标＋预测准入白名单＋方向评分",
                              "▲ 仿真输出正式准入未打通"], "part"),
    ]
    x = 58
    step = 308
    for index, (title, body, kind) in enumerate(cards):
        c.card(x, 538, 288, 322, title, body, kind=kind, body_size=11.4, gap=1.62)
        if index < len(cards) - 1:
            c.arrow(x + 290, 698, x + step - 4, 698)
        x += step

    # D. 存储与运行时
    c.band(40, 898, 1600, 152, "D · 存储与运行时")
    c.card(58, 930, 1564, 96, "SQLite（RISKSHIELD_DB，默认 runtime/riskshield.db）",
           ["cases · snapshots · graphs · simulation_runs / agents / memories / messages / actions / rounds / request_metrics · jobs · reports · alerts · previews",
            "后台任务在 API 进程内以线程执行；每轮账本分别记录有效 / 失败 / 超时决策，以及消息条数与记忆引用",
            "API 进程重启会把仍处于 queued / running 的任务标为 failed（interrupted_on_restart），不自动续跑"],
           kind="infra", body_size=11.6, gap=1.62)

    # E. 部署与出口
    c.band(40, 1066, 1600, 148, "E · 部署与出口边界")
    c.card(58, 1098, 512, 100, "容器（Dockerfile + compose.yaml）",
           ["api / web / local_gateway 同一镜像 riskshield:day4-local",
            "命名卷 riskshield_runtime 持久化 SQLite",
            "default 网络 internal:true；仅 local_gateway 发布 127.0.0.1 端口"],
           kind="infra", body_size=11.3, gap=1.6)
    c.card(586, 1098, 512, 100, "在线模型网关（model_gateway.py）",
           ["需显式设置 RISKSHIELD_ENABLE_MODEL_PROBE=1 与模型密钥",
            "本机离线替身任务不调用：model_calls=0、network_requests=0",
            "该计数是应用运行账本，不是操作系统抓包证据"],
           kind="part", body_size=11.3, gap=1.6)
    c.card(1114, 1098, 508, 100, "出口隔离 × 未通过",
           ["API / Web 实测无默认路由",
            "local_gateway 仍有默认路由 → 不能承诺全容器无外发",
            "域内部署与全程流量审计尚无证据"],
           kind="bad", body_size=11.3, gap=1.6)

    # F. 缺口清单
    c.rect(40, 1230, 1600, 108, kind="bad", radius=12)
    c.text(58, 1242, "F · 命题要求但当前未实现或未验证", size=13, bold=True, color=BAD_TEXT)
    gap_lines = [
        "五平台在线覆盖与 ≤15 分钟热点采集延迟　·　生产级多平台采集（当前仅 SEC 与同期帖子存档的受限采集）　·　情感三分类模型",
        "GraphRAG 自然语言追问　·　500 Agent × 30 轮在真实历史事件上的完整贯通　·　真实企微/邮件送达与红警 ≤30 分钟时效　·　域内数据不出域",
    ]
    c.lines(58, 1268, gap_lines, size=11.6, color=BAD_TEXT, gap=1.65)
    c.check("F 缺口清单", gap_lines, 11.6, 1564)
    return c


def end_to_end_flow() -> Canvas:
    c = Canvas(
        1680, 1140,
        f"风控盾 · 端到端流程（{COMMIT} · {DATE}）",
        "从案例到五模块报告、四级预警与通知预览的实际控制流；方框左侧标签为承载该步骤的组件",
    )

    steps = [
        ("Web · 处置队列", "① 选择或导入案例，核对 data_mode、来源版本、cutoff 与 cutoff 依据",
         ["cutoff 之后才可见的材料只进评测侧（not_input），不得进入图与 Agent 上下文"], "ok"),
        ("API · evidence.py", "② 构建截止证据图：POST /cases/{id}/graph → graph_id",
         ["build_mode 记录为 offline_archived_record_projection 或 day2_cutoff_evidence_graph；本机投影不等于在线 GraphRAG"], "part"),
        ("API · simulation.py", "③ 创建运行配置：POST /simulations（或 /demos/day4/run、/day4/historical/jobs）→ run_id",
         ["配置 Agent 数（界面 1–500）、轮数（1–30）、并发（1–128）与费用上限（≤¥30.00 字段）"], "ok"),
        ("API · tasks.py", "④ 排入后台任务：POST /jobs → job_id（HTTP 202）",
         ["受理前经 PROCESS_CAPACITY.try_admit()；状态 queued → running；任务在 API 进程内线程执行"], "ok"),
        ("执行器 · 逐轮", "⑤ offline_dynamic_substitute 逐轮执行：读截止图证据＋自身记忆＋收到的消息 → 生成动作 → 写回状态与账本",
         ["每轮分别记录有效 / 失败 / 超时决策、消息条数与记忆引用；消息与记忆被引用不证明其对动作存在因果影响"], "part"),
        ("执行器 · 结束判定", "⑥ 全部决策有效 → complete；否则 partial / stopped / failed",
         ["API 重启中断 → failed（interrupted_on_restart），不自动续跑；一轮结束不等于所有决策有效"], "part"),
        ("reporting.py", "⑦ 生成五模块报告 → report_id（complete 自动生成；部分运行走受限路径）",
         ["传播 / 情绪演化 / 关键节点 / 风险 / 建议，各模块绑定同一案例、图与运行；传播与情绪是模拟结果，不是平台实测"], "ok"),
        ("alerts.py", "⑧ 判级：POST /alerts/assess → alert_id",
         ["red ≥40% / orange ≥20% / yellow ≥5% / blue，按最后一轮有效决策中模拟投诉意向占比；status = provisional_offline_assessment，阈值未经真实事件校准"], "part"),
        ("alerts.py", "⑨ 通知预览：POST /alerts/{id}/previews（企业微信 / 邮件）",
         ["delivery_status = dry_run、sent_at = null、network_requests = 0；预览明确写出 OFFLINE PREVIEW ONLY"], "part"),
        ("evidence.py", "⑩ 来源引用追问：POST /graphs/{id}/query",
         ["当前为词项匹配标题/摘要并返回来源引用与图邻域；× 带上下文的自然语言追问尚未实现"], "bad"),
    ]

    y = 150
    for index, (tag, headline, notes, kind) in enumerate(steps):
        c.rect(40, y, 1120, 84, kind=kind, radius=10)
        c.text(58, y + 12, tag, size=11.6, bold=True, color=FAINT)
        c.text(58, y + 32, headline, size=12.8, bold=True)
        c.lines(58, y + 56, notes, size=11.3, color=MUTED, gap=1.5)
        c.check(f"步骤 {tag}", [headline], 12.8, 1084, bold=True)
        c.check(f"步骤 {tag}", notes, 11.3, 1084)
        if index < len(steps) - 1:
            c.arrow(600, y + 86, 600, y + 100)
        y += 98

    # right-hand panel
    id_lines = ["case_id → graph_id → run_id →", "job_id → report_id → alert_id",
                "", "本机基线示例：", "case_id = unh_change_20240222_day2_multisource",
                "graph_id = 936c73951d2ab46f8e0ee6bb",
                "run_id = run_7aff1ceff6934a1da13e",
                "job_id = job_02aa0aed4a594ebe995c",
                "report_id = report_26db8f02583eb98bde5f7490",
                "alert_id = alert_19fe8d54fd95ea0876eee8b6323fd0c4"]
    c.rect(1190, 150, 450, 290, kind="infra", radius=12)
    c.text(1208, 162, "ID 链（同一次演示应能逐环对齐）", size=13, bold=True)
    c.lines(1208, 190, id_lines, size=11.4, color=MUTED, gap=1.55)
    c.check("ID 链", id_lines, 11.4, 414)

    timing_lines = [
        "命题要求：事件输入 → 五模块报告 ≤60 分钟；",
        "红警发现 → 实际推送 ≤30 分钟。",
        "本机实测：离线替身端到端 0.189624 秒；",
        "合成事件 500×30 的 931.709 秒只是仿真引擎阶段。",
        "两项都不能当作完整真实事件的模型 SLA。",
        "",
        "红警时效未验证：没有真实发送与送达回执，",
        "dry_run 预览耗时不是推送时效。",
        "",
        "complete（任务状态）不等于 G3 / G4 通过；",
        "仿真传播图不等于平台真实传播。",
    ]
    c.rect(1190, 460, 450, 300, kind="part", radius=12)
    c.text(1208, 472, "时限与口径（必须与图同时陈述）", size=13, bold=True)
    c.lines(1208, 500, timing_lines, size=11.4, color=MUTED, gap=1.55)
    c.check("时限与口径", timing_lines, 11.4, 414)

    doc_lines = [
        "连续演示路径：docs/delivery/user-guide.md 第 2 节",
        "任务进度与失败处理：同文档第 3 节",
        "证据图与五模块报告：第 4 节",
        "四级预警与通知预览：第 5、6 节",
        "部署与 Docker 运行：docs/delivery/deployment.md",
        "接口清单与请求示例：docs/delivery/api.md",
        "运行边界速查表：user-guide.md 末表",
    ]
    c.rect(1190, 780, 450, 180, kind="plain", radius=12)
    c.text(1208, 792, "每个步骤的对应文档", size=13, bold=True)
    c.lines(1208, 820, doc_lines, size=11.4, color=MUTED, gap=1.55)
    c.check("对应文档", doc_lines, 11.4, 414)

    marker_lines = ["offline_dynamic_substitute　·　暂定四级阈值　·　dry_run",
                    "来源证据图 ≠ 模拟传播图　·　合成案例 ≠ 真实历史回测"]
    c.rect(1190, 980, 450, 76, kind="bad", radius=12)
    c.text(1208, 992, "演示时不得省略的标注", size=12.6, bold=True, color=BAD_TEXT)
    c.lines(1208, 1018, marker_lines, size=11.2, color=BAD_TEXT, gap=1.5)
    c.check("演示标注", marker_lines, 11.2, 414)
    return c


def boundary_status() -> Canvas:
    c = Canvas(
        1680, 850,
        f"风控盾 · 能力边界与状态（{COMMIT} · {DATE}）",
        "同一张图上区分：已验证的本机工程 / 只能离线替身的范围 / 命题指标仍未实现或未验证的部分",
    )
    c.legend(102, [("已验证（有实际运行证据）", "ok"), ("仅离线替身或受限，不可外推", "part"),
                   ("未实现或未验证（命题指标缺口）", "bad")])

    columns = [
        ("● 已在当前提交验证", "ok", [
            "案例导入与 cutoff 快照（真实历史材料）",
            "受限来源采集：SEC 与同期帖子存档",
            "截止证据图构建 + 词项检索与来源引用",
            "离线动态替身 10 Agent × 3 轮：30/30 有效决策",
            "五模块报告生成（绑定同一案例 / 图 / 运行）",
            "四级暂定判级与规则版本留痕",
            "企业微信 / 邮件 dry_run 预览载荷",
            "Docker 本机：构建、健康检查、重启持久化",
            "应用运行账本 model_calls=0、network_requests=0",
        ]),
        ("▲ 只能说明这些，不能外推", "part", [
            "offline_dynamic_substitute：动态执行器，非真实模型推演",
            "10×3 替身 ≠ 500×30 真实模型能力",
            "500×30 的 15,000/15,000 有效决策来自合成事件，估算 ¥2.88427524",
            "方向预测适配器只通过离线合成标签路径",
            "四级阈值未经真实事件校准，判级为暂定",
            "dry_run 只证明载荷已生成，不证明送达",
            "应用账本零网络请求 ≠ 操作系统抓包结论",
        ]),
        ("× 未实现或未验证（命题指标）", "bad", [
            "五平台在线覆盖（realtime_collection=false）",
            "热点采集延迟 ≤15 分钟（无发现/采集时间序列）",
            "情感三分类 ≥88%：留出集 27/32 = 84.375%",
            "负面识别：二分类 29/32、精度 6/8、召回 6/7，正式口径待裁定",
            "走势方向一致率 ≥70%：二分类 2/5 = 40%",
            "GraphRAG 自然语言追问（graph_rag=false）",
            "500×30 真实历史事件链路与走势真值配对",
            "真实通知送达与红警 ≤30 分钟时效",
            "域内运行与数据不出域（无目标环境部署与审计）",
        ]),
    ]
    top = 140
    column_height = 340
    for index, (title, kind, items) in enumerate(columns):
        x = 40 + index * 546
        c.rect(x, top, 522, column_height, kind=kind, radius=12)
        c.text(x + 18, top + 14, title, size=14, bold=True,
               color=BAD_TEXT if kind == "bad" else INK)
        y = top + 52
        for item in items:
            lines = wrap(item, 11.5, 448)
            c.text(x + 18, y, "•  " + lines[0], size=11.5, color=MUTED)
            for extra in lines[1:]:
                y += 17.5
                c.text(x + 44, y, extra, size=11.5, color=MUTED)
            c.check(f"列 {index + 1}", lines, 11.5, 448)
            y += 30
        if y > top + column_height - 10:
            c.warnings.append(
                f"[height] 列 {index + 1} 内容延伸到 y={y:.0f}，卡片底部为 {top + column_height}")

    boundary_items = [
        "只有可证明在 cutoff 时已公开可见的输入候选才进入图与 Agent 上下文；未知可见时间、未来材料与 evaluation_only 记录不得混入。",
        "后续走势只在评分侧读取：未来结果不得进入图检索、记忆或推演；本机 1 条后续 not_input 材料已按此排除。",
        "投诉样本不代表公司全渠道投诉量；模拟 Agent 的投诉意向占比不是现实投诉率。",
        "对外陈述始终区分：方案 / 代码 / 模拟测试 / 真实运行 / 正式达标——partial、dry_run、离线替身与合成案例都不得写成达标。",
    ]
    c.rect(40, 500, 1600, 156, kind="plain", radius=12)
    c.text(58, 514, "数据与时间边界（横跨全部三列）", size=13.5, bold=True)
    y = 544
    for item in boundary_items:
        lines = wrap(item, 11.8, 1560)
        for offset, line in enumerate(lines):
            c.text(58, y + offset * 18, line, size=11.8, color=MUTED)
        c.check("数据与时间边界", lines, 11.8, 1560)
        y += len(lines) * 18 + 11

    gap_items = [
        "• 图纸描述的是当前提交的代码链路与已记录的运行状态，不是命题指标的验收结论：正式验收仍以新版命题的量化目标与逐项证据为准。",
        "• 本机过程证据（分支交接、运行账本、冻结材料）保存在 Git 忽略目录，随源码提交的版本不包含它们；此处引用的数字均来自已合并的实测记录。",
        "• 演示材料的其余部分（现场演示脚本、当前提交截图/录屏、效果验证材料、汇报 PPT）尚未产出；本图不能替代它们，也不能替代真实运行证据。",
    ]
    c.rect(40, 676, 1600, 150, kind="bad", radius=12)
    c.text(58, 690, "这份图纸没有覆盖、也不能替代的部分", size=13.5, bold=True, color=BAD_TEXT)
    y = 720
    for item in gap_items:
        lines = wrap(item, 11.8, 1560)
        for offset, line in enumerate(lines):
            c.text(58, y + offset * 18, line, size=11.8, color=BAD_TEXT)
        c.check("不能替代的部分", lines, 11.8, 1560)
        y += len(lines) * 18 + 11
    return c


DIAGRAMS = {
    "architecture-overview-v1": architecture,
    "end-to-end-flow-v1": end_to_end_flow,
    "boundary-status-v1": boundary_status,
}


# --- renderers ---------------------------------------------------------------

def _ascent_ratio() -> float:
    try:
        from PIL import ImageFont
        for regular, _ in FONT_PAIRS:
            if Path(regular).exists():
                font = ImageFont.truetype(regular, 100)
                return font.getmetrics()[0] / 100.0
    except Exception:  # pragma: no cover - optional dependency
        pass
    return 0.88


def render_svg(canvas: Canvas, path: Path) -> None:
    ascent = _ascent_ratio()
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{canvas.width}" '
        f'height="{canvas.height}" viewBox="0 0 {canvas.width} {canvas.height}" '
        f'font-family="{SVG_FONT}">',
        f'<rect width="{canvas.width}" height="{canvas.height}" fill="#FFFFFF"/>',
        '<defs><marker id="head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{ARROW}"/></marker></defs>',
    ]
    for op in canvas.ops:
        if op[0] == "rect":
            _, x, y, w, h, fill, stroke, dash, radius, width = op
            dash_attr = ' stroke-dasharray="7 5"' if dash else ""
            parts.append(
                f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="{width}"{dash_attr}/>')
        elif op[0] == "text":
            _, x, y, s, size, bold, color, anchor = op
            weight = ' font-weight="700"' if bold else ""
            text_anchor = ' text-anchor="middle"' if anchor == "mt" else ""
            baseline = y + size * ascent
            escaped = (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            parts.append(f'<text x="{x}" y="{baseline:.2f}" font-size="{size}" '
                         f'fill="{color}"{weight}{text_anchor}>{escaped}</text>')
        elif op[0] == "line":
            _, x1, y1, x2, y2, color, width, dash = op
            dash_attr = ' stroke-dasharray="6 5"' if dash else ""
            parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
                         f'stroke-width="{width}"{dash_attr}/>')
        elif op[0] == "arrow":
            _, x1, y1, x2, y2, color, width = op
            parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
                         f'stroke-width="{width}" marker-end="url(#head)"/>')
    parts.append("</svg>")
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def render_png(canvas: Canvas, path: Path, scale: float = 2.0) -> None:
    from PIL import Image, ImageDraw, ImageFont

    regular = next(p for p, _ in FONT_PAIRS if Path(p).exists())
    bold_path = next((b for _, b in FONT_PAIRS if Path(b).exists()), regular)
    cache: dict[tuple[float, bool], ImageFont.FreeTypeFont] = {}

    def font(size: float, bold: bool):
        key = (size, bold)
        if key not in cache:
            cache[key] = ImageFont.truetype(bold_path if bold else regular,
                                            max(1, round(size * scale)))
        return cache[key]

    image = Image.new("RGB", (round(canvas.width * scale), round(canvas.height * scale)), "white")
    draw = ImageDraw.Draw(image)

    def box(coords, **kwargs):
        draw.rounded_rectangle(coords, **kwargs)

    for op in canvas.ops:
        if op[0] == "rect":
            _, x, y, w, h, fill, stroke, dash, radius, width = op
            coords = [x * scale, y * scale, (x + w) * scale, (y + h) * scale]
            box(coords, radius=radius * scale, fill=fill)
            if dash:
                _dashed_rect(draw, coords, stroke, radius * scale, width * scale)
            else:
                box(coords, radius=radius * scale, outline=stroke, width=max(1, round(width * scale)))
        elif op[0] == "text":
            _, x, y, s, size, bold, color, anchor = op
            draw.text((x * scale, y * scale), s, font=font(size, bold), fill=color,
                      anchor="mt" if anchor == "mt" else "lt")
        elif op[0] == "line":
            _, x1, y1, x2, y2, color, width, _ = op
            draw.line([x1 * scale, y1 * scale, x2 * scale, y2 * scale], fill=color,
                      width=max(1, round(width * scale)))
        elif op[0] == "arrow":
            _, x1, y1, x2, y2, color, width = op
            _arrow(draw, (x1 * scale, y1 * scale), (x2 * scale, y2 * scale), color,
                   max(1, round(width * scale)), scale)

    image.save(path, "PNG", optimize=True)


def _dashed_rect(draw, coords, color, radius, width) -> None:
    x1, y1, x2, y2 = (round(value) for value in coords)
    radius = max(0, round(radius))
    width = max(1, round(width))
    dash, gap = 14, 10
    for x in range(x1 + radius, x2 - radius, dash + gap):
        draw.line([x, y1, min(x + dash, x2 - radius), y1], fill=color, width=width)
        draw.line([x, y2, min(x + dash, x2 - radius), y2], fill=color, width=width)
    for y in range(y1 + radius, y2 - radius, dash + gap):
        draw.line([x1, y, x1, min(y + dash, y2 - radius)], fill=color, width=width)
        draw.line([x2, y, x2, min(y + dash, y2 - radius)], fill=color, width=width)
    for cx, cy, start in ((x1, y1, 180), (x2 - 2 * radius, y1, 270),
                          (x2 - 2 * radius, y2 - 2 * radius, 0), (x1, y2 - 2 * radius, 90)):
        draw.arc([cx, cy, cx + 2 * radius, cy + 2 * radius], start=start, end=start + 90,
                 fill=color, width=width)


def _arrow(draw, start, end, color, width, scale) -> None:
    x1, y1 = start
    x2, y2 = end
    draw.line([x1, y1, x2, y2], fill=color, width=width)
    head = 9 * scale
    if abs(x2 - x1) >= abs(y2 - y1):
        direction = 1 if x2 >= x1 else -1
        points = [(x2, y2), (x2 - direction * head, y2 - head * 0.45),
                  (x2 - direction * head, y2 + head * 0.45)]
    else:
        direction = 1 if y2 >= y1 else -1
        points = [(x2, y2), (x2 - head * 0.45, y2 - direction * head),
                  (x2 + head * 0.45, y2 - direction * head)]
    draw.polygon(points, fill=color)


def missing_glyphs(canvas: "Canvas") -> list[str]:
    """Characters the chosen font has no glyph for (they render as an empty box)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:  # pragma: no cover - optional dependency
        return []
    path = _font_path(False)
    if path is None:
        return []
    font = ImageFont.truetype(path, 40)

    def render(char: str) -> bytes:
        image = Image.new("L", (64, 64), 0)
        ImageDraw.Draw(image).text((6, 6), char, font=font, fill=255)
        return image.tobytes()

    reference = render("\ue000")  # private use area: always .notdef
    missing: list[str] = []
    seen: set[str] = set()
    for op in canvas.ops:
        if op[0] != "text":
            continue
        for char in op[3]:
            if ord(char) < 0x80 or char in seen or char.isspace():
                continue
            seen.add(char)
            if render(char) == reference:
                missing.append(f"{char} (U+{ord(char):04X})")
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--scale", type=float, default=2.0, help="PNG pixels per canvas unit")
    parser.add_argument("--only", choices=sorted(DIAGRAMS), action="append",
                        help="render a single diagram (repeatable)")
    args = parser.parse_args(argv)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    try:
        import PIL  # noqa: F401
        have_pillow = True
    except Exception:
        have_pillow = False

    names = args.only or sorted(DIAGRAMS)
    warnings: list[str] = []
    for name in names:
        canvas = DIAGRAMS[name]()
        warnings.extend(f"{name}: {item}" for item in canvas.warnings)
        for glyph in missing_glyphs(canvas):
            warnings.append(f"{name}: [glyph] 字体缺少字形 {glyph}")
        svg_path = args.out_dir / f"{name}.svg"
        render_svg(canvas, svg_path)
        print(f"wrote {svg_path.relative_to(REPO_ROOT)}")
        if have_pillow:
            png_path = args.out_dir / f"{name}.png"
            render_png(canvas, png_path, scale=args.scale)
            print(f"wrote {png_path.relative_to(REPO_ROOT)}")
        else:
            print(f"skipped PNG for {name}: Pillow is not importable in this interpreter",
                  file=sys.stderr)
    for item in warnings:
        print(f"WARNING {item}", file=sys.stderr)
    return 1 if warnings else 0


if __name__ == "__main__":
    raise SystemExit(main())
