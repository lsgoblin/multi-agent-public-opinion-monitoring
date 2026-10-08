param([string]$BaseUrl = 'http://127.0.0.1:8000')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$baseUrl = $BaseUrl
$startedAt = Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz'

$prepared = Invoke-RestMethod -Method Post -Uri "$baseUrl/demos/day4/run" -ContentType 'application/json' -Body '{"agent_count":10,"rounds":3,"concurrency":4}'
$prepared | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath (Join-Path $root 'prepared-run.json') -Encoding utf8

$started = Invoke-RestMethod -Method Post -Uri "$baseUrl/jobs" -ContentType 'application/json' -Body (@{ run_id = $prepared.run_id } | ConvertTo-Json -Compress)
$started | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath (Join-Path $root 'started-job.json') -Encoding utf8

$deadline = (Get-Date).AddMinutes(2)
do {
    $job = Invoke-RestMethod -Uri "$baseUrl/jobs/$($started.job_id)"
    if ($job.status -in @('complete', 'failed')) { break }
    Start-Sleep -Seconds 1
} while ((Get-Date) -lt $deadline)

$job | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath (Join-Path $root 'completed-job.json') -Encoding utf8
if ($job.status -ne 'complete') { throw "Job did not complete: $($job.status)" }

$report = Invoke-RestMethod -Uri "$baseUrl/reports/$($job.report_id)"
$alert = Invoke-RestMethod -Uri "$baseUrl/alerts/$($job.alert_id)"
$report | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath (Join-Path $root 'report.json') -Encoding utf8
$alert | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath (Join-Path $root 'alert.json') -Encoding utf8

if ($job.model_calls -ne 0 -or $job.network_requests -ne 0) { throw 'Offline counters are not zero' }
if ($job.completed_rounds -ne 3 -or $job.target_rounds -ne 3) { throw 'Round count mismatch' }
if ($report.report_id -ne $job.report_id -or $alert.alert_id -ne $job.alert_id) { throw 'Report or alert ID mismatch' }

$summary = [ordered]@{
    started_at = $startedAt
    completed_at = Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz'
    run_id = $job.run_id
    job_id = $job.job_id
    report_id = $job.report_id
    alert_id = $job.alert_id
    status = $job.status
    completed_rounds = $job.completed_rounds
    model_calls = $job.model_calls
    network_requests = $job.network_requests
}
$summary | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'run-summary.json') -Encoding utf8
$summary | ConvertTo-Json -Compress
