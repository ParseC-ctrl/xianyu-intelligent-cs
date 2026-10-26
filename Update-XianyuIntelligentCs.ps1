#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$Distro = '',
    [string]$ProjectPath = '',
    [string]$WslUser = '',
    [switch]$NoBrowser,
    [switch]$NoPause
)
$ErrorActionPreference = 'Stop'
$Launcher = Join-Path $PSScriptRoot 'Start-XianyuIntelligentCs.ps1'
if (-not (Test-Path -LiteralPath $Launcher)) {
    Write-Host 'Place the latest Start-XianyuIntelligentCs.ps1 beside this updater.' -ForegroundColor Red
    if (-not $NoPause) { [void](Read-Host 'Press Enter to close') }
    exit 1
}
& $Launcher @PSBoundParameters -Update
exit $LASTEXITCODE
