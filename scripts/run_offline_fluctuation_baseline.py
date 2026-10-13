"""Offline fluctuation baseline and stratified message/memory ablation.

Why this exists
---------------
The published ablation numbers (message 51/60, memory 59/60) compared **complete decision
objects**, and the comparison had no noise floor: both arms ran once, so the only number
available was the observed difference, with nothing to compare it against. The same rounds
also include round 1, where no agent has a message or a memory yet, so a difference there
cannot be caused by the masked input at all.

What this script measures, all offline and at zero cost
-------------------------------------------------------
1. **Fluctuation baseline**: the identical configuration run ``--repeats`` times. Because
   the offline substitute is deterministic (no model, so no sampling temperature), any
   difference between repeats is a *floor* introduced by run-to-run state, not by the
   mask. A non-zero baseline would mean the comparison needs that much slack; a zero
   baseline means the deterministic pipeline reproduces itself exactly.
2. **Masked comparison** of the same shape, reported on two levels separately:
   * ``action`` — only the chosen action (the coarse, decision-level outcome);
   * ``full`` — the complete decision object, which is what the earlier numbers used.
3. **First-round exclusion**: the same comparison restricted to rounds >= 2, where an
   agent can actually have received a message or accumulated a memory.

Provenance is unchanged: the mask is applied by hiding the field from the decision call
*after* the engine froze and hashed the observation, so the frozen-input record still
covers what the round really offered.

Cost: zero. ``model_calls=0``, ``network_requests=0``, no API key is read.
"""
import argparse
import json
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from riskshield.simulation import Day3Simulation, DecisionResult  # noqa: E402
from riskshield.store import Store  # noqa: E402
from riskshield.tasks import OfflineDynamicSubstitute  # noqa: E402
from tests.fixtures.day3_synthetic_case import (  # noqa: E402
    CASE_ID, GRAPH_ID, prepare_synthetic_case,
)


class MaskingSubstitute:
    """The frozen offline substitute, with one input field optionally hidden.

    The engine calls ``decide``; the mask is applied here, per call, to a copy of the
    observation the engine already froze and hashed. Masking ``messages`` or ``memories``
    therefore changes only what the decision component can see.
    """

    def __init__(self, *, mask_messages=False, mask_memories=False):
        self.inner = OfflineDynamicSubstitute()
        self.mask_messages = mask_messages
        self.mask_memories = mask_memories
        self.seen_messages = 0
        self.seen_memories = 0

    @property
    def mode(self):
        return self.inner.mode

    @property
    def reservation_cny(self):
        return self.inner.reservation_cny

    @property
    def input_cny_per_million(self):
        return self.inner.input_cny_per_million

    @property
    def cached_input_cny_per_million(self):
        return self.inner.cached_input_cny_per_million

    @property
    def output_cny_per_million(self):
        return self.inner.output_cny_per_million

    @property
    def price_version(self):
        return self.inner.price_version

    def describe(self):
        described = dict(self.inner.describe())
        described["ablation"] = {"mask_messages": self.mask_messages,
                                 "mask_memories": self.mask_memories}
        return described

    def decide(self, observation):
        # Record what the round really offered, before masking.
        self.seen_messages += len(observation.get("messages") or [])
        self.seen_memories += len(observation.get("memories") or [])
        effective = dict(observation)
        if self.mask_messages:
            effective["messages"] = []
        if self.mask_memories:
            effective["memories"] = []
        return self.inner.decide(effective)


