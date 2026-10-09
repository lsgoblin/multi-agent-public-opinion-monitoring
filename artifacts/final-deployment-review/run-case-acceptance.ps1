param([string]$BaseUrl = 'http://127.0.0.1:28001')
$ErrorActionPreference = 'Stop'
$out = Split-Path -Parent $MyInvocation.MyCommand.Path
$casePath = Join-Path (Get-Location) 'data/public/unh_change_20240222_day2_multisource_event.json'
$casePackage = Get-Content -Raw -Encoding utf8 $casePath | ConvertFrom-Json
$case = $casePackage.case_import_projection
function Invoke-JsonApi([string]$Method, [string]$Url, [object]$Body = $null) {
    if ($Method -eq 'GET') { return Invoke-RestMethod -Uri $Url -TimeoutSec 20 }
    $json = ConvertTo-Json -InputObject $Body -Depth 100 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    return Invoke-RestMethod -Method $Method -Uri $Url -ContentType 'application/json; charset=utf-8' -Body $bytes -TimeoutSec 20
}
function Get-Sha256([string]$Text) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}
$existing = $null
try { $existing = Invoke-JsonApi 'GET' "$BaseUrl/cases/$($case.case_id)" } catch { $existing = $null }
if ($null -eq $existing) {
    $imported = Invoke-JsonApi 'POST' "$BaseUrl/cases" $case
    $alreadyImported = $false
} else {
    $imported = $existing
    $alreadyImported = $true
}
$storedCase = Invoke-JsonApi 'GET' "$BaseUrl/cases/$($case.case_id)"
if ($storedCase.title -cne $case.title) { throw 'UTF-8 case title differs from the approved fixture' }
$started = Invoke-JsonApi 'POST' "$BaseUrl/day4/historical/jobs" @{ case_id = $case.case_id; agent_count = 10; rounds = 3; concurrency = 4 }
$jobId = $started.job_id
$progress = [System.Collections.Generic.List[object]]::new()
$deadline = (Get-Date).AddSeconds(90)
do {
    $job = Invoke-JsonApi 'GET' "$BaseUrl/jobs/$jobId"
    $snapshot = [pscustomobject]@{ observed_at = (Get-Date).ToUniversalTime().ToString('o'); status = $job.status; result_state = $job.result_state; completed_rounds = $job.completed_rounds; target_rounds = $job.target_rounds; progress = $job.progress }
    $last = if ($progress.Count) { $progress[$progress.Count - 1] } else { $null }
    if (-not $last -or $last.status -ne $snapshot.status -or $last.completed_rounds -ne $snapshot.completed_rounds) { $progress.Add($snapshot) }
    if ($job.status -notin @('queued', 'running')) { break }
    if ((Get-Date) -ge $deadline) { throw "Timed out while waiting for $jobId" }
    Start-Sleep -Milliseconds 200
} while ($true)
if ($job.status -ne 'complete' -or -not $job.all_decisions_valid -or $job.result_state -ne 'success') { throw "Job did not complete successfully: $($job | ConvertTo-Json -Compress)" }
$report = Invoke-JsonApi 'GET' "$BaseUrl/reports/$($job.report_id)"
$alert = Invoke-JsonApi 'GET' "$BaseUrl/alerts/$($job.alert_id)"
$graph = Invoke-JsonApi 'GET' "$BaseUrl/graphs/$($job.graph_id)"
$trajectory = Invoke-JsonApi 'GET' "$BaseUrl/simulations/$($job.run_id)/trajectory"
$binding = [pscustomobject]@{ case_id = $job.case_id; graph_id = $job.graph_id; run_id = $job.run_id }
$moduleNames = @($report.modules.PSObject.Properties.Name | Sort-Object)
$expectedModules = @('emotion_evolution', 'key_nodes', 'propagation', 'recommendations', 'risk')
if (($moduleNames -join ',') -ne ($expectedModules -join ',')) { throw "Unexpected report modules: $($moduleNames -join ',')" }
foreach ($name in $moduleNames) {
    $module = $report.modules.$name
    foreach ($key in @('case_id', 'graph_id', 'run_id')) { if ($module.$key -ne $binding.$key) { throw "Report module $name binding mismatch" } }
}
foreach ($key in @('case_id', 'graph_id', 'run_id')) { if ($report.binding.$key -ne $binding.$key -or $alert.$key -ne $binding.$key) { throw "Task/report/alert binding mismatch for $key" } }
if (-not $graph.claims.Count -or $report.evidence.source_record_ids.Count -eq 0) { throw 'Graph/report has no source records' }
$previews = foreach ($channel in @('wecom', 'email')) {
    $preview = Invoke-JsonApi 'POST' "$BaseUrl/alerts/$($job.alert_id)/previews" @{ channel = $channel }
    if ($preview.delivery_status -ne 'dry_run' -or $preview.network_requests -ne 0 -or $null -ne $preview.sent_at) { throw "Unexpected real notification status for $channel" }
    [pscustomobject]@{ channel = $channel; preview_id = $preview.preview_id; delivery_status = $preview.delivery_status; network_requests = $preview.network_requests; sent_at = $preview.sent_at }
}
$progress | ConvertTo-Json -Depth 10 | Set-Content -Encoding utf8 (Join-Path $out 'progress.json')
$previews | ConvertTo-Json -Depth 10 | Set-Content -Encoding utf8 (Join-Path $out 'notification-previews.json')
[pscustomobject]@{ case_id = $case.case_id; case_title_utf8_matches = $true; case_was_previously_imported = $alreadyImported; import_record_count = $imported.record_count; graph_id = $job.graph_id; run_id = $job.run_id; job_id = $job.job_id; report_id = $job.report_id; alert_id = $job.alert_id; job_status = $job.status; result_state = $job.result_state; execution_mode = $job.execution_mode; data_mode = $job.data_mode; completed_rounds = $job.completed_rounds; target_rounds = $job.target_rounds; all_decisions_valid = $job.all_decisions_valid; action_counts = $job.action_counts; model_calls = $job.model_calls; network_requests = $job.network_requests; report_modules = $moduleNames; graph_claims = $graph.claims.Count; observed_actions = $trajectory.actions.Count; report_sha256 = Get-Sha256 (ConvertTo-Json -InputObject $report -Depth 100 -Compress); alert_sha256 = Get-Sha256 (ConvertTo-Json -InputObject $alert -Depth 100 -Compress); alert_level = $alert.level; alert_status = $alert.status; previews = $previews } | ConvertTo-Json -Depth 20 | Set-Content -Encoding utf8 (Join-Path $out 'case-closure-summary.json')
Get-Content -Raw -Encoding utf8 (Join-Path $out 'case-closure-summary.json')
