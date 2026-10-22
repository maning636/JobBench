$ErrorActionPreference = 'Stop'
# 相对脚本自身定位，不写死绝对路径 —— 否则换台机器/换个目录就跑不起来。
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$log  = Join-Path $root 'server.log'
$pidFile = Join-Path $root 'server.pid'

function Get-JobBenchPid {
    if (Test-Path $pidFile) {
        $raw = (Get-Content $pidFile -Raw).Trim()
        if ($raw -match '^\d+$') {
            $proc = Get-Process -Id ([int]$raw) -ErrorAction SilentlyContinue
            if ($proc) { return [int]$raw }
        }
    }
    return $null
}

$existing = Get-JobBenchPid
if ($existing) {
    Write-Output "ALREADY RUNNING pid=$existing"
    exit 0
}

# pid 文件在但进程已经没了 —— 清掉，否则每次都误判成"还在跑"
if (Test-Path $pidFile) {
    Write-Output "stale pid file, cleaning"
    Remove-Item $pidFile -Force
}

# 优先用项目自己的 venv，其次回落到系统 python。
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source } else { Write-Output 'NO PYTHON FOUND'; exit 1 }
}

$p = Start-Process -FilePath $python `
    -ArgumentList '-X','utf8','-m','jobforge.web.app','--port','3399','--db','data\jobforge.db' `
    -WorkingDirectory $root `
    -RedirectStandardOutput $log `
    -RedirectStandardError (Join-Path $root 'server.err.log') `
    -WindowStyle Hidden `
    -PassThru
Set-Content -Path $pidFile -Value $p.Id -Encoding ascii -NoNewline
Write-Output "STARTED pid=$($p.Id)"
