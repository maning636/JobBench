$ErrorActionPreference = 'Stop'
# 相对脚本自身定位，不写死绝对路径。
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $root 'server.pid'

if (-not (Test-Path $pidFile)) {
    Write-Output 'no server.pid — 服务本来就没在跑'
    exit 0
}
$raw = (Get-Content $pidFile -Raw).Trim()
if ($raw -notmatch '^\d+$') {
    Write-Output "pid 文件内容不对：$raw"
    Remove-Item $pidFile -Force
    exit 0
}
$procId = [int]$raw
$proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
if (-not $proc) {
    Write-Output "进程 $procId 已经不在了，清掉 pid 文件"
    Remove-Item $pidFile -Force
    exit 0
}
Stop-Process -Id $procId -Force
Remove-Item $pidFile -Force
Write-Output "STOPPED pid=$procId"
