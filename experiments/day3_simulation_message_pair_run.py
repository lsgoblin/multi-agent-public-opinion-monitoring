"""Bounded synthetic Bailian message comparison; dry-run unless explicitly enabled.

The historical decision selects the candidate only. Every T/C result in this
module must come from a new request, and this module is not a G3 gate.
"""

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from riskshield.day3 import (Day3Simulation, _persisted_error,
                             _safe_request_audit, _safe_schema_diagnostics)
from experiments.day3_bailian_synthetic_backend import (BailianBackend, BailianConfig,
                                     BailianConfigurationError)
from experiments.day3_simulation_message_pair_audit import (MessagePairAuditError,
                                           load_verified_frozen_pair)
from riskshield.schemas import AgentDecision


AGENT_ID = "agent_0019"
ROUND_NUMBER = 4
MESSAGE_ID = "msg_3_agent_0018_agent_0019"
SEQUENCE = (("T1", "treatment"), ("C1", "control"),
            ("C2", "control"), ("T2", "treatment"))
TASK_BUDGET_CNY = Decimal("0.01")
EXPECTED_PRICES = (Decimal("0.15"), Decimal("0.03"), Decimal("1.5"))
BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
LOCAL_ERROR_CODES = frozenset({
    "invalid_budget", "budget_outside_0_01_cap", "candidate_mismatch",
    "flash_price_or_bounds_mismatch", "route_mismatch",
    "reconstructed_payload_mismatch", "paired_inputs_differ_outside_target_message",
    "frozen_common_fields_mismatch", "four_call_reservation_exceeds_budget",
    "explicit_live_authorization_required", "same_day_official_price_check_required",
    "output_already_exists", "dispatch_audit_mismatch", "missing_dispatch_audit",
    "usage_unknown", "invalid_decision_citation", "actual_model_mismatch",
    "actual_cost_exceeds_reservation",
})


class MessagePairRunError(RuntimeError):
    """Fixed local gate error; never include request or response content."""

    def __init__(self, code):
        self.code = code if code in LOCAL_ERROR_CODES else "other"
        super().__init__(self.code)


def _beijing_today():
    return datetime.now(BEIJING_TZ).date().isoformat()


