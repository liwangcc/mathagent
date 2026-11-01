$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$start = Join-Path $scriptRoot "start-platform-node.ps1"
$stop = Join-Path $scriptRoot "stop-platform-node.ps1"
$status = Join-Path $scriptRoot "platform-node-status.ps1"

while ($true) {
    Clear-Host
    Write-Host "MathAgent 平台加速节点" -ForegroundColor Cyan
    Write-Host "1. 开启节点"
    Write-Host "2. 关闭节点"
    Write-Host "3. 查看状态"
    Write-Host "4. 退出"
    $choice = Read-Host "请选择"
    try {
        switch ($choice) {
            "1" { & $start | Format-List }
            "2" { & $stop | Format-List }
            "3" { & $status | Format-List }
            "4" { return }
            default { Write-Host "无效选择" -ForegroundColor Yellow }
        }
    }
    catch {
        Write-Host $_.Exception.Message -ForegroundColor Red
    }
    if ($choice -ne "4") {
        Read-Host "按回车继续"
    }
}
