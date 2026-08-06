$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
python -m pip install 'playwright>=1.45,<2'
Write-Host 'Playwright 已安装。应用将使用电脑上已安装的 Microsoft Edge 独立配置；自动模式仍为实验性，请保留人工降级并遵守平台规则。'