def _budget(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise MessagePairRunError("invalid_budget") from None
    if not amount.is_finite() or not Decimal("0") < amount <= TASK_BUDGET_CNY:
        raise MessagePairRunError("budget_outside_0_01_cap")
    return amount


def _checked_pair(db_path, env_path, budget):
    historical, treatment, control = load_verified_frozen_pair(
        db_path, message_id=MESSAGE_ID)
    if (historical["recipient_id"] != AGENT_ID or
            historical["recipient_round"] != ROUND_NUMBER or
            historical["message_id"] != MESSAGE_ID or
            historical["model"] != "qwen-flash" or
            treatment.get("data_mode") != "synthetic" or
            control.get("data_mode") != "synthetic"):
        raise MessagePairRunError("candidate_mismatch")
    configured = BailianConfig.from_env(
        env_path, require_api_key=True, required_models={"qwen-flash"})
    route = configured.routes["qwen-flash"]
    if (re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", route.price_version) is None or
            route.price_version == configured.api_key):
        raise MessagePairRunError("flash_price_or_bounds_mismatch")
    if (route.max_input_tokens != 8192 or route.max_output_tokens != 256 or
            (route.input_cny_per_million, route.cached_input_cny_per_million,
             route.output_cny_per_million) != EXPECTED_PRICES):
        raise MessagePairRunError("flash_price_or_bounds_mismatch")
    adapter = BailianBackend(configured)
    try:
        treatment_payload, _ = adapter.prepare_request(treatment)
        control_payload, _ = adapter.prepare_request(control)
        treatment_audit = adapter._request_audit(treatment_payload)
        control_audit = adapter._request_audit(control_payload)
        quote = adapter.pricing(treatment)
        if adapter.pricing(control) != quote or quote.requested_model != "qwen-flash":
            raise MessagePairRunError("route_mismatch")
    finally:
        adapter.close()
    if (treatment_audit["prepared_payload_canonical_json_sha256"] !=
            historical["reconstructed_treatment_payload_canonical_json_sha256"] or
            control_audit["prepared_payload_canonical_json_sha256"] !=
            historical["reconstructed_control_payload_canonical_json_sha256"]):
        raise MessagePairRunError("reconstructed_payload_mismatch")
    tf = json.loads(treatment_payload["messages"][1]["content"])
    cf = json.loads(control_payload["messages"][1]["content"])
    differences = {key for key in tf if tf[key] != cf[key]}
    if (set(tf) != set(cf) or
            differences != {"received_simulated_messages", "allowed_citation_ids"} or
            treatment_payload["messages"][0] != control_payload["messages"][0] or
            {key: value for key, value in treatment_payload.items() if key != "messages"} !=
            {key: value for key, value in control_payload.items() if key != "messages"} or
            sum(item["message_id"] == MESSAGE_ID for item in
                tf["received_simulated_messages"]) != 1 or
            any(item["message_id"] == MESSAGE_ID for item in
                cf["received_simulated_messages"]) or
            tf["allowed_citation_ids"].count(MESSAGE_ID) != 1 or
            MESSAGE_ID in cf["allowed_citation_ids"] or
            len(tf["received_simulated_messages"]) !=
            len(cf["received_simulated_messages"]) + 1 or
            len(tf["allowed_citation_ids"]) != len(cf["allowed_citation_ids"]) + 1):
        raise MessagePairRunError("paired_inputs_differ_outside_target_message")
    for name in ("prompt_sha256", "frozen_local_state_sha256",
                 "frozen_fictional_evidence_sha256",
                 "frozen_own_simulated_memory_sha256"):
        if treatment_audit[name] != control_audit[name]:
            raise MessagePairRunError("frozen_common_fields_mismatch")
    if quote.reservation_cny * len(SEQUENCE) > budget:
        raise MessagePairRunError("four_call_reservation_exceeds_budget")
    return historical, configured, treatment, control, treatment_audit, control_audit, quote


def _public_plan(historical, treatment_audit, control_audit, quote, budget):
    return {
        "candidate_run_id": historical["run_id"],
        "agent_id": AGENT_ID, "round": ROUND_NUMBER, "message_id": MESSAGE_ID,
        "sequence": [label for label, _ in SEQUENCE],
        "requested_model": "qwen-flash", "region": "cn-beijing",
        "data_mode": "synthetic", "price_version": quote.price_version,
        "price_verification": "manual_official_check_required_before_live",
        "task_budget_cny": str(budget),
        "reservation_per_call_cny": str(quote.reservation_cny),
        "four_call_reservation_cny": str(quote.reservation_cny * len(SEQUENCE)),
        "pair_field_differences": ["received_simulated_messages", "allowed_citation_ids"],
        "treatment_request_audit": treatment_audit,
        "control_request_audit": control_audit,
        "historical_decision_used_as_new_result": False,
        "g3_passed": False,
    }


def dry_run(*, db_path, env_path=".env", budget_cny=TASK_BUDGET_CNY):
    """Read-only configuration and full-ledger preflight; never constructs a live client."""
    budget = _budget(budget_cny)
    historical, _, _, _, treatment_audit, control_audit, quote = _checked_pair(
        db_path, env_path, budget)
    return {**_public_plan(historical, treatment_audit, control_audit, quote, budget),
            "status": "dry_run_ready", "calls_attempted": 0,
            "network_requests": 0, "model_calls": 0}


def _save(report, destination):
    """Persist only the already sanitized report after each attempted request."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(report, ensure_ascii=False, indent=2)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent,
                                         prefix=destination.name + ".", suffix=".tmp",
                                         delete=False) as handle:
            name = handle.name
            handle.write(data)
        os.replace(name, destination)
    finally:
        if name is not None and os.path.exists(name):
            os.unlink(name)


def _verdict(rows):
    by_label = {row["label"]: row for row in rows}
    t1, t2, c1, c2 = (by_label[name] for name in ("T1", "T2", "C1", "C2"))
    consistent = t1["action"] == t2["action"] and c1["action"] == c2["action"]
    changed = t1["action"] != c1["action"]
    cited = all(MESSAGE_ID in row["evidence_ids"] for row in (t1, t2))
    return {"within_arm_actions_consistent": consistent,
            "between_arm_actions_differ": changed,
            "both_treatments_cite_message": cited,
            "controlled_message_effect_observed": consistent and changed and cited,
            "g3_passed": False}


def run_live(*, db_path, output_path, env_path=".env", budget_cny=TASK_BUDGET_CNY,
             enable_live=False, authorization_label="", price_checked_on=""):
    """Execute at most four new requests, sequentially, with no retries."""
    if not enable_live or not authorization_label.strip():
        raise MessagePairRunError("explicit_live_authorization_required")
    if price_checked_on != _beijing_today():
        raise MessagePairRunError("same_day_official_price_check_required")
    destination = Path(output_path)
    if destination.exists():
        raise MessagePairRunError("output_already_exists")
    budget = _budget(budget_cny)
    historical, configured, treatment, control, treatment_audit, control_audit, quote = (
        _checked_pair(db_path, env_path, budget))
    report = {**_public_plan(historical, treatment_audit, control_audit, quote, budget),
              "status": "running", "authorization_label_sha256": hashlib.sha256(
                  authorization_label.encode("utf-8")).hexdigest(),
              "price_checked_on": price_checked_on,
              "calls_attempted": 0, "estimated_cost_cny": "0",
              "results": []}
    _save(report, destination)
    backend = BailianBackend(configured, enable_live=True, max_calls=len(SEQUENCE))
    total = Decimal("0")
    try:
        for label, arm in SEQUENCE:
            if total + quote.reservation_cny > budget:
                report["status"] = "partial"
                report["stop_reason"] = "next_reservation_exceeds_budget"
                break
            obs = treatment if arm == "treatment" else control
            expected_audit = treatment_audit if arm == "treatment" else control_audit
            captured = {"audit": {"stage": "before_dispatch"}}
            report["in_flight"] = {"label": label, "arm": arm,
                                   "reservation_cny": str(quote.reservation_cny)}
            _save(report, destination)

            def record_audit(value):
                safe = _safe_request_audit(value)
                if safe is None or safe != expected_audit:
                    raise MessagePairRunError("dispatch_audit_mismatch")
                captured["audit"] = safe

            before = backend.calls
            row = {"label": label, "arm": arm, "requested_model": "qwen-flash",
                   "status": "failed", "action": None, "evidence_ids": [],
                   "usage": None, "actual_model": None,
                   "request_audit": captured["audit"], "schema_diagnostics": None,
                   "estimated_cost_cny": "0", "error": None}
            measured_cost = None
            try:
                result = backend.decide_with_audit(obs, record_audit)
                if captured["audit"] != expected_audit:
                    raise MessagePairRunError("missing_dispatch_audit")
                cost, normalized = Day3Simulation._cost(quote, result.usage)
                if normalized is None:
                    raise MessagePairRunError("usage_unknown")
                measured_cost = cost
                row["usage"] = {key: normalized[key] for key in (
                    "prompt_tokens", "cached_input_tokens", "completion_tokens")}
                row["estimated_cost_cny"] = str(cost)
                decision = AgentDecision.model_validate(result.decision)
                allowed = json.loads(backend.prepare_request(obs)[0]["messages"][1]["content"])[
                    "allowed_citation_ids"]
                if (decision.agent_id != AGENT_ID or
                        not set(decision.evidence_ids) <= set(allowed)):
                    raise MessagePairRunError("invalid_decision_citation")
                actual_model = result.model
                if (type(actual_model) is not str or len(actual_model) > 80 or
                        re.fullmatch(r"qwen-flash(?:[-_][A-Za-z0-9.-]+)?",
                                     actual_model) is None):
                    raise MessagePairRunError("actual_model_mismatch")
                if cost > quote.reservation_cny:
                    raise MessagePairRunError("actual_cost_exceeds_reservation")
                row.update({"status": "valid", "action": decision.action,
                            "evidence_ids": decision.evidence_ids,
                            "actual_model": actual_model,
                            "estimated_cost_cny": str(cost)})
            except Exception as exc:
                row["error"] = ("CausalRunError:" + exc.code
                                if isinstance(exc, MessagePairRunError)
                                else _persisted_error(exc))
                row["schema_diagnostics"] = _safe_schema_diagnostics(exc)
                if measured_cost is None:
                    row["estimated_cost_cny"] = str(
                        quote.reservation_cny if backend.calls > before else Decimal("0"))
            row["request_audit"] = captured["audit"]
            total += Decimal(row["estimated_cost_cny"])
            report["results"].append(row)
            report.pop("in_flight", None)
            report["calls_attempted"] = backend.calls
            report["estimated_cost_cny"] = str(total)
            if row["status"] != "valid":
                report["status"] = "partial"
                report["stop_reason"] = row["error"]
                _save(report, destination)
                break
            _save(report, destination)
        else:
            report["status"] = "complete"
            report["verdict"] = _verdict(report["results"])
    finally:
        backend.close()
        _save(report, destination)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--budget-cny", default="0.01")
    parser.add_argument("--output")
    parser.add_argument("--enable-live", action="store_true")
    parser.add_argument("--authorization-label", default="")
    parser.add_argument("--price-checked-on", default="")
    args = parser.parse_args(argv)
    if args.enable_live and not args.output:
        parser.error("live run requires --output")
    try:
        if args.enable_live:
            report = run_live(
                db_path=args.db, output_path=args.output, env_path=args.env_file,
                budget_cny=args.budget_cny, enable_live=True,
                authorization_label=args.authorization_label,
                price_checked_on=args.price_checked_on)
        else:
            report = dry_run(db_path=args.db, env_path=args.env_file,
                             budget_cny=args.budget_cny)
    except MessagePairRunError as exc:
        print(json.dumps({"status": "preflight_failed", "reason": str(exc),
                          "network_requests": 0, "model_calls": 0}, ensure_ascii=False))
        return 2
    except (MessagePairAuditError, BailianConfigurationError):
        print(json.dumps({"status": "preflight_failed", "reason": "candidate_or_config_invalid",
                          "network_requests": 0, "model_calls": 0}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] in {"dry_run_ready", "complete"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