def run_arm(arm, *, agents, rounds, concurrency, seed, db_path):
    """One offline arm; returns its per-decision record and its ledger summary."""
    if db_path.exists():
        db_path.unlink()
    store = Store(db_path)
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=agents, rounds=rounds,
                               concurrency=concurrency, seed=seed,
                               budget_cny=Decimal("30.00"))
    backend = MaskingSubstitute(mask_messages=(arm == "no_message"),
                               mask_memories=(arm == "no_memory"))
    started = time.perf_counter()
    result = engine.advance(run_id, backend, collect_performance=True,
                            call_timeout_seconds=120)
    wall = time.perf_counter() - started
    trajectory = engine.trajectory(run_id)
    return {
        "arm": arm,
        "run_id": run_id,
        "status": result["status"],
        "completed_rounds": result["completed_rounds"],
        "action_counts": result["action_counts"],
        "all_decisions_valid": result["all_decisions_valid"],
        "cost_cny": result["cost_cny"],
        "wall_seconds": round(wall, 3),
        "model_calls": 0,
        "offered_messages_to_decision": backend.seen_messages,
        "offered_memories_to_decision": backend.seen_memories,
        "decisions": [{
            "round": row["round_number"],
            "agent_id": row["agent_id"],
            "status": row["status"],
            "action": (row["decision"] or {}).get("action"),
            "decision": row["decision"],
            "saw_messages": len((row["observation_refs"] or {}).get("messages") or []),
            "saw_memories": len((row["observation_refs"] or {}).get("memories") or []),
        } for row in trajectory],
    }


def compare(left, right, *, level, exclude_first_round):
    """Count differing decisions between two arms at a chosen granularity."""
    def key(row):
        return (row["round"], row["agent_id"])

    def value(row):
        if level == "action":
            return row["action"]
        return row["decision"]

    left_map = {key(row): row for row in left["decisions"]}
    right_map = {key(row): row for row in right["decisions"]}
    keys = sorted(set(left_map) & set(right_map))
    if exclude_first_round:
        keys = [k for k in keys if k[0] >= 2]
    differing = []
    for k in keys:
        if value(left_map[k]) != value(right_map.get(k, {})):
            differing.append({
                "round": k[0], "agent_id": k[1],
                "left_action": left_map[k]["action"],
                "right_action": right_map.get(k, {}).get("action"),
                "left_saw_messages": left_map[k]["saw_messages"],
                "left_saw_memories": left_map[k]["saw_memories"],
            })
    return {
        "compared_arms": [left["arm"], right["arm"]],
        "granularity": level,
        "excluded_first_round": exclude_first_round,
        "decisions_compared": len(keys),
        "differing_decisions": len(differing),
        "difference_rate": (round(len(differing) / len(keys), 4) if keys else None),
        "differences": differing,
    }


def action_distribution(arm, *, exclude_first_round):
    counts = {}
    for row in arm["decisions"]:
        if exclude_first_round and row["round"] < 2:
            continue
        counts[row["action"]] = counts.get(row["action"], 0) + 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agents", type=int, default=12)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20261011)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--out", required=True)
    parser.add_argument("--workdir", required=True,
                        help="where the per-run ledger databases are written")
    args = parser.parse_args()
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    # 1. Fluctuation baseline: identical configuration, repeated.
    repeats = []
    for index in range(args.repeats):
        run = run_arm("full", agents=args.agents, rounds=args.rounds,
                      concurrency=args.concurrency, seed=args.seed,
                      db_path=workdir / f"baseline-{index + 1}.db")
        repeats.append(run)
        print(f"baseline repeat {index + 1}: {run['status']} "
              f"counts={run['action_counts']} wall={run['wall_seconds']}s", flush=True)
    baseline = repeat_comparison(repeats)

    # 2. Masked arms, same configuration and seed.
    arms = {"full": repeats[0]}
    for arm in ("no_message", "no_memory"):
        arms[arm] = run_arm(arm, agents=args.agents, rounds=args.rounds,
                            concurrency=args.concurrency, seed=args.seed,
                            db_path=workdir / f"ablate-{arm}.db")
        print(f"{arm}: {arms[arm]['status']} counts={arms[arm]['action_counts']} "
              f"wall={arms[arm]['wall_seconds']}s", flush=True)

    payload = {
        "design": {
            "agents": args.agents, "rounds": args.rounds,
            "concurrency": args.concurrency, "seed": args.seed,
            "repeats": args.repeats,
            "case_id": CASE_ID, "graph_id": GRAPH_ID,
            "execution_mode": "offline_dynamic_substitute",
            "model_calls": 0, "network_requests": 0,
            "masking": ("the masked field is hidden from the decision call after the "
                        "engine froze and hashed the observation, so the frozen record "
                        "still covers what the round really offered"),
            "levels": {
                "action": "only the chosen action is compared",
                "full": "the complete decision object is compared (the basis of the "
                        "earlier 51/60 and 59/60 numbers)",
            },
            "qualification": "engineering_baseline_only_not_formal_forecast",
        },
        "fluctuation_baseline": baseline,
        "arms": {arm: {k: v for k, v in run.items() if k != "decisions"}
                 for arm, run in arms.items()},
        "message_ablation": {
            "action_level": compare(arms["full"], arms["no_message"], level="action",
                                    exclude_first_round=False),
            "action_level_rounds_ge_2": compare(arms["full"], arms["no_message"],
                                                level="action",
                                                exclude_first_round=True),
            "full_decision_level": compare(arms["full"], arms["no_message"], level="full",
                                           exclude_first_round=False),
            "full_decision_level_rounds_ge_2": compare(arms["full"], arms["no_message"],
                                                       level="full",
                                                       exclude_first_round=True),
        },
        "memory_ablation": {
            "action_level": compare(arms["full"], arms["no_memory"], level="action",
                                    exclude_first_round=False),
            "action_level_rounds_ge_2": compare(arms["full"], arms["no_memory"],
                                                level="action",
                                                exclude_first_round=True),
            "full_decision_level": compare(arms["full"], arms["no_memory"], level="full",
                                           exclude_first_round=False),
            "full_decision_level_rounds_ge_2": compare(arms["full"], arms["no_memory"],
                                                       level="full",
                                                       exclude_first_round=True),
        },
        "action_distributions": {
            arm: {
                "all_rounds": action_distribution(run, exclude_first_round=False),
                "rounds_ge_2": action_distribution(run, exclude_first_round=True),
            } for arm, run in arms.items()
        },
    }
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    report(payload)
    print(f"\nwritten {target}")
    return 0


