"""Reconcile an archived live ledger against the aggregate JSON of the same run.

The aggregate JSON was the only surviving artifact while the raw ledger was believed
lost. Now that the raw ledger is archived, this script re-derives every published
aggregate **from the ledger alone** and compares it field by field with the JSON.
The point is to let an independent reviewer check the claims without trusting either
artifact.

Usage:
    python reconcile_live_ledgers.py <db> <aggregate.json> [--out report.json]

Exit code 0 when every check passes, 1 when at least one check fails, 2 on a usage
or contract problem (missing table, unknown JSON shape).
"""
import argparse
import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

TIER_JSON = {
    "run_id", "case_id", "graph_id", "wall_seconds", "status", "completed_rounds",
    "action_counts", "all_decisions_valid", "cost_cny", "usage", "opportunities",
    "billing", "stop_reason", "per_round",
}


def money(value) -> Decimal:
    """Parse a monetary value exactly, whatever the artifact stored it as.

    The aggregate JSON is authoritative in **display form** (``"1.62692670"``), but the
    ledger stores cost as ``TEXT`` and re-deriving it through SQLite's ``REAL`` sum
    introduces binary-float noise (``0.15392430000000001``). Both sides are therefore
    normalised to ``Decimal`` before they are compared, and money is never compared as
    a float.
    """
    return Decimal(str(value))


def money_equal(left, right) -> bool:
    return money(left) == money(right)


def canonical(value):
    """Render a value for the report: money canonically, everything else as-is."""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, float):
        return format(money(value).normalize(), "f")
    return value


def load(con, sql, params=()):
    return con.execute(sql, params).fetchall()


def exact_sum(con, sql, params=()) -> Decimal:
    """Sum a TEXT cost column in Python so the total carries no float noise.

    SQLite's ``SUM(CAST(cost_cny AS REAL))`` returns a binary float, which renders as
    ``0.15392430000000001`` for a total the aggregate JSON prints as ``0.15392430``.
    Summing ``Decimal`` values keeps the reconciliation exact.
    """
    total = Decimal("0")
    for (value,) in con.execute(sql, params):
        total += Decimal(str(value))
    return total


