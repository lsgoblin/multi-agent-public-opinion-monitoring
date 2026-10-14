"""Freeze the input title for three events, then predict its sentiment with a real model.

Why these three
---------------
They are the events that carry a direction result but no sentiment result, because the
frozen corpus holds only their aggregate hit counts, never an archived title:

    ftc_pbm_insulin_complaint_20240920              baseline 1 story
    unh_change_cyberattack_20240222                 baseline 12 stories
    unitedhealth_ceo_shooting_20241204              baseline 37 stories

Selection rule, fixed in this file BEFORE any title was read or any prediction made:

    within the event's own frozen baseline window and its own frozen query, take the
    story with the most comments; break ties by earliest creation time; if the window
    holds no story, the event gets no sentiment unit.

The rule is mechanical, it uses only the baseline window (never the observation window),
and it is the same rule for all three events. The full window listing is written next to
the choice so an auditor can see what else was in the window.

What this run does and does not do
----------------------------------
* It sends three title strings to qwen-flash and records the labels. Real call, real cost.
* Which three strings: decided by the rule above, not by what the model says about them.
* It does NOT touch the observation windows, and it reads no label.
* Every label still needs two independent annotation passes before it becomes scoreable,
  which is a separate step.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"D:\test\多智能体仿真舆情监测与投诉预警")
sys.path.insert(0, str(ROOT / "src"))
OUT = ROOT / "docs/delivery/event-eval-20261014"
WORK = OUT / "sentiment-late-run"

from riskshield import sentiment  # noqa: E402

EXECUTION_MODE = "formal_model_run_v1"
METHOD_VERSION = "bailian-qwen-flash-late-title-sentiment-v1"
SYSTEM_PROMPT = (
    "你是舆情情感标注器。只判断给定标题文字本身表达的情感倾向，"
    "不判断底层事件真假、严重程度或公众整体情绪。\n"
    "标签定义：\n"
    "- negative：标题明确表达不满、担忧、指责、损失或其他负向评价。\n"
    "- neutral：标题主要陈述事件或引用诉讼指称，没有清晰的评价立场。\n"
    "- positive：标题明确表达赞许、支持、满意或其他正向评价。\n"
    "只返回 JSON 对象，恰有 label 与 reason 两个字段；label 只能是 "
    "positive、neutral、negative 之一。")
USER_TEMPLATE = "标题：<见冻结输入>"

# ---------------------------------------------------------------- frozen windows
# Per-day and per-story counts come from the sealed observation files; the story
# listings below were fetched on 2026-10-14 with each event's own frozen query and
# numeric filter. Only stories inside the baseline window are kept.
EVENTS = {
    "ftc_pbm_insulin_complaint_20240920": {
        "query": "pharmacy benefit manager",
        "cutoff": "2024-09-27T00:00:00Z",
        "baseline_window": ["2024-09-20T00:00:00Z", "2024-09-27T00:00:00Z"],
        "baseline_totals": {"stories": 1, "comments": 2, "days_with_any_hit": 3},
        "stories": [
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "41613182", "created_at": "2024-09-21T22:39:37Z",
             "num_comments": 1, "points": 32,
             "url": "https://www.npr.org/2024/09/21/nx-s1-5121886/insulin-ftc-lawsuit-pharmacy-benefit-manager"},
        ],
    },
    "unh_change_cyberattack_20240222": {
        "query": "Change Healthcare",
        "cutoff": "2024-02-29T00:00:00Z",
        "baseline_window": ["2024-02-22T00:00:00Z", "2024-02-29T00:00:00Z"],
        "baseline_totals": {"stories": 12, "comments": 19, "days_with_any_hit": 6},
        "stories": [
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "39461914", "created_at": "2024-02-22T01:19:06Z",
             "num_comments": 0, "points": 5,
             "url": "https://status.changehealthcare.com/incidents/hqpjz25fn3n7"},
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "39470316", "created_at": "2024-02-22T17:33:10Z",
             "num_comments": 0, "points": 2,
             "url": "https://techcrunch.com/2024/02/21/change-healthcare-cyberattack/"},
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "39475342", "created_at": "2024-02-23T00:32:53Z",
             "num_comments": 0, "points": 3,
             "url": "https://www.reuters.com/business/healthcare-pharmaceuticals/change-healthcare-network-hit-by-cybersecurity-attack-2024-02-22/"},
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "39475448", "created_at": "2024-02-23T00:46:11Z",
             "num_comments": 0, "points": 2,
             "url": "https://status.changehealthcare.com/incidents/hqpjz25fn3n7"},
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "39479930", "created_at": "2024-02-23T12:52:42Z",
             "num_comments": 0, "points": 6,
             "url": "https://techcrunch.com/2024/02/21/change-healthcare-cyberattack/"},
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "39492021", "created_at": "2024-02-24T15:10:11Z",
             "num_comments": 0, "points": 2,
             "url": "https://www.scmagazine.com/news/exclusive-cyberattack-on-change-healthcare-was-an-exploit-of-the-connectwise-flaw"},
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "39494632", "created_at": "2024-02-24T19:53:35Z",
             "num_comments": 1, "points": 3,
             "url": "https://www.fiercehealthcare.com/payers/optums-change-healthcare-responding-cybersecurity-issue"},
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "39513584", "created_at": "2024-02-26T16:51:14Z",
             "num_comments": 0, "points": 1,
             "url": "https://www.modernhealthcare.com/cybersecurity/change-healthcare-outage-cyberattack-2024-update"},
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "39519057", "created_at": "2024-02-27T01:44:22Z",
             "num_comments": 0, "points": 3,
             "url": "https://www.reuters.com/technology/cybersecurity/cyber-security-outage-change-healthcare-continues-sixth-straight-day-2024-02-26/"},
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "39519558", "created_at": "2024-02-27T02:47:31Z",
             "num_comments": 1, "points": 2,
             "url": "https://techcrunch.com/2024/02/26/ransomware-attack-change-healthcare-prescription-pharmacy-outages/"},
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "39524514", "created_at": "2024-02-27T14:29:50Z",
             "num_comments": 0, "points": 7,
             "url": "https://www.reuters.com/technology/cybersecurity/cyber-security-outage-change-healthcare-continues-sixth-straight-day-2024-02-26/"},
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "39525619", "created_at": "2024-02-27T15:59:44Z",
             "num_comments": 1, "points": 1,
             "url": "https://techcrunch.com/2024/02/26/ransomware-attack-change-healthcare-prescription-pharmacy-outages/"},
        ],
    },
    "unitedhealth_ceo_shooting_20241204": {
        "query": "UnitedHealth",
        "cutoff": "2024-12-11T00:00:00Z",
        "baseline_window": ["2024-12-04T00:00:00Z", "2024-12-11T00:00:00Z"],
        "baseline_totals": {"stories": 37, "comments": 124, "days_with_any_hit": 7},
        "stories": [
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "42317604", "created_at": "2024-12-04T14:17:23Z",
             "num_comments": 460, "points": 107,
             "url": "https://www.bloomberg.com/news/articles/2024-12-04/unitedhealthcare-ceo-brian-thompson-fatally-shot-in-manhattan"},
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "42319995", "created_at": "2024-12-04T17:52:34Z",
             "num_comments": 2, "points": 21,
             "url": "https://www.reuters.com/world/us/unitedhealthcare-ceo-fatally-shot-ny-post-reports-2024-12-04/"},
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "42327272", "created_at": "2024-12-05T11:52:44Z",
             "num_comments": 475, "points": 182,
             "url": "https://gizmodo.com/bitter-americans-react-to-unitedhealthcare-ceos-murder-my-empathy-is-out-of-network-2000534520"},
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "42357720", "created_at": "2024-12-08T15:39:29Z",
             "num_comments": 373, "points": 297,
             "url": "https://www.propublica.org/article/unitedhealth-healthcare-insurance-denial-ulcerative-colitis"},
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "42367117", "created_at": "2024-12-09T15:32:12Z",
             "num_comments": 1, "points": 8,
             "url": "https://www.wsj.com/us-news/manhunt-for-unitedhealthcare-ceo-killer-meets-unexpected-obstacle-sympathy-for-the-gunman-31276307"},
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "42367930", "created_at": "2024-12-09T17:01:04Z",
             "num_comments": 24, "points": 33,
             "url": "https://www.theatlantic.com/ideas/archive/2024/12/unitedhealthcare-ceo-assassination-investigation/680903/"},
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "42369465", "created_at": "2024-12-09T19:17:53Z",
             "num_comments": 1, "points": 49,
             "url": "https://nypost.com/2024/12/09/us-news/person-of-interest-in-fatal-shooting-of-unitedhealthcare-boss-brian-thompson-idd-as-luigi-mangione-an-ex-ivy-league-student/"},
            {"title": "<verbatim title held in the ignored local copy; verify by text_sha256>",
             "objectID": "42372277", "created_at": "2024-12-10T00:33:27Z",
             "num_comments": 2, "points": 1,
             "url": "https://www.foxnews.com/live-news/unitedhealthcare-ceo-brian-thompson-murder-live-updates-12-09-2024"},
        ],
    },
}


def select_unit(event):
    """The frozen rule: most comments wins, ties go to the earlier creation time."""
    stories = event["stories"]
    if not stories:
        return None, "the baseline window holds no story"
    best = sorted(stories, key=lambda s: (-s["num_comments"], s["created_at"]))[0]
    return best, ("most comments in the baseline window (%d); ties broken by earliest "
                  "creation time" % best["num_comments"])


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--budget-cny", type=float, default=1.0)
    args = parser.parse_args()

    units, listing = {}, {}
    for event_id, event in EVENTS.items():
        chosen, why = select_unit(event)
        listing[event_id] = {
            "query": event["query"], "cutoff": event["cutoff"],
            "baseline_window_utc": event["baseline_window"],
            "baseline_totals_from_the_sealed_observation": event["baseline_totals"],
            "stories_returned_in_the_window": event["stories"],
            "selection_rule": ("most comments in the baseline window; ties broken by "
                               "earliest creation time; fixed before any title was read"),
            "selected": chosen,
            "selected_because": why,
        }
        if chosen is None:
            continue
        units[event_id] = {
            "event_id": event_id,
            "unit_id": event_id,
            "frozen_text": chosen["title"],
            "text_sha256": hashlib.sha256(chosen["title"].encode("utf-8")).hexdigest(),
            "source_record_id": "hn_story_" + chosen["objectID"],
            "source_version": "hn-algolia-baseline-window-snapshot-2026-10-14",
            "visible_at": chosen["created_at"],
            "cutoff": event["cutoff"],
            "cutoff_provenance": "derived_backtest_window_rule_not_historical_freeze",
            "story_url": chosen["url"],
            "num_comments_at_collection": chosen["num_comments"],
            "allowed_inputs": ["frozen_title_text", "source_attribution", "cutoff"],
            "forbidden_inputs": ["labels", "future_outcomes", "post_cutoff_sources"],
        }

    frozen = {
        "schema": "riskshield.data_evaluation.frozen_inputs.v1",
        "material_class": "real_frozen_material",
        "package_version": "late-three-events-title-cutoff-inputs-v1",
        "note": ("cutoff-only title inputs for three events that had a direction result but no "
                 "archived title in the frozen corpus; the observation windows are not part "
                 "of this package and were not read while choosing"),
        "events": listing,
        "units": units,
    }
    frozen_path = WORK / "frozen-inputs-late-three-v1.json"
    frozen_path.write_text(json.dumps(frozen, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8", newline="\n")

    print("selection, by the rule fixed before any title was read:")
    for event_id, entry in listing.items():
        picked = entry["selected"]
        print("  %-46s window holds %2d stories -> %s"
              % (event_id, len(entry["stories_returned_in_the_window"]),
                 ("%r (%d comments)" % (picked["title"][:58], picked["num_comments"]))
                 if picked else "NO UNIT"))
    print("frozen inputs written: %s" % frozen_path.name)

    if not os.environ.get(sentiment.API_KEY_ENV):
        for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if key.strip() and value.strip():
                    os.environ.setdefault(key.strip(), value.strip())
    try:
        configuration = sentiment.model_configuration()
    except sentiment.SentimentError as exc:
        print("configuration error: %s" % exc, file=sys.stderr)
        return 2
    api_key = configuration["api_key"]
    print("model: %s  endpoint: %s  units: %d  dry_run: %s"
          % (configuration["model"], configuration["endpoint"], len(units), args.dry_run))
    if args.dry_run:
        for event_id, unit in units.items():
            print("  would send: %s | %r" % (event_id, unit["frozen_text"][:80]))
        return 0

    import httpx

    run_id = "run_bailian_late_three_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    spent = 0.0
    predictions, evidence, failures = {}, {}, []

    def call(title: str) -> dict:
        payload = {
            "model": configuration["model"],
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": USER_TEMPLATE.format(title=title)}],
            "response_format": {"type": "json_object"},
            "max_tokens": 200,
        }
        started = time.perf_counter()
        response = httpx.post(configuration["endpoint"], json=payload,
                              headers={"Authorization": "Bearer " + api_key},
                              timeout=40, follow_redirects=False)
        if response.status_code != 200:
            raise sentiment.SentimentError("HTTP %d" % response.status_code)
        body = response.json()
        choice = body["choices"][0]
        if choice["finish_reason"] != "stop":
            raise sentiment.SentimentError("finish_reason != stop")
        result = sentiment.SentimentResult.model_validate_json(choice["message"]["content"])
        usage = body["usage"]
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if type(usage.get(name)) is not int or usage[name] < 0:
                raise sentiment.SentimentError("token usage missing")
        return {"label": result.label, "reason": result.reason, "model": body["model"],
                "token_usage": {k: usage[k] for k in
                                ("prompt_tokens", "completion_tokens", "total_tokens")},
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "ceiling_cny": sentiment._peak_cny(configuration["pricing"], usage)}

    for event_id, unit in units.items():
        if spent >= args.budget_cny:
            failures.append({"unit_id": event_id, "reason": "budget_ceiling_reached"})
            continue
        try:
            outcome = call(unit["frozen_text"])
        except Exception as exc:  # noqa: BLE001 - recorded, never retried into a new answer
            failures.append({"unit_id": event_id, "reason": "call_failed: %s" % exc})
            print("  %-46s FAILED %s" % (event_id, exc))
            continue
        spent += outcome["ceiling_cny"]
        evidence[event_id] = {
            "run_id": run_id, "unit_id": event_id, "execution_mode": EXECUTION_MODE,
            "method_version": METHOD_VERSION, "run_status": "complete",
            "requests_attempted": 1, "requests_completed": 1, "failed_decisions": 0,
            "model_returned": outcome["model"], "token_usage": outcome["token_usage"],
        }
        predictions[event_id] = {
            "unit_id": event_id, "event_id": event_id,
            "task_type": "sentiment_three_class", "label": outcome["label"],
            "execution_mode": EXECUTION_MODE, "method_version": METHOD_VERSION,
            "run_id": run_id, "predicted_at": now(), "cutoff": unit["cutoff"],
            "input_binding": {
                "input_package_ref": {"path": str(frozen_path.relative_to(ROOT)).replace("\\", "/"),
                                      "pointer": ""},
                "input_package_sha256": hashlib.sha256(
                    frozen_path.read_bytes()).hexdigest(),
                "cutoff": unit["cutoff"], "cutoff_before_label_read": True,
                "text_sha256": unit["text_sha256"],
                "source_version": unit["source_version"],
            },
            "production_evidence_ref": {
                "path": str((WORK / "production-evidence-late-three-v1.json").relative_to(ROOT)).replace("\\", "/"),
                "pointer": "/units/%s" % event_id},
            "freeze_record_ref": {
                "path": str((WORK / "freeze-record-late-three-v1.json").relative_to(ROOT)).replace("\\", "/"),
                "pointer": ""},
            "labels_accessed": False, "outcomes_accessed": False,
            "reason": outcome["reason"], "ceiling_cny": outcome["ceiling_cny"],
        }
        print("  %-46s -> %-9s (spent CNY %.6f)" % (event_id, outcome["label"], spent))

    document = {
        "schema": "riskshield.data_evaluation.prediction_import.v1",
        "task_type": "sentiment_three_class",
        "document_version": "bailian-late-three-v1",
        "material_class": "real_frozen_material",
        "formal_score_eligible": "decided_by_the_import_module_not_here",
        "run_id": run_id, "method_version": METHOD_VERSION,
        "endpoint": configuration["endpoint"], "requested_model": configuration["model"],
        "pricing": configuration["pricing"], "predictions": predictions,
    }
    pred_path = WORK / "predictions-late-three-v1.json"
    pred_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8", newline="\n")
    (WORK / "production-evidence-late-three-v1.json").write_text(json.dumps({
        "schema": "riskshield.data_evaluation.production_evidence.v1",
        "material_class": "real_frozen_material", "run_id": run_id,
        "execution_mode": EXECUTION_MODE, "method_version": METHOD_VERSION,
        "run_status": "complete" if not failures else "partial",
        "units": evidence, "failures": failures,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    (WORK / "freeze-record-late-three-v1.json").write_text(json.dumps({
        "schema": "riskshield.data_evaluation.freeze_record.v1",
        "material_class": "real_frozen_material", "frozen_at": now(),
        "frozen_by": "late sentiment run for three direction-only events",
        "frozen_inputs_sha256": hashlib.sha256(frozen_path.read_bytes()).hexdigest(),
        "labels_read_before_freeze": False, "outcomes_read_before_freeze": False,
        "late_collection_note": (
            "the observation windows of these three events were fetched before this input "
            "freeze, so this is a late freeze and is recorded as one. Only the baseline "
            "window was used to choose the title."),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    (WORK / "run-log-late-three-v1.json").write_text(json.dumps({
        "run_id": run_id, "requested_model": configuration["model"],
        "units_requested": len(units), "predicted": len(predictions),
        "failures": failures, "estimated_cny": round(spent, 8),
        "budget_cny": args.budget_cny,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    print(json.dumps({"predicted": len(predictions), "failed": len(failures),
                      "estimated_cny": round(spent, 8)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
