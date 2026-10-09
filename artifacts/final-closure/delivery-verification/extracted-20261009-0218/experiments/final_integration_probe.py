"""Run one local API-to-report historical case with an offline substitute."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from riskshield.api import create_app
from riskshield.schemas import CaseImport
from riskshield.store import Store


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "data/public/unh_change_20240222_day2_multisource_event.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path,
        default=Path("artifacts/final-integration-review/end-to-end-summary-20261009.json"),
        help="Write the run summary to a new workspace-relative or absolute path.",
    )
    args = parser.parse_args()
    output = (ROOT / args.out).resolve() if not args.out.is_absolute() else args.out.resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("Integration evidence output must stay inside the workspace")

    package = json.loads(PACKAGE.read_text(encoding="utf-8"))
    case = CaseImport.model_validate(package["case_import_projection"])
    with tempfile.TemporaryDirectory(prefix="riskshield-integration-") as directory:
        database = Path(directory) / "integration.sqlite3"
        Store(database).import_case(case)
        with TestClient(create_app(database)) as client:
            started_at = time.perf_counter()
            response = client.post("/day4/historical/jobs", json={
                "case_id": case.case_id, "agent_count": 10, "rounds": 3,
                "concurrency": 4,
            })
            response.raise_for_status()
            job = response.json()
            deadline = time.monotonic() + 30
            while job["status"] in {"queued", "running"} and time.monotonic() < deadline:
                time.sleep(0.02)
                job_response = client.get(f"/jobs/{job['job_id']}")
                job_response.raise_for_status()
                job = job_response.json()
            if job["status"] != "complete" or not job["all_decisions_valid"]:
                raise RuntimeError(f"Offline integration job incomplete: {job['result_state']}")
            report_response = client.get(f"/reports/{job['report_id']}")
            report_response.raise_for_status()
            report = report_response.json()
            report_readable_seconds = round(time.perf_counter() - started_at, 6)
            alert_response = client.get(f"/alerts/{job['alert_id']}")
            alert_response.raise_for_status()
            alert = alert_response.json()
            graph_response = client.get(f"/graphs/{job['graph_id']}")
            graph_response.raise_for_status()
            graph = graph_response.json()
            trajectory_response = client.get(f"/simulations/{job['run_id']}/trajectory")
            trajectory_response.raise_for_status()
            actions = trajectory_response.json()["actions"]
            previews = []
            for channel in ("wecom", "email"):
                preview_response = client.post(
                    f"/alerts/{job['alert_id']}/previews", json={"channel": channel})
                preview_response.raise_for_status()
                preview = preview_response.json()
                previews.append({"channel": channel,
                                 "delivery_status": preview["delivery_status"],
                                 "network_requests": preview["network_requests"],
                                 "sent_at": preview["sent_at"]})
    binding = {"case_id": case.case_id, "graph_id": job["graph_id"],
               "run_id": job["run_id"]}
    if report["binding"] != binding:
        raise RuntimeError("Report binding differs from task")
    if any(module[key] != value for module in report["modules"].values()
           for key, value in binding.items()):
        raise RuntimeError("A report module differs from task binding")
    allowed = set(report["evidence"]["source_record_ids"])
    if (not allowed or any(not set(action["observation_refs"]["evidence"]) <= allowed
                           for action in actions)):
        raise RuntimeError("Simulation used evidence outside the cutoff graph")
    if any(item["delivery_status"] != "dry_run" or item["network_requests"] != 0
           or item["sent_at"] is not None for item in previews):
        raise RuntimeError("A preview was unexpectedly sent")
    summary = {
        "schema": "riskshield.final_integration.end_to_end.v2",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "execution_mode": job["execution_mode"],
        "data_mode": job["data_mode"],
        "graph_build_mode": report["graph_build_mode"],
        "source_package_sha256": digest(PACKAGE),
        "code_sha256": {name: digest(ROOT / name) for name in (
            "src/riskshield/api.py", "src/riskshield/schemas.py",
            "src/riskshield/store.py", "src/riskshield/evidence.py",
            "src/riskshield/tasks.py",
            "src/riskshield/simulation.py", "src/riskshield/reporting.py",
            "src/riskshield/alerts.py", "src/riskshield/model_gateway.py",
            "ui/app.py")},
        "binding": binding,
        "job_id": job["job_id"], "report_id": job["report_id"],
        "alert_id": job["alert_id"],
        "job_status": job["status"], "result_state": job["result_state"],
        "simulation_status": job["simulation_status"],
        "all_decisions_valid": job["all_decisions_valid"],
        "action_counts": job["action_counts"],
        "completed_rounds": job["completed_rounds"],
        "target_rounds": job["target_rounds"],
        "model_calls": job["model_calls"],
        "network_requests": job["network_requests"],
        "input_received_at": job["input_received_at"],
        "report_generated_at": job["report_generated_at"],
        "input_to_report_seconds": job["elapsed_seconds"],
        "input_to_report_readable_seconds": report_readable_seconds,
        "graph_claim_record_ids": sorted(claim["record_id"] for claim in graph["claims"]),
        "report_source_record_ids": sorted(allowed),
        "excluded_counts": graph["excluded_counts"],
        "report_modules": sorted(report["modules"]),
        "actions_with_runtime_messages": sum(bool(row["observation_refs"]["messages"])
                                             for row in actions),
        "actions_with_own_memory": sum(bool(row["observation_refs"]["memories"])
                                       for row in actions),
        "alert_level": alert["level"],
        "alert_status": alert["status"],
        "previews": previews,
        "limitations": ["Local archived-record projection, not online GraphRAG",
                        "Offline dynamic substitute, not a real model run",
                        "Dry-run previews, not delivery or 30-minute notification evidence"],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(output), "binding": binding,
                      "input_to_report_seconds": job["elapsed_seconds"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