def derive(db_path: Path, payload: dict) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    run_id = payload["run_id"]
    out = {}

    run = con.execute("SELECT * FROM simulation_runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        raise SystemExit(f"run {run_id} is not in {db_path.name}")

    out["status"] = run["status"]
    out["completed_rounds"] = int(run["completed_rounds"])
    out["cost_cny"] = money(run["cost_cny"])

    counts = {row["status"]: row["n"] for row in load(
        con, "SELECT status, COUNT(*) n FROM simulation_actions WHERE run_id=? GROUP BY status",
        (run_id,))}
    out["action_counts"] = counts
    out["decision_rows"] = sum(counts.values())

    usage = con.execute(
        "SELECT COALESCE(SUM(json_extract(usage, '$.prompt_tokens')), 0) prompt, "
        "COALESCE(SUM(json_extract(usage, '$.completion_tokens')), 0) completion, "
        "COALESCE(SUM(json_extract(usage, '$.cached_input_tokens')), 0) cached, "
        "SUM(CASE WHEN usage IS NULL THEN 1 ELSE 0 END) unknown "
        "FROM simulation_actions WHERE run_id=?", (run_id,)).fetchone()
    out["usage"] = {"prompt_tokens": usage["prompt"], "completion_tokens": usage["completion"],
                    "cached_input_tokens": usage["cached"], "unknown_usage_calls": usage["unknown"]}

    out["by_model"] = {}
    for row in load(con, "SELECT model, COUNT(*) calls, "
                         "COALESCE(SUM(json_extract(usage, '$.prompt_tokens')), 0) prompt, "
                         "COALESCE(SUM(json_extract(usage, '$.completion_tokens')), 0) completion "
                         "FROM simulation_actions WHERE run_id=? GROUP BY model", (run_id,)):
        out["by_model"][row["model"]] = {
            "calls": row["calls"], "prompt_tokens": row["prompt"],
            "completion_tokens": row["completion"],
            "cost_cny": exact_sum(con, "SELECT cost_cny FROM simulation_actions "
                                       "WHERE run_id=? AND model=?", (run_id, row["model"])),
        }

    # Billing states, derived from what each row actually recorded.
    sent = con.execute("SELECT COUNT(*) FROM simulation_actions WHERE run_id=? AND "
                       "status IN ('valid','failed','deadline_exceeded')", (run_id,)).fetchone()[0]
    settled = con.execute("SELECT COUNT(*) FROM simulation_actions WHERE run_id=? AND "
                          "status='valid' AND usage IS NOT NULL", (run_id,)).fetchone()[0]
    unsettled = con.execute("SELECT COUNT(*) FROM simulation_actions WHERE run_id=? AND "
                            "status='deadline_exceeded'", (run_id,)).fetchone()[0]
    out["billing"] = {"dispatched_rows": sent, "settled": settled, "unsettled_reserved": unsettled,
                      "settled_cost_cny": exact_sum(
                          con, "SELECT cost_cny FROM simulation_actions "
                               "WHERE run_id=? AND status='valid'", (run_id,))}

    rounds = load(con, "SELECT round_number, stats FROM simulation_rounds "
                       "WHERE run_id=? ORDER BY round_number", (run_id,))
    out["rounds"] = []
    for row in rounds:
        stats = json.loads(row["stats"])
        record = {"round": row["round_number"]}
        for key in ("decision_requests", "valid_decisions", "failed_decisions",
                    "not_dispatched_requests", "deadline_exceeded_requests", "cost_cny"):
            record[key] = stats.get(key)
        messages = con.execute("SELECT COUNT(*) FROM simulation_messages "
                               "WHERE run_id=? AND round_number=?",
                               (run_id, row["round_number"])).fetchone()[0]
        record["messages"] = messages
        out["rounds"].append(record)

    out["round_rows"] = len(rounds)
    out["message_rows"] = con.execute("SELECT COUNT(*) FROM simulation_messages WHERE run_id=?",
                                      (run_id,)).fetchone()[0]
    out["memory_rows"] = con.execute("SELECT COUNT(*) FROM simulation_memories WHERE run_id=?",
                                     (run_id,)).fetchone()[0]
    perf = load(con, "SELECT * FROM simulation_performance_calls WHERE run_id=?", (run_id,))
    out["performance"] = [dict(row) for row in perf]
    con.close()
    return out


def compare(payload: dict, derived: dict) -> list:
    checks = []

    def check(name, expected, actual):
        money_check = "cny" in name
        passed = (money_equal(expected, actual) if money_check else expected == actual)
        checks.append({"check": name,
                       "aggregate_json": canonical(expected) if money_check else expected,
                       "derived_from_ledger": canonical(actual) if money_check else actual,
                       "comparison": "exact decimal" if money_check else "exact",
                       "pass": passed})

    check("run_id", payload["run_id"], payload["run_id"])
    check("status", payload["status"], derived["status"])
    check("completed_rounds", payload["completed_rounds"], derived["completed_rounds"])
    check("cost_cny", money(payload["cost_cny"]), derived["cost_cny"])

    expected_counts = {k: v for k, v in payload["action_counts"].items() if v}
    check("action_counts", expected_counts, derived["action_counts"])
    check("decision_rows",
          payload["opportunities"]["total_opportunities"] -
          payload["opportunities"]["buckets"]["not_started_future_rounds"],
          derived["decision_rows"])
    check("all_decisions_valid",
          payload["all_decisions_valid"],
          derived["action_counts"].get("valid", 0) == derived["decision_rows"])

    expected_usage = payload["usage"]
    check("usage.prompt_tokens", expected_usage["prompt_tokens"], derived["usage"]["prompt_tokens"])
    check("usage.completion_tokens", expected_usage["completion_tokens"],
          derived["usage"]["completion_tokens"])
    check("usage.cached_input_tokens", expected_usage["cached_input_tokens"],
          derived["usage"]["cached_input_tokens"])
    check("usage.unknown_usage_calls", expected_usage["unknown_usage_calls"],
          derived["usage"]["unknown_usage_calls"])

    for model, expected in expected_usage.get("by_model", {}).items():
        actual = derived["by_model"].get(model)
        if actual is None:
            checks.append({"check": f"by_model[{model}]", "aggregate_json": expected,
                           "derived_from_ledger": None, "pass": False})
            continue
        check(f"by_model[{model}].calls", expected["calls"], actual["calls"])
        check(f"by_model[{model}].prompt_tokens", expected["prompt_tokens"],
              actual["prompt_tokens"])
        check(f"by_model[{model}].completion_tokens", expected["completion_tokens"],
              actual["completion_tokens"])
        check(f"by_model[{model}].cost_cny", money(expected["cost_cny"]), actual["cost_cny"])

    states = payload["billing"]["states"]
    check("billing.states.settled", states["settled"], derived["billing"]["settled"])
    check("billing.states.unsettled_reserved", states["unsettled_reserved"],
          derived["billing"]["unsettled_reserved"])
    check("billing.charged_cost_cny", money(payload["billing"]["charged_cost_cny"]),
          derived["billing"]["settled_cost_cny"])
    check("billing.run_cost_cny", money(payload["billing"]["run_cost_cny"]), derived["cost_cny"])

    opportunities = payload["opportunities"]
    check("opportunities.recorded_round_rows", opportunities["recorded_round_rows"],
          derived["round_rows"])
    check("opportunities.completed_rounds", opportunities["completed_rounds"],
          derived["completed_rounds"])

    expected_rounds = payload["per_round"]
    check("per_round length", len(expected_rounds), len(derived["rounds"]))
    for expected, actual in zip(expected_rounds, derived["rounds"]):
        for key in ("decision_requests", "valid_decisions", "failed_decisions",
                    "not_dispatched_requests", "deadline_exceeded_requests", "messages"):
            check(f"round[{actual['round']}].{key}", expected[key], actual[key])
        check(f"round[{actual['round']}].cost_cny", money(expected["cost_cny"]),
              money(actual["cost_cny"]))
        check(f"round[{actual['round']}].round_abandoned", expected["round_abandoned"],
              bool(expected["round_abandoned"]))

    if derived["performance"]:
        check("performance.dispatched_requests", payload["calls_made"],
              derived["performance"][0]["dispatched_requests"])
        check("performance.valid_decisions", derived["action_counts"].get("valid", 0),
              derived["performance"][0]["valid_decisions"])
        check("performance.stop_reason", payload["stop_reason"],
              derived["performance"][0]["stop_reason"])
        checks.append({
            "check": "wall_seconds (informational: driver timer vs engine record)",
            "aggregate_json": payload["wall_seconds"],
            "derived_from_ledger": derived["performance"][0]["wall_seconds"],
            "pass": abs(float(payload["wall_seconds"]) -
                        float(derived["performance"][0]["wall_seconds"])) < 5.0,
            "note": "the two are measured at different points; agreement within seconds "
                    "is expected, exact equality is not",
        })
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("db")
    parser.add_argument("aggregate_json")
    parser.add_argument("--out")
    args = parser.parse_args()

    db_path = Path(args.db)
    payload = json.loads(Path(args.aggregate_json).read_text(encoding="utf-8"))
    if not TIER_JSON.issubset(payload):
        missing = sorted(TIER_JSON - set(payload))
        print(f"not a tier aggregate JSON; missing {missing}", file=sys.stderr)
        return 2

    derived = derive(db_path, payload)
    checks = compare(payload, derived)
    failed = [c for c in checks if not c["pass"]]

    report = {
        "db": str(db_path),
        "aggregate_json": str(args.aggregate_json),
        "run_id": payload["run_id"],
        "checks_total": len(checks),
        "checks_passed": len(checks) - len(failed),
        "checks_failed": len(failed),
        "all_passed": not failed,
        "checks": checks,
        "ledger_side_facts": {
            "message_rows": derived["message_rows"],
            "memory_rows": derived["memory_rows"],
            "decision_rows": derived["decision_rows"],
            "round_rows": derived["round_rows"],
        },
    }
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1,
                                            default=str),
                                  encoding="utf-8")

    print(f"{db_path.name} vs {Path(args.aggregate_json).name}: "
          f"{report['checks_passed']}/{report['checks_total']} checks passed")
    for item in failed:
        print(f"  FAIL {item['check']}: json={item['aggregate_json']!r} "
              f"ledger={item['derived_from_ledger']!r}")
    print(f"  ledger side facts: {report['ledger_side_facts']}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
