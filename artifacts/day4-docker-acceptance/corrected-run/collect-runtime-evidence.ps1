$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$lines = [System.Collections.Generic.List[string]]::new()
$lines.Add('Day 4 Docker local acceptance, corrected image and fresh project')
$lines.Add("captured_at=$(Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz')")
$lines.Add('project=riskshieldaccept; host API=127.0.0.1:18000; host Web=127.0.0.1:18501')

function Record([string]$label, [scriptblock]$action) {
    $lines.Add('')
    $lines.Add("$ $label")
    $result = & $action 2>&1 | Out-String
    $lines.Add($result.TrimEnd())
    if ($LASTEXITCODE -ne 0 -and $null -ne $LASTEXITCODE) {
        throw "$label exited with $LASTEXITCODE"
    }
}

Record 'docker compose -p riskshieldaccept ps -a' { docker compose -p riskshieldaccept ps -a }
Record 'docker inspect corrected containers (ID, image, start time)' { docker inspect riskshieldaccept-api-1 riskshieldaccept-web-1 riskshieldaccept-local_gateway-1 --format '{{.Name}} {{.Id}} {{.Image}} {{.State.StartedAt}}' }
Record 'docker network inspect riskshieldaccept_default --format {{.Internal}}' { docker network inspect riskshieldaccept_default --format '{{.Internal}}' }
Record 'docker network inspect riskshieldaccept_host_ingress --format {{.Internal}}' { docker network inspect riskshieldaccept_host_ingress --format '{{.Internal}}' }
Record 'docker volume inspect riskshieldaccept_riskshield_runtime' { docker volume inspect riskshieldaccept_riskshield_runtime --format '{{.Name}} {{.CreatedAt}}' }
Record 'api /proc/net/route and TCP connect_ex 1.1.1.1:443' { docker compose -p riskshieldaccept exec -T api python -c "import pathlib,socket; print(pathlib.Path('/proc/net/route').read_text()); s=socket.socket(); s.settimeout(2); print('connect_ex='+str(s.connect_ex(('1.1.1.1',443))))" }
Record 'web /proc/net/route, TCP connect_ex 1.1.1.1:443, and api health' { docker compose -p riskshieldaccept exec -T web python -c "import pathlib,socket,urllib.request; print(pathlib.Path('/proc/net/route').read_text()); s=socket.socket(); s.settimeout(2); print('connect_ex='+str(s.connect_ex(('1.1.1.1',443)))); print('web_to_api='+str(urllib.request.urlopen('http://api:8000/health',timeout=3).status))" }
Record 'local_gateway /proc/net/route' { docker compose -p riskshieldaccept exec -T local_gateway python -c "import pathlib; print(pathlib.Path('/proc/net/route').read_text())" }
Record 'GET /health, /readiness, /channels/status and Web root' {
    $health = Invoke-RestMethod -Uri 'http://127.0.0.1:18000/health'
    $readiness = Invoke-RestMethod -Uri 'http://127.0.0.1:18000/readiness'
    $channels = Invoke-WebRequest -Uri 'http://127.0.0.1:18000/channels/status'
    $web = Invoke-WebRequest -Uri 'http://127.0.0.1:18501/'
    [ordered]@{ health = $health; g3_v2_passed = $readiness.g3_v2_passed; g4_v2_passed = $readiness.g4_v2_passed; channels_http_status = [int]$channels.StatusCode; web_http_status = [int]$web.StatusCode } | ConvertTo-Json -Depth 5
}
Record 'corrected-run/run-summary.json' { Get-Content -LiteralPath (Join-Path $root 'run-summary.json') -Raw }
Record 'corrected-run/restart-summary.json' { Get-Content -LiteralPath (Join-Path $root 'restart-summary.json') -Raw }

$lines | Set-Content -LiteralPath (Join-Path $root 'runtime-cli.txt') -Encoding utf8
Write-Output (Join-Path $root 'runtime-cli.txt')
