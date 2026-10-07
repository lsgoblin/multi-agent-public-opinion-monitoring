import json
import os
from decimal import Decimal

import pytest

import experiments.day3_bailian_synthetic_run as runner
from riskshield.day3 import DecisionResult, SimulationError
from experiments.day3_bailian_synthetic_backend import (BEIJING_BASE_URL, BailianBackend,
                                     BailianConfig, BailianDecisionSchemaError,
                                     BailianRoute, ROLE_MODELS)
from riskshield.schemas import AgentDecision


@pytest.fixture
def clean_bailian_environment(monkeypatch):
    names = ["DASHSCOPE_API_KEY", "BAILIAN_API_KEY", "RISKSHIELD_BAILIAN_BASE_URL"]
    names += [f"RISKSHIELD_DAY3_MODEL_{role.upper()}" for role in ROLE_MODELS]
    names += [f"RISKSHIELD_BAILIAN_{prefix}_{suffix}"
              for prefix in ("TURBO", "FLASH")
              for suffix in ("PRICE_VERSION", "INPUT_CNY_PER_MILLION",
                             "CACHED_INPUT_CNY_PER_MILLION", "OUTPUT_CNY_PER_MILLION",
                             "MAX_INPUT_TOKENS", "MAX_OUTPUT_TOKENS")]
    for name in names:
        monkeypatch.delenv(name, raising=False)


