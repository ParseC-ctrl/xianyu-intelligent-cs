#requires -Version 5.1
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Source = Join-Path $ProjectRoot 'Start-XianyuIntelligentCs.ps1'
$Tokens = $null
$ParseErrors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile($Source, [ref]$Tokens, [ref]$ParseErrors)
if ($ParseErrors.Count) { throw ($ParseErrors | Out-String) }
$UpdateSource = Join-Path $ProjectRoot 'Update-XianyuIntelligentCs.ps1'
[void][System.Management.Automation.Language.Parser]::ParseFile($UpdateSource, [ref]$Tokens, [ref]$ParseErrors)
if ($ParseErrors.Count) { throw ($ParseErrors | Out-String) }
$Temporary = Join-Path ([IO.Path]::GetTempPath()) ('xianyu-launcher-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Temporary | Out-Null
try {
    $Target = Join-Path $Temporary 'Start-XianyuIntelligentCs.ps1'
    Copy-Item -LiteralPath $Source -Destination $Target
    Copy-Item -LiteralPath $UpdateSource -Destination (Join-Path $Temporary 'Update-XianyuIntelligentCs.ps1')
    $Wrapper = Join-Path $Temporary 'fake-wsl.ps1'
    @'
param([string]$Target,[string]$Scenario,[string]$StatePath)
$global:LauncherScenario=$Scenario
$global:LauncherStatePath=$StatePath
function global:wsl.exe {
    $global:LASTEXITCODE=0
    if ($args -contains '--quiet') { 'Ubuntu';return }
    if ($args -contains '--verbose') {
        if ($global:LauncherScenario -eq 'wsl1') { '* Ubuntu Stopped 1' } else { '* Ubuntu Stopped 2' }
        return
    }
    $Command=[string]$args[-1]
    $Match=[regex]::Match($Command,'^printf %s ([A-Za-z0-9+/=]+) \| base64 -d \| bash$')
    if (-not $Match.Success) { throw 'Unsafe or invalid WSL transport command' }
    $Body=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Match.Groups[1].Value))
    if ($Body.Contains("`r")) { throw 'CRLF was passed into Linux bash' }
    $RootUser=$args -contains 'root'
    @{Root=$RootUser;Body=$Body} | ConvertTo-Json -Compress | Add-Content -LiteralPath $global:LauncherStatePath -Encoding UTF8
    if ($RootUser) { return }
    if ($global:LauncherScenario -eq 'app-fails') { $global:LASTEXITCODE=7;return }
    '__XIANYU_URL__=http://127.0.0.1:4173/xianyu-intelligent-cs/'
    '__XIANYU_PROJECT__=/home/fixture/apps/existing'
}
function global:Start-Process { param($FilePath) throw 'Synthetic missing default browser' }
function global:Invoke-WebRequest { param($Uri,[switch]$UseBasicParsing,$TimeoutSec) return [pscustomobject]@{StatusCode=200} }
$Extra = @{}
if ($Scenario -eq 'update-faq') { $Extra.FaqSource = "C:\private folder\faqs.json"; $Extra.AccountKey = 'second' }
& $Target @Extra -Distro Ubuntu -ProjectPath "/home/fixture/space ' quote" -NoBrowser:($Scenario -ne 'browser-fails') -NoPause
exit $LASTEXITCODE
'@ | Set-Content -LiteralPath $Wrapper -Encoding UTF8
    foreach ($Scenario in @('success','update','update-faq','browser-fails','wsl1','app-fails')) {
        $Settings = Join-Path $Temporary '.local\windows-start.json'
        if (Test-Path $Settings) { Remove-Item -LiteralPath $Settings }
        $State = Join-Path $Temporary ($Scenario+'.jsonl')
        $EntryPoint = $Target
        if (@('update','update-faq') -contains $Scenario) { $EntryPoint = Join-Path $Temporary 'Update-XianyuIntelligentCs.ps1' }
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Wrapper -Target $EntryPoint -Scenario $Scenario -StatePath $State
        $Code=$LASTEXITCODE
        if (@('success','update','update-faq','browser-fails') -contains $Scenario) {
            if ($Code -ne 0) { throw 'Normal startup failed' }
            $Rows=@(Get-Content -LiteralPath $State -Encoding UTF8 | ForEach-Object { $_ | ConvertFrom-Json })
            if ($Rows.Count -ne 2 -or -not $Rows[0].Root -or $Rows[1].Root) { throw 'Root must only be used for Docker startup' }
            if (-not (Test-Path $Settings)) { throw 'Successful selection was not saved' }
            $Encoded=[regex]::Match($Rows[1].Body,"project_encoded='([^']*)'").Groups[1].Value
            $Decoded=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Encoded))
            if ($Decoded -ne "/home/fixture/space ' quote") { throw 'Project argument corrupted' }
            if ($Scenario -eq 'update' -and -not $Rows[1].Body.Contains("action_encoded='dXBkYXRl'")) { throw 'Update wrapper did not request an update' }
            if ($Scenario -eq 'update-faq') {
                $FaqEncoded=[regex]::Match($Rows[1].Body,"faq_source_encoded='([^']*)'").Groups[1].Value
                if ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($FaqEncoded)) -ne "C:\private folder\faqs.json") { throw 'Private FAQ path corrupted' }
                if (-not $Rows[1].Body.Contains("faq_account_encoded='c2Vjb25k'")) { throw 'FAQ account scope missing' }
            }
        } else {
            if ($Code -eq 0) { throw 'Failure was reported as success' }
            if (Test-Path $Settings) { throw 'Failed startup overwrote saved selection' }
            if ($Scenario -eq 'wsl1' -and (Test-Path $State)) { throw 'WSL1 must fail before starting Docker' }
        }
    }
    Write-Host 'Windows launcher: PowerShell 5.1 syntax, root separation, safe arguments, settings and failures passed'
} finally {
    Remove-Item -LiteralPath $Temporary -Recurse -Force
}

exit 0
