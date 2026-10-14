"""Adjudicate the one unresolved sentiment disagreement, on the owner's decision.

Event:  unitedhealth_ceo_shooting_20241204
Title:  "<verbatim title held in the ignored local copy; verify by text_sha256>"
Pass A: neutral (Leo)
Pass B: negative (Dick)

The two passes disagreed. The frozen rule document (label-rule-v2.json) states that
relabelling requires an adjudication record with a named human decider, so this file
exists only because the project owner acted as that decider.

The owner's decision and its stated basis:

    negative — the title points the mockery at the company through the quoted line, which
    puts the company in an adverse public-opinion position; the rule's own instruction
    that "no sentiment word" cannot by itself justify neutral supports reading past the
    surface reporting tone.

Both original answers are preserved verbatim and are still what the agreement audit counts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"D:\测试\多智能体仿真舆情监测与投诉预警") if False else Path(
    r"D:\test\多智能体仿真舆情监测与投诉预警")
OUT = ROOT / "docs/delivery/event-eval-20261014"

EVENT = "unitedhealth_ceo_shooting_20241204"
TITLE = "<verbatim title held in the ignored local copy; verify by text_sha256>"
TEXT_SHA256_PREFIX = "305410542d024135a81c8ac6b7ed641d"

ORIGINAL = [
    {"annotator_id": "Leo", "label": "neutral",
     "reason": "转述公众反应并引用讽语，主体未遭不利也未表明编者立场。"},
    {"annotator_id": "Dick", "label": "negative",
     "reason": "引语“共情不在网络内”讥讽该公司，标题措辞明显负向。"},
]

document = {
    "schema": "riskshield.data_evaluation.sentiment_adjudication.v1",
    "created_for": "the one unresolved sentiment label disagreement among the late three",
    "decided_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "decided_by": "project owner",
    "rule_arm": "label-rule-v2.json",
    "rule_source": "artifacts/branch-work/data-evaluation/evidence/20261013/v08/label-rule-v2.json",
    "labels_changed_by_this_record": 1,
    "records": [
        {
            "event_id": EVENT,
            "frozen_text": TITLE,
            "text_sha256_prefix": TEXT_SHA256_PREFIX,
            "original_annotation_labels": [row["label"] for row in ORIGINAL],
            "original_annotator_ids": [row["annotator_id"] for row in ORIGINAL],
            "original_answers_preserved": True,
            "both_original_answers_preserved": True,
            "raw_agreement_before_adjudication": False,
            "resolved_label": "negative",
            "resolution": (
                "the title points the mockery at the company through the quoted line, which "
                "places the company in an adverse public-opinion position; the rule's own "
                "instruction that a missing sentiment word cannot by itself justify neutral "
                "supports reading past the surface reporting tone"),
            "owner_note": (
                "the owner chose negative over neutral and cannot_judge; the neutral reading "
                "(the title merely reports a third party's reaction) is recorded and remains "
                "visible in the original answers"),
            "boundary_used": (
                "fact_versus_tone: an adverse situation does not turn neutral because the "
                "wording reports it rather than evaluates it"),
        }
    ],
}

(OUT / "sentiment-adjudication-unitedhealth-ceo-v1.json").write_text(
    json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

print("adjudication written: %s" % EVENT)
print("  decided_by     : %s" % document["decided_by"])
print("  rule arm       : %s" % document["rule_arm"])
print("  original labels: %s" % document["records"][0]["original_annotation_labels"])
print("  resolved label : %s" % document["records"][0]["resolved_label"])
print("  originals kept : %s" % document["records"][0]["both_original_answers_preserved"])
