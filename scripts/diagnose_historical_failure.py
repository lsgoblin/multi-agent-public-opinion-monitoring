"""Diagnostic run: reproduce the failure and keep the raw exchanges.

Smallest configuration that can reproduce it -- one round, two agents -- so the
diagnosis costs a fraction of the failed run.
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(r"D:\test\多智能体仿真舆情监测与投诉预警")
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(ROOT / "src"))

for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

import bailian_backend_ref as ref  # noqa: E402
import bailian_diagnostic_adapter as diag  # noqa: E402
from riskshield.simulation import Day3Simulation  # noqa: E402
from riskshield.store import Store  # noqa: E402

CASE = "unh_change_cyberattack_20240222"
GRAPH = "936c73951d2ab46f8e0ee6bb"
DB = ROOT / "artifacts/live-run-20261014/ledger-live.db"
CAPTURE = ROOT / "artifacts/live-run-20261014/diagnostic-exchanges"

AGENTS = 2
ROUNDS = 1

config = ref.BailianConfig.from_env()
store = Store(DB)
engine = Day3Simulation(store)
run_id = engine.create_run(CASE, GRAPH, agent_count=AGENTS, rounds=ROUNDS,
                           concurrency=2, budget_cny=Decimal("0.20"))
print("diagnostic run: %s  agents=%d rounds=%d" % (run_id, AGENTS, ROUNDS))

backend = diag.CapturingHistoricalBackend(
    config, enable_live=True, max_calls=AGENTS * ROUNDS, capture_dir=CAPTURE)
try:
    summary = engine.advance(run_id, backend, call_timeout_seconds=90.0)
finally:
    captured = backend.captured_exchanges
    backend.close()

print()
print("status           : %s" % summary.get("status"))
print("action counts    : %s" % summary.get("action_counts"))
print("cost             : CNY %s" % summary.get("cost_cny"))
print("calls            : %d" % backend.calls)
print("captured exchanges: %d" % captured)

files = sorted(CAPTURE.glob("exchange-*.json"))
print("capture files    : %d in %s" % (len(files), CAPTURE))
for path in files[:4]:
    record = json.loads(path.read_text(encoding="utf-8"))
    print()
    print("--- %s ---" % path.name)
    print("  note        : %s" % record.get("note"))
    print("  http_status : %s" % record.get("http_status"))
    body = record.get("response_json") or {}
    choice = (body.get("choices") or [{}])[0]
    content = ((choice.get("message") or {}).get("content"))
    print("  finish      : %s" % choice.get("finish_reason"))
    print("  model       : %s" % body.get("model"))
    print("  usage       : %s" % json.dumps(body.get("usage"), ensure_ascii=False))
    print("  content     : %s" % (str(content)[:300] if content is not None else None))
    print("  top keys    : %s" % sorted(body.keys()))
