"""Real-model message / memory ablation on the small tier.

Three arms, same seed, same case, same real models:
  * full      -- agent sees its runtime messages and its own memory;
  * no_message-- runtime messages masked at the decision component;
  * no_memory -- the agent's own memory masked at the decision component.

The mask is applied by hiding the field from the decision call *after* the engine froze
and hashed the observation, so the frozen-input record still covers what the round really
offered. Every arm records the effective decision input, the decision, its citations, and
per-round counts. Differences are then reported decision by decision.

No retry; a failed call stays failed.
"""
import argparse
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ADAPTER = PROJECT_ROOT / "runtime" / "bailian_backend_ref.py"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "runtime"))

import importlib.util  # noqa: E402

from riskshield.simulation import Day3Simulation  # noqa: E402
from riskshield.store import Store  # noqa: E402
from tests.fixtures.day3_synthetic_case import (  # noqa: E402
    CASE_ID, GRAPH_ID, prepare_synthetic_case,
)


class MaskingBackend:
    """Delegates to the real adapter, optionally hiding messages or memory."""

    def __init__(self, inner, *, mask_messages=False, mask_memories=False):
        self.inner = inner
        self.mask_messages = mask_messages
        self.mask_memories = mask_memories
        self.effective = []
        self.frozen = []

    @property
    def mode(self):
        return self.inner.mode

    def pricing(self, observation):
        return self.inner.pricing(observation)

    def describe(self):
        described = dict(self.inner.describe())
        described["ablation"] = {"mask_messages": self.mask_messages,
                                 "mask_memories": self.mask_memories}
        return described

    def decide_with_audit(self, observation, record_audit):
        frozen = json.loads(json.dumps(observation, ensure_ascii=False, default=str))
        self.frozen.append(frozen)
        effective = dict(observation)
        if self.mask_messages:
            effective["messages"] = []
        if self.mask_memories:
            effective["memories"] = []
        self.effective.append(json.loads(json.dumps(effective, ensure_ascii=False,
                                                    default=str)))
        return self.inner.decide_with_audit(effective, record_audit)

    def close(self):
        return self.inner.close()


def run_arm(arm, *, agents, rounds, concurrency, config, max_calls, budget, db_path,
            seed=20261011, refuse_existing=True):
    """One arm, with its ledger written to an explicit persistent path.

    The earlier version of this driver created its database with ``tempfile.mkdtemp``. That
    is exactly how the 15,000-row 500x30 ledger ended up in a scratch directory, so every arm
    now names its own database and refuses to overwrite one that already exists.
    """
    db_path = Path(db_path).expanduser().resolve()
    if not db_path.is_absolute():
        raise SystemExit(f"arm ledger path must be absolute, got {db_path}")
    if not db_path.parent.is_dir():
        raise SystemExit(f"arm ledger directory does not exist: {db_path.parent}")
    if db_path.exists():
        if refuse_existing:
            raise SystemExit(f"arm ledger {db_path} already exists; refusing to overwrite "
                             f"an archived ledger (move it or pass a new --outdir)")
        db_path.unlink()
    store = Store(db_path)
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=agents, rounds=rounds,
                               concurrency=concurrency, seed=seed,
                               budget_cny=Decimal(budget))
    inner = bailian.BailianBackend(config, enable_live=True, max_calls=max_calls)
    backend = MaskingBackend(inner, mask_messages=(arm == "no_message"),
                             mask_memories=(arm == "no_memory"))
    started = time.perf_counter()
    try:
        result = engine.advance(run_id, backend, collect_performance=True,
                                call_timeout_seconds=300)
    finally:
        inner.close()
    wall = time.perf_counter() - started

    trajectory = engine.trajectory(run_id)
    decisions = [{"round": row["round_number"], "agent_id": row["agent_id"],
                  "status": row["status"],
                  "decision": row["decision"],
                  "observation_refs": row["observation_refs"]}
                 for row in trajectory]

    def visible(observation, index):
        # An observation has no round field of its own; the call order is what identifies it.
        return {"call_index": index, "agent_id": observation["agent_id"],
                "n_messages": len(observation["messages"]),
                "n_memories": len(observation["memories"]),
                "state": observation["state"],
                "evidence_ids": [e["evidence_id"] for e in observation["evidence"]]}

    return {
        "arm": arm,
        "run_id": run_id,
        "ledger_db": str(db_path),
        "status": result["status"],
        "completed_rounds": result["completed_rounds"],
        "action_counts": result["action_counts"],
        "all_decisions_valid": result["all_decisions_valid"],
        "cost_cny": result["cost_cny"],
        "usage": result["usage"],
        "wall_seconds": round(wall, 3),
        "calls_made": inner.calls,
        "billing": result["billing"],
        "opportunities": result["opportunities"],
        "per_round": [{k: row.get(k) for k in (
            "round", "valid_decisions", "failed_decisions",
            "not_dispatched_requests", "messages", "elapsed_seconds", "cost_cny")}
            for row in result["rounds"]],
        "effective_inputs": [visible(o, index) for index, o in enumerate(backend.effective)],
        "decisions": decisions,
    }