def repeat_comparison(repeats):
    """Whether identical repeats differed at all, at both levels."""
    if len(repeats) < 2:
        return {"repeats": len(repeats), "note": "at least two repeats are needed"}
    first = repeats[0]
    results = []
    for other in repeats[1:]:
        results.append({
            "pair": [first["run_id"], other["run_id"]],
            "action_level_differences":
                compare(first, other, level="action", exclude_first_round=False)
                ["differing_decisions"],
            "full_decision_level_differences":
                compare(first, other, level="full", exclude_first_round=False)
                ["differing_decisions"],
            "decisions_compared": len(first["decisions"]),
        })
    total = len(first["decisions"])
    worst = max((item["full_decision_level_differences"] for item in results), default=0)
    return {
        "repeats": len(repeats),
        "decisions_per_repeat": total,
        "pairs": results,
        "baseline_full_decision_differences": worst,
        "baseline_action_differences": max(
            (item["action_level_differences"] for item in results), default=0),
        "note": ("the offline substitute takes no sampling temperature, so an identical "
                 "configuration is expected to reproduce itself exactly; a non-zero value "
                 "here would be the floor any masked comparison has to exceed"),
    }


def report(payload):
    baseline = payload["fluctuation_baseline"]
    print("\n=== fluctuation baseline (identical configuration, repeated) ===")
    print(f"  repeats={baseline['repeats']} decisions/repeat="
          f"{baseline.get('decisions_per_repeat')}")
    print(f"  worst full-decision differences between repeats: "
          f"{baseline.get('baseline_full_decision_differences')}")
    print(f"  worst action differences between repeats: "
          f"{baseline.get('baseline_action_differences')}")
    for label in ("message_ablation", "memory_ablation"):
        block = payload[label]
        print(f"\n=== {label} ===")
        for level in ("action_level", "action_level_rounds_ge_2",
                      "full_decision_level", "full_decision_level_rounds_ge_2"):
            item = block[level]
            print(f"  {level:34s} {item['differing_decisions']}/"
                  f"{item['decisions_compared']}")
    print("\n=== action distributions ===")
    for arm, block in payload["action_distributions"].items():
        print(f"  {arm:12s} all={block['all_rounds']} rounds>=2={block['rounds_ge_2']}")


if __name__ == "__main__":
    raise SystemExit(main())
