$ErrorActionPreference = 'Stop'

$Repo = 'https://github.com/HeyKlee/TurnToAPI.git'
$Root = Join-Path $env:LOCALAPPDATA 'TurnToAPI'

if (Get-Command py -ErrorAction SilentlyContinue) { $Python = 'py' }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $Python = 'python' }
else { throw 'Python 3.11+ is required.' }

if (!(Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git for Windows is required.' }

if (Test-Path (Join-Path $Root '.git')) {
    git -C $Root fetch --prune origin
    git -C $Root checkout main
    git -C $Root pull --ff-only origin main
}
else {
    if (Test-Path $Root) { Remove-Item $Root -Recurse -Force }
    git clone $Repo $Root
}

Set-Location $Root
& $Python -m venv '.venv'
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
& $VenvPython -m pip install --upgrade pip wheel setuptools
& $VenvPython -m pip install -r (Join-Path $Root 'requirements.txt')
& $VenvPython -m playwright install firefox

foreach ($Dir in @('logs','browser_debug','playwright_profiles\arena','playwright_profiles\chatgpt')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root $Dir) | Out-Null
}

$Config = Join-Path $Root 'config.yaml'
if (!(Test-Path $Config)) { Copy-Item (Join-Path $Root 'config.example.yaml') $Config }

$BindHost = '127.0.0.1'
if (Get-Command tailscale -ErrorAction SilentlyContinue) {
    $Candidate = (& tailscale ip -4 2>$null | Select-Object -First 1)
    if ($Candidate) { $BindHost = $Candidate.Trim() }
}
Set-Content -Path (Join-Path $Root '.turntoapi-bind') -Value $BindHost -Encoding ASCII

$StartScript = @'
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$HostAddress = (Get-Content (Join-Path $Root '.turntoapi-bind') -Raw).Trim()
& $Python (Join-Path $Root 'turn_to_api_server.py') --host $HostAddress --port 8000 --config (Join-Path $Root 'config.yaml')
'@
Set-Content -Path (Join-Path $Root 'start-turntoapi.ps1') -Value $StartScript -Encoding UTF8

$KillScript = @'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
Where-Object {
    $_.CommandLine -and $_.CommandLine -like "*$Root*" -and
    ($_.CommandLine -like '*turn_to_api_server.py*' -or $_.CommandLine -like '*turn_to_api_live_proxy.py*')
} |
ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host 'TurnToAPI stopped. Normal Firefox was not targeted.' -ForegroundColor Green
'@
Set-Content -Path (Join-Path $Root 'kill-turntoapi.ps1') -Value $KillScript -Encoding UTF8

& $VenvPython -m py_compile (Join-Path $Root 'turn_to_api_server.py') (Join-Path $Root 'browser_web_adapter.py') (Join-Path $Root 'arena_model_registry.py') (Join-Path $Root 'turn_to_api_live_proxy.py')

$Args = @('-NoProfile','-ExecutionPolicy','Bypass','-File',(Join-Path $Root 'start-turntoapi.ps1'))
Start-Process -FilePath 'powershell.exe' -ArgumentList $Args -WorkingDirectory $Root -WindowStyle Hidden

$Ready = $false
for ($i = 0; $i -lt 40; $i++) {
    try {
        $Health = Invoke-RestMethod ("http://" + $BindHost + ":8000/health") -TimeoutSec 2
        if ($Health.status -eq 'online') { $Ready = $true; break }
    }
    catch {}
    Start-Sleep -Milliseconds 500
}

if ($Ready) {
    Write-Host 'TurnToAPI installed successfully.' -ForegroundColor Green
    Write-Host ("Endpoint: http://" + $BindHost + ":8000/v1")
}
else {
    Write-Warning 'Install completed, but health check did not pass yet.'
}

Write-Host ("Install directory: " + $Root)
Write-Host 'Arena uses a persistent headed Firefox. Complete security verification manually if shown.'
