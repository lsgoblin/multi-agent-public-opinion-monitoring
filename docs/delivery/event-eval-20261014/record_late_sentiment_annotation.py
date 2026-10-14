"""Record the two late annotation passes for the three newly predicted events.

Events and their frozen titles (chosen by the rule fixed in run_late_three_sentiment.py
before any title was read). The wording itself is not copied here; each row below keeps
the text hash, and the verbatim text stays in the ignored local copy:

    ftc_pbm_insulin_complaint_20240920      text_sha256 bfaaa3f7104f7cec...
                                            (FTC action against insulin middlemen, NPR)
    unh_change_cyberattack_20240222         text_sha256 d2056838c0c93fa8...
                                            (UnitedHealth attribution of the Change
                                             Healthcare attack, Fierce Healthcare)
    unitedhealth_ceo_shooting_20241204      text_sha256 305410542d024135...
                                            (public reaction to the CEO's murder, Bloomberg)

Two passes, each shown only the three titles and the label definitions. Neither saw the
model predictions and neither saw the other's answer.

Outcome:

    event 1  negative / negative  -> resolved
    event 2  neutral  / neutral   -> resolved
    event 3  neutral  / negative  -> NOT resolved, and deliberately left unresolved

Event 3 is the interesting one. The frozen rule (label-rule-v2.json) states that directly
stating the subject's own adverse situation is negative, and that reciting a third party's
allegation is neutral. This title reports a public reaction rather than the company's own
adverse situation, and the mocking quote is attributed to Americans, not to the company.
That reading supports neutral; the opposite reading treats a public mocking of the company
as the company's adverse situation. The rule does not settle which applies, and the same
rule document says in its own words:

    "effect_on_labels": "none applied by this document. Relabelling requires an
     adjudication record with a named human decider."

No named human decider is available in this session, so this file does not choose. The
event stays out of the sentiment denominator, exactly as the direction track leaves its
one disagreement out.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"D:\test\多智能体仿真舆情监测与投诉预警")
OUT = ROOT / "docs/delivery/event-eval-20261014"

STORIES = {
    "ftc_pbm_insulin_complaint_20240920": {
        "title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
        "text_sha256": "bfaaa3f7104f7cec1f835352035abe3e",
    },
    "unh_change_cyberattack_20240222": {
        "title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
        "text_sha256": "d2056838c0c93fa8c49779fb11c5f365",
    },
    "unitedhealth_ceo_shooting_20241204": {
        "title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
        "text_sha256": "305410542d024135a81c8ac6b7ed641d",
    },
}

PASSES = [
    {
        "annotator_id": "Leo",
        "role": "A",
        "labels": {
            "ftc_pbm_insulin_complaint_20240920": (
                "negative", "措辞为负面指控：中间商牟利数十亿、患者承担高额费用，含不满与指责立场。"),
            "unh_change_cyberattack_20240222": (
                "neutral", "仅转述公司对攻击来源的推测归因，未作评价，也未陈述自身不利结果。"),
            "unitedhealth_ceo_shooting_20241204": (
                "neutral", "转述公众反应并引用讽语，主体未遭不利也未表明编者立场。"),
        },
    },
    {
        "annotator_id": "Dick",
        "role": "B",
        "labels": {
            "ftc_pbm_insulin_complaint_20240920": (
                "negative", "FTC 起诉中间商牟利致患者负担高，措辞明确指向指责与损害"),
            "unh_change_cyberattack_20240222": (
                "neutral", "仅转述公司对攻击源的说法，属未证实指称，无评价立场"),
            "unitedhealth_ceo_shooting_20241204": (
                "negative", "引语“共情不在网络内”讥讽该公司，标题措辞明显负向"),
        },
    },
]

records = []
for event_id in STORIES:
    labels = {p["annotator_id"]: p["labels"][event_id][0] for p in PASSES}
    reasons = {p["annotator_id"]: p["labels"][event_id][1] for p in PASSES}
    agreed = len(set(labels.values())) == 1
    records.append({
        "event_id": event_id,
        "frozen_text": STORIES[event_id]["title"],
        "text_sha256_prefix": STORIES[event_id]["text_sha256"],
        "passes": [{"annotator_id": a, "label": lab, "reason": reasons[a]}
                   for a, lab in labels.items()],
        "labels_agree": agreed,
        "resolved_label": next(iter(set(labels.values()))) if agreed else None,
        "disposition": ("scored" if agreed else
                        "excluded: the two passes disagree, and the frozen rule does not "
                        "settle which reading applies, so no label is chosen"),
    })

document = {
    "schema": "riskshield.data_evaluation.sentiment_late_annotation.v1",
    "batch_id": "sentiment-late-three-v1",
    "collected_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "collection_note": (
        "written after the three titles were frozen and predicted. The titles come from each "
        "event's own baseline window, chosen by the rule frozen before any title was read. "
        "The observation windows of these events existed before this collection; that is a "
        "late freeze and is recorded as one in the run's freeze record."),
    "blindness": {
        "each_pass_saw": ["the three frozen titles", "the label definitions"],
        "neither_pass_saw": ["the model predictions", "the other pass's answer"],
        "prediction_exposure": False,
        "cross_exposure": False,
    },
    "records": records,
    "adjudication": {
        "applied": False,
        "why": ("the one disagreement is left unresolved. The frozen rule's own text says "
                "relabelling requires an adjudication record with a named human decider, and "
                "no such decider is available in this session. Choosing a label here would "
                "either overrule a pass by fiat or break the rule document's own constraint."),
        "consequence": "that event stays out of the sentiment denominator",
    },
    "channel_note": "two independent annotation channels, not human annotators",
}

(OUT / "sentiment-late-annotation-three-v1.json").write_text(
    json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

print("late sentiment annotation, three events:")
for record in records:
    print("  %-46s %s -> %s"
          % (record["event_id"],
             " / ".join("%s=%s" % (p["annotator_id"], p["label"]) for p in record["passes"]),
             record["resolved_label"] or "EXCLUDED (disagreement)"))
print()
print("scored : %d" % sum(1 for r in records if r["labels_agree"]))
print("excluded: %d" % sum(1 for r in records if not r["labels_agree"]))