def write_preflight_env(path, *, key=True, flash=True, endpoint=BEIJING_BASE_URL,
                        flash_input="0.15"):
    lines = [f"RISKSHIELD_BAILIAN_BASE_URL={endpoint}"]
    if key:
        lines.append("DASHSCOPE_API_KEY=secret-never-print-this-value")
    for prefix, input_rate, cached_rate, output_rate in (
        ("TURBO", "0.3", "0.06", "0.6"),
        ("FLASH", flash_input, "0.03", "1.5"),
    ):
        if prefix == "FLASH" and not flash:
            continue
        root = f"RISKSHIELD_BAILIAN_{prefix}_"
        lines += [
            f"{root}PRICE_VERSION=local-label-only",
            f"{root}INPUT_CNY_PER_MILLION={input_rate}",
            f"{root}CACHED_INPUT_CNY_PER_MILLION={cached_rate}",
            f"{root}OUTPUT_CNY_PER_MILLION={output_rate}",
            f"{root}MAX_INPUT_TOKENS=8192",
            f"{root}MAX_OUTPUT_TOKENS={160 if prefix == 'TURBO' else 256}",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def config(api_key="test-only-key"):
    def route(model):
        return BailianRoute(
            model=model, price_version=f"{model}-test-price-v1",
            input_cny_per_million=Decimal("0.3"),
            cached_input_cny_per_million=Decimal("0.06"),
            output_cny_per_million=Decimal("0.6"),
            max_input_tokens=8192,
            max_output_tokens=160 if model == "qwen-turbo" else 256,
        )

    return BailianConfig(
        api_key=api_key, base_url=BEIJING_BASE_URL,
        routes={"qwen-turbo": route("qwen-turbo"),
                "qwen-flash": route("qwen-flash")},
    )


class OfflineRunnerBackend:
    mode = "real_model"
    instances = []

    def __init__(self, configured, *, enable_live, max_calls):
        assert enable_live is True
        self.config = configured
        self.max_calls = max_calls
        self.calls = 0
        self.closed = False
        self.instances.append(self)

    def close(self):
        self.closed = True

    def describe(self):
        return {"type": "offline-runner-test", "role_models": ROLE_MODELS}

    def pricing(self, obs):
        return self.config.routes[ROLE_MODELS[obs["persona"]["role"]]].pricing()

    def decide(self, obs):
        self.calls += 1
        if obs["agent_id"] == "agent_0000":
            action = "share"
            refs = [obs["evidence"][0]["evidence_id"]]
        elif obs["messages"]:
            action = "comment"
            refs = [obs["messages"][0]["message_id"]]
        else:
            action = "observe"
            refs = [obs["evidence"][0]["evidence_id"]]
        return DecisionResult(
            AgentDecision(agent_id=obs["agent_id"], action=action,
                          evidence_ids=refs, reason="Synthetic runner audit test."),
            usage={"prompt_tokens": 20, "cached_input_tokens": 4,
                   "completion_tokens": 5},
            model=ROLE_MODELS[obs["persona"]["role"]] + "-test",
        )


def test_runner_budget_cap_is_30_cny():
    assert runner._budget("30.00") == Decimal("30.00")
    with pytest.raises(SimulationError, match="¥30.00"):
        runner._budget("30.01")
    with pytest.raises(SimulationError, match="¥30.00"):
        runner._budget("NaN")


def test_preflight_full_scale_fixed_routes_and_reservation(tmp_path,
                                                           clean_bailian_environment):
    env = write_preflight_env(tmp_path / ".env")
    before = dict(os.environ)
    report = runner.preflight_bailian(
        env_path=env, agent_count=500, rounds=30, max_calls=15000,
        budget_cny="30.00")
    assert dict(os.environ) == before
    assert report["preflight_status"] == "configuration_passed"
    assert report["agents_by_model"] == {"qwen-turbo": 200, "qwen-flash": 300}
    assert report["opportunities_by_model"] == {"qwen-turbo": 6000,
                                                 "qwen-flash": 9000}
    assert report["decision_opportunities"] == 15000
    assert report["reservation_cny_by_model"] == {
        "qwen-turbo": "15.3216000", "qwen-flash": "14.5152000"}
    assert Decimal(report["total_conservative_reservation_cny"]) == Decimal("29.8368")
    assert Decimal(report["remaining_budget_cny"]) == Decimal("0.1632")
    assert report["g3_passed"] is False
    assert report["live_authorized"] is False
    assert "manual" in report["price_verification"]
    assert "secret-never-print-this-value" not in json.dumps(report)


def test_preflight_default_budget_fails_with_visible_cost_breakdown(
        tmp_path, clean_bailian_environment):
    env = write_preflight_env(tmp_path / ".env")
    report = runner.preflight_bailian(env_path=env, agent_count=500, rounds=30)
    assert report["preflight_status"] == "failed"
    assert report["budget_status"] == "reservation_exceeds_budget"
    assert Decimal(report["task_budget_cny"]) == Decimal("5.00")
    assert Decimal(report["total_conservative_reservation_cny"]) == Decimal("29.8368")
    with pytest.raises(SimulationError, match="¥30.00"):
        runner.preflight_bailian(env_path=env, agent_count=500, rounds=30,
                                 budget_cny="30.01")


def test_preflight_100_by_10_stays_within_default_budget(
        tmp_path, clean_bailian_environment):
    env = write_preflight_env(tmp_path / ".env")
    report = runner.preflight_bailian(env_path=env, agent_count=100, rounds=10,
                                      max_calls=1000)
    assert report["preflight_status"] == "configuration_passed"
    assert report["agents_by_model"] == {"qwen-turbo": 40, "qwen-flash": 60}
    assert Decimal(report["total_conservative_reservation_cny"]) == Decimal("1.98912")
    assert Decimal(report["task_budget_cny"]) == Decimal("5.00")


def test_preflight_turbo_only_probe_does_not_require_flash(
        tmp_path, clean_bailian_environment):
    env = write_preflight_env(tmp_path / ".env", flash=False)
    report = runner.preflight_bailian(env_path=env, agent_count=1, rounds=1)
    assert report["preflight_status"] == "configuration_passed"
    assert report["agents_by_model"] == {"qwen-turbo": 1, "qwen-flash": 0}
    with pytest.raises(runner.BailianConfigurationError, match="FLASH_INPUT"):
        runner.preflight_bailian(env_path=env, agent_count=500, rounds=30,
                                 budget_cny="30")


def test_preflight_requires_dedicated_key_and_beijing_endpoint(
        tmp_path, clean_bailian_environment, monkeypatch):
    env = write_preflight_env(tmp_path / ".env", key=False)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-gateway-secret")
    with pytest.raises(runner.BailianConfigurationError, match="DASHSCOPE_API_KEY"):
        runner.preflight_bailian(env_path=env, agent_count=1, rounds=1)
    env = write_preflight_env(tmp_path / ".env", endpoint="https://example.org/v1")
    with pytest.raises(runner.BailianConfigurationError, match="Beijing HTTPS endpoint"):
        runner.preflight_bailian(env_path=env, agent_count=1, rounds=1)


def test_preflight_cli_never_uses_network_store_or_output(
        tmp_path, clean_bailian_environment, monkeypatch, capsys):
    env = write_preflight_env(tmp_path / ".env")
    db, output = tmp_path / "forbidden.db", tmp_path / "forbidden.json"
    monkeypatch.setattr(runner, "Store", lambda *args, **kwargs:
                        pytest.fail("preflight created a Store"))
    monkeypatch.setattr("httpx.Client", lambda *args, **kwargs:
                        pytest.fail("preflight created an HTTP client"))
    exit_code = runner.main([
        "--preflight", "--env-file", str(env), "--db", str(db),
        "--output", str(output), "--agent-count", "500", "--rounds", "30",
        "--budget-cny", "30", "--max-calls", "15000",
    ])
    printed = capsys.readouterr().out
    assert exit_code == 0
    assert json.loads(printed)["preflight_status"] == "configuration_passed"
    assert "secret-never-print-this-value" not in printed
    assert not db.exists() and not output.exists()
    with pytest.raises(SystemExit):
        runner.main(["--preflight", "--enable-live", "--agent-count", "1",
                     "--rounds", "1"])


def test_runner_requires_explicit_live_flag_and_exact_call_limit(tmp_path):
    common = dict(
        db_path=tmp_path / "run.db", output_path=tmp_path / "run.json",
        env_path=tmp_path / ".env", agent_count=2, rounds=2, concurrency=2,
        max_calls=4, budget_cny="1", role_offset=0, purpose="offline test",
        authorization_label="test-only",
    )
    with pytest.raises(SimulationError, match="enable_live"):
        runner.run_synthetic_bailian(**common)
    with pytest.raises(SimulationError, match=r"agent_count \* rounds"):
        runner.run_synthetic_bailian(**(common | {"enable_live": True, "max_calls": 3}))
    assert not (tmp_path / "run.json").exists()


def test_runner_writes_sanitized_trajectory_and_separate_success_counts(tmp_path,
                                                                        monkeypatch):
    configured = config(api_key="runner-test-secret")
    monkeypatch.setattr(
        runner.BailianConfig, "from_env",
        classmethod(lambda cls, path, require_api_key, required_models: configured),
    )
    monkeypatch.setattr(runner, "BailianBackend", OfflineRunnerBackend)
    output = tmp_path / "result.json"
    report = runner.run_synthetic_bailian(
        db_path=tmp_path / "run.db", output_path=output, env_path=tmp_path / ".env",
        agent_count=2, rounds=2, concurrency=2, max_calls=4,
        budget_cny=Decimal("1"), role_offset=0,
        purpose="synthetic causal preparation",
        authorization_label="test-only-no-network", enable_live=True,
    )
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert report["run_status"] == "complete"
    assert report["completed_rounds"] == 2
    assert report["valid_decisions"] == 4
    assert report["failed_decisions"] == 0
    assert report["completed_without_failures"] is True
    assert report["calls_attempted"] == 4
    assert OfflineRunnerBackend.instances[-1].closed is True
    assert len(report["trajectory"]) == 4
    assert report["messages"]
    message_id = report["messages"][0]["message_id"]
    receiver_round_two = next(row for row in report["trajectory"]
                              if row["round"] == 2 and row["agent_id"] == "agent_0001")
    assert message_id in receiver_round_two["observation_refs"]["messages"]
    assert "content" not in report["messages"][0]
    assert all(row["request_audit"] is None and row["schema_diagnostics"] is None
               for row in saved["trajectory"])
    assert "runner-test-secret" not in json.dumps(saved)


def test_runner_report_keeps_safe_audit_and_schema_details_without_secrets(
        tmp_path, monkeypatch):
    secret = "provider-body-and-api-key-must-stay-out-of-report"

    class AuditedOfflineBackend(OfflineRunnerBackend):
        def __init__(self, configured, *, enable_live, max_calls):
            super().__init__(configured, enable_live=enable_live, max_calls=max_calls)
            self.adapter = BailianBackend(configured)

        def decide_with_audit(self, obs, record_audit):
            payload, _ = self.adapter.prepare_request(obs)
            record_audit(self.adapter._request_audit(payload))
            if obs["agent_id"] == "agent_0001":
                self.calls += 1
                error = BailianDecisionSchemaError(secret, category="response_contract")
                error.safe_schema_diagnostics = {"error_count": 1, "groups": [
                    {"field": "action", "type": "missing", "count": 1}]}
                raise error
            return self.decide(obs)

    configured = config(api_key=secret)
    monkeypatch.setattr(
        runner.BailianConfig, "from_env",
        classmethod(lambda cls, path, require_api_key, required_models: configured),
    )
    monkeypatch.setattr(runner, "BailianBackend", AuditedOfflineBackend)
    output = tmp_path / "audited.json"
    report = runner.run_synthetic_bailian(
        db_path=tmp_path / "audited.db", output_path=output, env_path=tmp_path / ".env",
        agent_count=2, rounds=1, concurrency=2, max_calls=2,
        budget_cny="1", role_offset=0, purpose="offline audit",
        authorization_label="test-only-no-network", enable_live=True,
    )
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert report["valid_decisions"] == 1
    assert report["failed_decisions"] == 1
    assert saved["trajectory"] == report["trajectory"]
    assert all(row["request_audit"]["stage"] == "prepared_for_dispatch"
               for row in saved["trajectory"])
    assert all(len(row["request_audit"]["prepared_payload_canonical_json_sha256"]) == 64
               for row in saved["trajectory"])
    failed = next(row for row in saved["trajectory"] if row["status"] == "failed")
    assert failed["schema_diagnostics"] == {"error_count": 1, "groups": [
        {"field": "action", "type": "missing", "count": 1}]}
    assert next(row for row in saved["trajectory"] if row["status"] == "valid")[
        "schema_diagnostics"] is None
    assert secret not in json.dumps(saved)


def test_complete_status_does_not_hide_failed_decisions(tmp_path, monkeypatch):
    class FailingBackend(OfflineRunnerBackend):
        def decide(self, obs):
            self.calls += 1
            raise RuntimeError("provider body must stay suppressed")

    configured = config(api_key="runner-test-secret")
    monkeypatch.setattr(
        runner.BailianConfig, "from_env",
        classmethod(lambda cls, path, require_api_key, required_models: configured),
    )
    monkeypatch.setattr(runner, "BailianBackend", FailingBackend)
    report = runner.run_synthetic_bailian(
        db_path=tmp_path / "failed.db", output_path=tmp_path / "failed.json",
        env_path=tmp_path / ".env", agent_count=1, rounds=1, concurrency=1,
        max_calls=1, budget_cny="1", role_offset=0, purpose="failure accounting",
        authorization_label="test-only-no-network", enable_live=True,
    )
    assert report["run_status"] == "complete"
    assert report["completed_rounds"] == 1
    assert report["valid_decisions"] == 0
    assert report["failed_decisions"] == 1
    assert report["completed_without_failures"] is False
    assert report["trajectory"][0]["error"] == "RuntimeError"
    assert OfflineRunnerBackend.instances[-1].closed is True


def test_runner_closes_shared_client_when_engine_raises(tmp_path, monkeypatch):
    instances = []

    class TrackingBackend(OfflineRunnerBackend):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)

    monkeypatch.setattr(
        runner.BailianConfig, "from_env",
        classmethod(lambda cls, path, require_api_key, required_models: config()),
    )
    monkeypatch.setattr(runner, "BailianBackend", TrackingBackend)
    monkeypatch.setattr(runner.Day3Simulation, "advance",
                        lambda self, run_id, backend: (_ for _ in ()).throw(
                            RuntimeError("offline engine failure")))
    with pytest.raises(RuntimeError, match="offline engine failure"):
        runner.run_synthetic_bailian(
            db_path=tmp_path / "run.db", output_path=tmp_path / "run.json",
            env_path=tmp_path / ".env", agent_count=1, rounds=1,
            concurrency=1, max_calls=1, budget_cny="1", role_offset=0,
            purpose="offline lifecycle", authorization_label="test-only",
            enable_live=True,
        )
    assert len(instances) == 1
    assert instances[0].closed is True
    assert not (tmp_path / "run.json").exists()