def compare(full, other):
    """Decision-by-decision differences at the same (round, agent), plus citations."""
    key = lambda row: (row["round"], row["agent_id"])
    full_map = {key(row): row for row in full["decisions"]}
    other_map = {key(row): row for row in other["decisions"]}
    differing, citing_messages, citing_memories = [], 0, 0
    for position, (k, left) in enumerate(sorted(full_map.items())):
        right = other_map.get(k)
        left_decision = left["decision"] or {}
        right_decision = (right or {}).get("decision") or {}
        refs = left["observation_refs"]
        if left_decision.get("evidence_ids") and set(
                left_decision["evidence_ids"]) & set(refs.get("messages", [])):
            citing_messages += 1
        if left_decision.get("evidence_ids") and set(
                left_decision["evidence_ids"]) & set(refs.get("memories", [])):
            citing_memories += 1
        if left_decision != right_decision:
            differing.append({
                "round": k[0], "agent_id": k[1],
                "full_action": left_decision.get("action"),
                "masked_action": right_decision.get("action"),
                "full_cited": left_decision.get("evidence_ids"),
                "masked_cited": right_decision.get("evidence_ids"),
                "full_saw_messages": len(refs.get("messages", [])),
                "full_saw_memories": len(refs.get("memories", [])),
            })

    def action_counts(arm):
        counts = {}
        for row in arm["decisions"]:
            action = (row["decision"] or {}).get("action")
            counts[action] = counts.get(action, 0) + 1
        return counts

    return {
        "compared_arms": [full["arm"], other["arm"]],
        "decisions_compared": len(full_map),
        "differing_decisions": len(differing),
        "difference_rate": round(len(differing) / len(full_map), 4) if full_map else None,
        "differences": differing[:80],
        "full_action_distribution": action_counts(full),
        "masked_action_distribution": action_counts(other),
        "full_decisions_citing_runtime_message": citing_messages,
        "full_decisions_citing_own_memory": citing_memories,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agents", type=int, default=12)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--budget-cny", default="5.00")
    parser.add_argument("--out", required=True)
    parser.add_argument("--outdir", required=True,
                        help="persistent directory for the per-arm ledgers; each arm gets "
                             "its own database and an existing one is never overwritten")
    parser.add_argument("--env", default=os.environ.get("RISKSHIELD_ENV_FILE"),
                        help="credentials file (default $RISKSHIELD_ENV_FILE); no key or "
                             "value from it is ever printed")
    parser.add_argument("--adapter", default=str(DEFAULT_ADAPTER))
    parser.add_argument("--seed", type=int, default=20261011)
    args = parser.parse_args()

    if not args.env:
        raise SystemExit("no credentials file given; pass --env <path> or set "
                         "RISKSHIELD_ENV_FILE")
    env = Path(args.env).expanduser()
    if not env.is_file():
        raise SystemExit(f"credentials file not found: {env}")
    adapter_path = Path(args.adapter).expanduser()
    if not adapter_path.is_file():
        raise SystemExit(f"live adapter not found: {adapter_path}")
    spec = importlib.util.spec_from_file_location("bailian_ref", adapter_path)
    bailian = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bailian)

    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    if not outdir.is_dir():
        raise SystemExit(f"--outdir is not a directory: {outdir}")

    loaded = bailian.load_local_env(env)
    print(f"credentials keys loaded: {len(loaded)}", flush=True)
    config = bailian.BailianConfig.from_env(env, require_api_key=True)

    calls = args.agents * args.rounds
    arms = {}
    for arm in ("full", "no_message", "no_memory"):
        arms[arm] = run_arm(arm, agents=args.agents, rounds=args.rounds,
                            concurrency=args.concurrency, config=config,
                            max_calls=calls + 5, budget=args.budget_cny,
                            db_path=outdir / f"ablate-{arm}.db", seed=args.seed)
        a = arms[arm]
        print(f"{arm}: {a['status']} counts={a['action_counts']} "
              f"cost={a['cost_cny']} calls={a['calls_made']} wall={a['wall_seconds']}s "
              f"ledger={a['ledger_db']}", flush=True)

    payload = {
        "design": {"agents": args.agents, "rounds": args.rounds,
                   "concurrency": args.concurrency, "seed": args.seed,
                   "case_id": CASE_ID, "graph_id": GRAPH_ID,
                   "ledger_dir": str(outdir),
                   "models": {"netizen": "qwen-turbo", "customer": "qwen-turbo",
                              "media": "qwen-flash", "kol": "qwen-flash",
                              "regulator": "qwen-flash"},
                   "masking": ("the masked field is hidden from the decision call after "
                               "the engine froze the observation, so the frozen record "
                               "still covers what the round offered"),
                   "note": ("Real model calls. Differences are rule-free observations of "
                            "this model pair at this temperature, not a general claim.")},
        "arms": arms,
        "message_ablation": compare(arms["full"], arms["no_message"]),
        "memory_ablation": compare(arms["full"], arms["no_memory"]),
        "total_cost_cny": str(sum(Decimal(a["cost_cny"]) for a in arms.values())),
    }
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"message ablation: {payload['message_ablation']['differing_decisions']}"
          f"/{payload['message_ablation']['decisions_compared']} decisions differ", flush=True)
    print(f"memory ablation : {payload['memory_ablation']['differing_decisions']}"
          f"/{payload['memory_ablation']['decisions_compared']} decisions differ", flush=True)
    print(f"total cost {payload['total_cost_cny']} CNY; written {target}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
