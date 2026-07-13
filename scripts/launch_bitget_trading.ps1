$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BaseUrl = 'http://127.0.0.1:8000/'
$RuntimeDir = Join-Path $ProjectRoot '.runtime'
$StdoutLog = Join-Path $RuntimeDir 'desktop-ui-stdout.log'
$StderrLog = Join-Path $RuntimeDir 'desktop-ui-stderr.log'

function Test-PythonRuntime([string] $Candidate) {
    if (-not $Candidate -or -not (Test-Path $Candidate)) {
        return $false
    }
    try {
        & $Candidate -c 'import pandas, uvicorn' 2>$null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

$PythonCandidates = @(
    $env:BITGET_PYTHON,
    (Join-Path $ProjectRoot '.venv\Scripts\python.exe'),
    (Get-Command python.exe -ErrorAction SilentlyContinue).Source,
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
)
$Python = $PythonCandidates |
    Where-Object { Test-PythonRuntime $_ } |
    Select-Object -First 1

if (-not $Python) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        "실행 가능한 Python 환경을 찾지 못했습니다.`n`n프로젝트 폴더에서 다음 명령을 먼저 실행해주세요:`npython -m pip install -r requirements.txt",
        'Bitget Trading',
        'OK',
        'Error'
    ) | Out-Null
    exit 1
}

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

function Test-UiReady {
    try {
        $response = Invoke-WebRequest -Uri $BaseUrl -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    } catch {
        return $false
    }
}

if (-not (Test-UiReady)) {
    Start-Process `
        -FilePath $Python `
        -ArgumentList @('-m', 'src.main', 'ui') `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $StdoutLog `
        -RedirectStandardError $StderrLog `
        -WindowStyle Hidden | Out-Null
}

$ready = $false
1..30 | ForEach-Object {
    if (Test-UiReady) {
        $ready = $true
        return
    }
    Start-Sleep -Milliseconds 500
}

if (-not $ready) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        "Bitget Trading 서버를 시작하지 못했습니다.`n`n로그를 확인해주세요:`n$StderrLog",
        'Bitget Trading',
        'OK',
        'Error'
    ) | Out-Null
    exit 1
}

$ChromeCandidates = @(
    (Join-Path ${env:ProgramFiles} 'Google\Chrome\Application\chrome.exe'),
    (Join-Path ${env:LOCALAPPDATA} 'Google\Chrome\Application\chrome.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe')
)
$Chrome = $ChromeCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if (-not $Chrome) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        "Chrome를 찾지 못했습니다.`n`n다음 주소를 브라우저에서 열어주세요:`n$BaseUrl",
        'Bitget Trading',
        'OK',
        'Warning'
    ) | Out-Null
    exit 0
}

Start-Process -FilePath $Chrome -ArgumentList @('--new-window', '--start-maximized', $BaseUrl) | Out-Null
