param(
    [string]$BaseUrl = 'http://127.0.0.1:8000',
    [string]$WebUrl = 'http://127.0.0.1:8501/'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$baseUrl = $BaseUrl
$summary = Get-Content -LiteralPath (Join-Path $root 'run-summary.json') -Raw | ConvertFrom-Json

$health = Invoke-RestMethod -Uri "$baseUrl/health"
$page = Invoke-WebRequest -Uri $WebUrl
$job = Invoke-RestMethod -Uri "$baseUrl/jobs/$($summary.job_id)"
$report = Invoke-RestMethod -Uri "$baseUrl/reports/$($summary.report_id)"
$alert = Invoke-RestMethod -Uri "$baseUrl/alerts/$($summary.alert_id)"

$job | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath (Join-Path $root 'post-restart-job.json') -Encoding utf8
$report | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath (Join-Path $root 'post-restart-report.json') -Encoding utf8
$alert | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath (Join-Path $root 'post-restart-alert.json') -Encoding utf8

foreach ($name in @('job', 'report', 'alert')) {
    $beforeFile = switch ($name) { 'job' { 'completed-job.json' } default { "$name.json" } }
    $before = Get-Content -LiteralPath (Join-Path $root $beforeFile) -Raw | ConvertFrom-Json | ConvertTo-Json -Depth 100 -Compress
    $after = Get-Content -LiteralPath (Join-Path $root "post-restart-$name.json") -Raw | ConvertFrom-Json | ConvertTo-Json -Depth 100 -Compress
    if ($before -cne $after) { throw "$name content changed after restart" }
}

if ($health.status -ne 'ok' -or $page.StatusCode -ne 200) { throw 'Health or Web check failed after restart' }
if ($job.status -ne 'complete' -or $job.model_calls -ne 0 -or $job.network_requests -ne 0) { throw 'Job state or counters changed after restart' }

[ordered]@{
    checked_at = Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz'
    health = $health.status
    web_http_status = [int]$page.StatusCode
    run_id = $job.run_id
    job_id = $job.job_id
    report_id = $report.report_id
    alert_id = $alert.alert_id
    job_report_alert_content_equal = $true
    model_calls = $job.model_calls
    network_requests = $job.network_requests
} | ConvertTo-Json | Tee-Object -FilePath (Join-Path $root 'restart-summary.json')
