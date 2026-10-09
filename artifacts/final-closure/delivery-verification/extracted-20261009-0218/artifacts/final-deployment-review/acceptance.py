"""Local deployment acceptance against the API running in the same container."""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen


BASE_URL = "http://127.0.0.1:8000"
RUNTIME = Path("/app/runtime")
CASE_PATH = Path("/app/data/public/unh_change_20240222_day2_multisource_event.json")


def call(path: str, method: str = "GET", body: dict | None = None) -> tuple[dict, bytes]:
    data = None if body is None else json.dumps(
        body, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    request = Request(
        BASE_URL + path,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method=method,
    )
    with urlopen(request, timeout=20) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")), raw


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def run() -> dict:
    package_raw = CASE_PATH.read_bytes()
    package = json.loads(package_raw.decode("utf-8"))
    case = package["case_import_projection"]
    try:
        call(f"/cases/{case['case_id']}")
        imported = False
    except Exception:
        call("/cases", "POST", case)
        imported = True
    stored_case, _ = call(f"/cases/{case['case_id']}")
    if stored_case["title"] != case["title"]:
        raise RuntimeError("Stored case title differs from UTF-8 fixture")

    started, _ = call("/day4/historical/jobs", "POST", {
        "case_id": case["case_id"], "agent_count": 10, "rounds": 3,
        "concurrency": 4,
    })
    job_id = started["job_id"]
    progress = []
    deadline = time.monotonic() + 90
    while True:
        job, job_raw = call(f"/jobs/{job_id}")
        snapshot = {
            "status": job["status"],
            "result_state": job["result_state"],
            "completed_rounds": job["completed_rounds"],
            "target_rounds": job["target_rounds"],
            "progress": job["progress"],
        }
        if not progress or snapshot != progress[-1]:
            progress.append(snapshot)
        if job["status"] not in {"queued", "running"}:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for {job_id}")
        time.sleep(0.2)
    if job["status"] != "complete" or job["result_state"] != "success" or not job["all_decisions_valid"]:
        raise RuntimeError(f"Incomplete case run: {snapshot}")

    report, report_raw = call(f"/reports/{job['report_id']}")
    alert, alert_raw = call(f"/alerts/{job['alert_id']}")
    graph, _ = call(f"/graphs/{job['graph_id']}")
    trajectory, _ = call(f"/simulations/{job['run_id']}/trajectory")
    binding = {key: job[key] for key in ("case_id", "graph_id", "run_id")}
    modules = sorted(report["modules"])
    expected_modules = sorted((
        "emotion_evolution", "key_nodes", "propagation", "recommendations", "risk",
    ))
    if modules != expected_modules or report["binding"] != binding:
        raise RuntimeError("Report binding or five-module set does not match task")
    if any(any(module.get(key) != value for key, value in binding.items())
           for module in report["modules"].values()):
        raise RuntimeError("A report module is bound to a different case, graph, or run")
    if any(alert.get(key) != value for key, value in binding.items()):
        raise RuntimeError("Alert is bound to a different case, graph, or run")
    if not graph["claims"] or not report["evidence"]["source_record_ids"]:
        raise RuntimeError("Graph or report has no cutoff source records")

    previews = []
    for channel in ("wecom", "email"):
        preview, _ = call(f"/alerts/{job['alert_id']}/previews", "POST", {"channel": channel})
        if (preview["delivery_status"] != "dry_run" or preview["network_requests"] != 0
                or preview["sent_at"] is not None):
            raise RuntimeError(f"Unexpected notification delivery for {channel}")
        previews.append({key: preview[key] for key in (
            "channel", "preview_id", "delivery_status", "network_requests", "sent_at",
        )})

    records = {
        "case": call(f"/cases/{case['case_id']}")[1],
        "job": job_raw,
        "report": report_raw,
        "alert": alert_raw,
    }
    before = {
        "case_id": case["case_id"], "job_id": job_id,
        "report_id": job["report_id"], "alert_id": job["alert_id"],
        "sha256": {name: sha256(raw) for name, raw in records.items()},
    }
    write_json(RUNTIME / "persistence-before-restart.json", before)
    summary = {
        "case_id": case["case_id"],
        "case_title_utf8_matches": True,
        "case_imported": imported,
        "case_input_sha256": sha256(package_raw),
        "graph_id": job["graph_id"], "run_id": job["run_id"],
        "job_id": job_id, "report_id": job["report_id"], "alert_id": job["alert_id"],
        "job_status": job["status"], "result_state": job["result_state"],
        "execution_mode": job["execution_mode"], "data_mode": job["data_mode"],
        "completed_rounds": job["completed_rounds"], "target_rounds": job["target_rounds"],
        "all_decisions_valid": job["all_decisions_valid"],
        "action_counts": job["action_counts"],
        "model_calls": job["model_calls"], "network_requests": job["network_requests"],
        "progress": progress, "report_modules": modules,
        "graph_claims": len(graph["claims"]),
        "observed_actions": len(trajectory["actions"]),
        "report_sha256": before["sha256"]["report"],
        "alert_sha256": before["sha256"]["alert"],
        "alert_level": alert["level"], "alert_status": alert["status"],
        "previews": previews,
    }
    write_json(RUNTIME / "case-closure-summary.json", summary)
    return summary


def verify_restart() -> dict:
    before = json.loads((RUNTIME / "persistence-before-restart.json").read_text(encoding="utf-8"))
    records = {
        "case": call(f"/cases/{before['case_id']}")[1],
        "job": call(f"/jobs/{before['job_id']}")[1],
        "report": call(f"/reports/{before['report_id']}")[1],
        "alert": call(f"/alerts/{before['alert_id']}")[1],
    }
    after = {
        "case_id": before["case_id"], "job_id": before["job_id"],
        "report_id": before["report_id"], "alert_id": before["alert_id"],
        "sha256": {name: sha256(raw) for name, raw in records.items()},
    }
    health, _ = call("/health")
    job, _ = call(f"/jobs/{before['job_id']}")
    if before["sha256"] != after["sha256"] or job["result_state"] != "success":
        raise RuntimeError("Case, task, report, or alert changed after restart")
    if health.get("status") != "ok":
        raise RuntimeError("API health check failed after restart")
    write_json(RUNTIME / "persistence-after-restart.json", after)
    result = {"all_record_hashes_match": True, "api_health": health["status"],
              "job_state": job["result_state"], **after}
    write_json(RUNTIME / "restart-persistence-summary.json", result)
    return result


if __name__ == "__main__":
    if sys.argv[1:] == ["verify-restart"]:
        output = verify_restart()
    else:
        output = run()
    print(json.dumps(output, ensure_ascii=False, indent=2))
