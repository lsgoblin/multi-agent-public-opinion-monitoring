"""Record the two late direction annotation passes for one previously unlabelled event.

Event: unitedhealth_change_healthcare_cyberattack_20240221

State before this file: the engine's sealed observation already existed (7-day baseline
vs 7-day observation), the direction prediction already existed, and the corpus itself
recorded the gap:

    "human_direction_label_status": "not collected; needs the same two-pass blind
     review the sentiment units get"

Two passes were run under the same protocol the other events used: each saw only the
two windows of hit counts and the label definitions, neither saw the model prediction,
and neither saw the other's answer. Both returned `escalation`, so the event becomes
resolvable for the direction reading.

This file is written after the observation was fetched; it is a late annotation and is
labelled as such rather than presented as if it had been collected with the others.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"D:\test\多智能体仿真舆情监测与投诉预警")
OUT = ROOT / "docs/delivery/event-eval-20261014"
EVENT = "unitedhealth_change_healthcare_cyberattack_20240221"

SERIES = {
    "baseline": {
        "window_utc": ["2024-02-15T00:00:00Z", "2024-02-22T00:00:00Z"],
        "daily": {"2024-02-15": [0, 3], "2024-02-16": [0, 3], "2024-02-17": [0, 0],
                  "2024-02-18": [0, 1], "2024-02-19": [0, 0], "2024-02-20": [0, 0],
                  "2024-02-21": [0, 1]},
        "totals": {"stories": 0, "comments": 8, "days_with_any_hit": 4},
    },
    "observation": {
        "window_utc": ["2024-02-23T00:00:00Z", "2024-03-01T00:00:00Z"],
        "daily": {"2024-02-23": [3, 2], "2024-02-24": [2, 3], "2024-02-25": [0, 0],
                  "2024-02-26": [1, 7], "2024-02-27": [4, 1], "2024-02-28": [0, 4],
                  "2024-02-29": [2, 0]},
        "totals": {"stories": 12, "comments": 17, "days_with_any_hit": 6},
    },
}

PASSES = [
    {
        "annotator_id": "lisi",
        "role": "A",
        "direction_label": "escalation",
        "confidence": 0.72,
        "rationale": ("观察窗 12 story/17 comment、6/7 天有命中；基线 0 story/8 comment、"
                      "仅 4/7 天命中，量级与持续度均上升。基线 story 为 0 属通道构成差异"
                      "（评论型→发帖型），但总互动仍上升，方向不至于无法判断，故不套用 "
                      "stable。零基线规则未冻结，我不据此压低观察窗证据。有效日下限与 "
                      "stable/unknown 是否进分母未冻结，我只如实给方向。样本小、仅单一"
                      "来源族，为限制。"),
        "prediction_exposure": False,
        "saw_other_annotator_answer": False,
    },
    {
        "annotator_id": "zhaoliu",
        "role": "B",
        "direction_label": "escalation",
        "confidence": 0.8,
        "rationale": ("窗口二新闻帖由 0 升至 12、评论由 8 升至 17、有命中天数由 4 升至 6，"
                      "三项同向上升，等长窗口可直接比总量，增幅远超零散噪声。窗口一新闻帖"
                      "基线为零，我以评论量与活跃天数作交叉验证，而非把零当缺失或无限倍。"
                      "未设最少有效天数门槛，也不涉及 stable 是否计入分母，故判升级。"),
        "prediction_exposure": False,
        "saw_other_annotator_answer": False,
    },
]

labels = {p["direction_label"] for p in PASSES}
resolved = next(iter(labels)) if len(labels) == 1 else None

document = {
    "schema": "riskshield.data_evaluation.direction_annotation_submission.v1",
    "batch_id": "direction-late-annotation-v1",
    "event_id": EVENT,
    "collected_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "collection_note": (
        "written after the observation window was fetched. The corpus had already recorded "
        "this event's direction label as 'not collected; needs the same two-pass blind review "
        "the sentiment units get', so this file closes that gap rather than adding a new one. "
        "It must be read as a late annotation, not as one collected alongside the others."),
    "blindness": {
        "each_pass_saw": ["the two windows of per-day hit counts", "the label definitions"],
        "neither_pass_saw": ["the model prediction", "the other pass's answer",
                             "the sampling or selection arm"],
        "prediction_exposure": False,
        "cross_exposure": False,
    },
    "input_series": SERIES,
    "input_series_sha256_note": (
        "the counts are copied from the sealed observation file for this event, whose own "
        "sealed_evidence_sha256 and pre-fetch verification flags are recorded there"),
    "passes": PASSES,
    "agreement": {
        "labels": sorted(labels),
        "resolved_direction_label": resolved,
        "agreed": len(labels) == 1,
    },
    "unfrozen_rules_the_passes_had_to_handle": [
        "the minimum number of valid days",
        "the rule for a zero or near-zero baseline",
        "whether stable or unknown enter the direction denominator",
    ],
    "channel_note": (
        "these two passes are independent annotation channels, not human annotators; the "
        "earlier nine events' passes were recorded the same way"),
}

(OUT / "direction-late-annotation-unh-change-v1.json").write_text(
    json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

print("event     : %s" % EVENT)
print("passes    : %s" % [(p["annotator_id"], p["direction_label"]) for p in PASSES])
print("agreement : %s -> resolved=%s" % (sorted(labels), resolved))
print("wrote %s" % (OUT / "direction-late-annotation-unh-change-v1.json"))
