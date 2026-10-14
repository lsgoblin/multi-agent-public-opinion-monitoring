"""Consistency check across the three published result files.

Reads the record-level JSON, the event-level JSON, the two markdown reports and the
delivery test report, and asserts that every headline number agrees and that no file
claims 94 records are 94 independent events or human-annotated truths. Read-only.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(r"D:\test\多智能体仿真舆情监测与投诉预警")
OUT = ROOT / "docs/delivery/event-eval-20261014"

record = json.loads((OUT / "frozen-result-94-events-v1.json").read_text(encoding="utf-8"))
event = json.loads((OUT / "event-level-result-v2.json").read_text(encoding="utf-8"))
v1_md = (OUT / "real-historical-evaluation-result-v1.md").read_text(encoding="utf-8")
v2_md = (OUT / "event-level-evaluation-result-v2.md").read_text(encoding="utf-8")
report_md = (ROOT / "docs/delivery/test-report.md").read_text(encoding="utf-8")

failures: list[str] = []


def expect(label, condition):
    print("   %-72s %s" % (label, "OK" if condition else "FAIL"))
    if not condition:
        failures.append(label)


print("internal consistency of the frozen JSONs:")
sent = event["sentiment_track"]
dire = event["direction_track"]
expect("record-level denominator is 94",
       record["track_sentiment"]["distinct_real_events"] == 94)
expect("event-level sentiment is 7/10",
       (sent["three_class_accuracy"]["numerator"], sent["three_class_accuracy"]["denominator"]) == (7, 10))
expect("event-level direction binary is 3/6",
       (dire["direction_agreement_binary"]["numerator"],
        dire["direction_agreement_binary"]["denominator"]) == (3, 6))
expect("event-level direction three-class is 3/10",
       (dire["direction_three_class_diagnostic"]["numerator"],
        dire["direction_three_class_diagnostic"]["denominator"]) == (3, 10))
expect("event-level breakdown accounts for all 94 rows",
       event["unit_definition"]["record_level_breakdown"]["sum"] == 94
       and sum(v for k, v in event["unit_definition"]["record_level_breakdown"].items()
               if k != "sum") == 94)
expect("six named events sit inside the 94 rows",
       event["unit_definition"]["record_level_breakdown"]["named_events_within_the_94_rows"] == 6)
expect("direction truth now contains exactly one escalation",
       dire["truth_distribution"].get("escalation") == 1)
expect("direction predictions still never say stable",
       "stable" not in dire["predicted_distribution"])
expect("one sentiment event carries an adjudicated basis",
       sum(1 for r in sent["events"] if r.get("label_basis") == "adjudicated_disagreement") == 1)
expect("no sentiment event is excluded any more",
       sent["excluded_events"] == [])
expect("the late direction event is recorded as late",
       dire["late_collected_events"] == ["unitedhealth_change_healthcare_cyberattack_20240221"])
expect("both tracks reach the brief's lower bound of ten events",
       sent["evaluable_events"] >= 10 and dire["resolved_events"] >= 10)

print()
print("the two markdown reports agree with the JSONs:")
for token in ("84.04%", "89.19%", "80.49%", "79 / 94", "33 / 37"):
    expect("v1 report carries %s" % token, token in v1_md)
for token in ("70.00%", "100%", "50.00%", "30.00%"):
    expect("v2 report carries %s" % token, token in v2_md)
for pattern, description in ((r"7/10", "sentiment 7/10"), (r"3/6", "direction 3/6"),
                             (r"3/10", "direction 3/10")):
    expect("v2 report carries %s" % description, re.search(pattern, v2_md) is not None)

print()
print("the delivery report carries both levels:")
for token in ("84.04%", "89.19%", "70.00%", "50.00%", "30.00%"):
    expect("test-report carries %s" % token, token in report_md)

print()
print("the claims that must not appear anywhere:")
banned = [
    (r"94\s*件独立真实(历史)?事件", "describes the 94 records as 94 independent events"),
    (r"两名真人独立标注", "claims two human annotators"),
    (r"人工标注员.{0,10}(填写|完成)", "claims human annotators filled the labels"),
]
for text, name, source in ((v1_md, "v1 report", "real-historical-evaluation-result-v1.md"),
                           (v2_md, "v2 report", "event-level-evaluation-result-v2.md"),
                           (report_md, "test report", "docs/delivery/test-report.md")):
    for pattern, description in banned:
        hit = re.search(pattern, text)
        # the phrase is allowed only inside an explicit negation
        if hit:
            window = text[max(0, hit.start() - 40):hit.end() + 40]
            negated = any(word in window for word in ("不得", "不把", "不是", "不称为", "不声称", "没有"))
            expect("%s: %s (negated=%s)" % (source, description, bool(negated)), negated)
        else:
            expect("%s: %s absent" % (source, description), True)

print()
print("=" * 78)
if failures:
    print("VERDICT: %d consistency failure(s)" % len(failures))
    for item in failures:
        print("   - %s" % item)
    sys.exit(1)
print("VERDICT: all three result files and the delivery report agree, and no file")
print("         overstates the 94 records or the annotation channels.")
