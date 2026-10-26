#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$Distro = '',
    [string]$ProjectPath = '',
    [string]$WslUser = '',
    [switch]$Build,
    [switch]$NoBrowser,
    [switch]$NoPause
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$SettingsPath = Join-Path $PSScriptRoot '.local\windows-start.json'
$script:ServiceUrl = ''
$script:ResolvedProject = ''
$ExitCode = 0

# ASCII source supports Windows PowerShell 5.1 without an encoding-dependent BOM.
# Arguments are UTF-8/base64 data, never interpolated as executable shell text.
function Encode-Text([string]$Text) {
    return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Text))
}
function Invoke-WslScript([string]$Text, [string]$UserName) {
    $Text = $Text.Replace("`r`n", "`n").Replace("`r", "`n")
    $Encoded = Encode-Text $Text
    $NativeArgs = @('--distribution', $Distro)
    if ($UserName) { $NativeArgs += @('--user', $UserName) }
    $NativeArgs += @('--exec', 'bash', '-lc', "printf %s $Encoded | base64 -d | bash")
    & wsl.exe @NativeArgs | ForEach-Object {
        $Line = ([string]$_).Replace([string][char]0, '').TrimEnd()
        if ($Line.StartsWith('__XIANYU_URL__=')) { $script:ServiceUrl = $Line.Substring(15) }
        elseif ($Line.StartsWith('__XIANYU_PROJECT__=')) { $script:ResolvedProject = $Line.Substring(19) }
        else { Write-Host $Line }
    }
    if ($LASTEXITCODE -ne 0) { throw "WSL command failed (exit $LASTEXITCODE). See the error above." }
}

$EngineScript = @'
set -euo pipefail
export DOCKER_HOST=unix:///var/run/docker.sock
unset DOCKER_CONTEXT
command -v docker >/dev/null || { echo 'Docker Engine is not installed in this WSL distribution.' >&2; exit 1; }
if docker info >/dev/null 2>&1; then exit 0; fi
echo 'Starting native Docker Engine inside WSL...'
if [[ "$(ps -p 1 -o comm= | tr -d ' ')" == systemd ]]; then
    systemctl start docker
elif [[ -x /etc/init.d/docker ]]; then
    service docker start
elif command -v pgrep >/dev/null && pgrep -x dockerd >/dev/null; then
    echo 'A dockerd process already exists; waiting for its local socket.'
else
    command -v dockerd >/dev/null || { echo 'dockerd is not installed.' >&2; exit 1; }
    nohup dockerd > /var/log/docker-wsl.log 2>&1 < /dev/null &
fi
for ((attempt=0;attempt<30;attempt++)); do
    if docker info >/dev/null 2>&1; then exit 0; fi
    sleep 2
done
echo 'Docker Engine did not become ready. Inspect the docker service or /var/log/docker-wsl.log.' >&2
exit 1
'@

$ServiceScript = @'
set -euo pipefail
export DOCKER_HOST=unix:///var/run/docker.sock
unset DOCKER_CONTEXT
project="$(printf %s "$project_encoded" | base64 -d)"
action="$(printf %s "$action_encoded" | base64 -d)"
if ! docker info >/dev/null 2>&1; then
    echo 'Docker is running but this WSL user cannot access its socket. Use the original deployment user or fix docker group membership.' >&2
    exit 1
fi
valid_project() { [[ -f "$1/compose.customer.yml" && -f "$1/config/customer.env" && -f "$1/scripts/customer.sh" ]]; }
if [[ -z "$project" ]]; then
    candidates=()
    while IFS= read -r id; do
        [[ -n "$id" ]] || continue
        dir="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$id")"
        if valid_project "$dir"; then candidates+=("$dir"); fi
    done < <(docker ps -aq --filter label=com.docker.compose.project=xianyu-intelligent-cs --filter label=com.docker.compose.service=customer)
    if (( ${#candidates[@]} == 0 )); then
        for dir in "$HOME/apps/xianyu-intelligent-cs-public" "$HOME/apps/xianyu-intelligent-cs"; do
            if valid_project "$dir"; then candidates+=("$dir"); fi
        done
    fi
    unique=()
    if (( ${#candidates[@]} )); then
    for dir in "${candidates[@]}"; do
        duplicate=0
        if (( ${#unique[@]} )); then
            for previous in "${unique[@]}"; do [[ "$dir" != "$previous" ]] || duplicate=1; done
        fi
        (( duplicate )) || unique+=("$dir")
    done
    fi
    if (( ${#unique[@]} != 1 )); then
        echo 'Could not select exactly one existing deployment. Specify -ProjectPath with its WSL Linux path.' >&2
        if (( ${#unique[@]} )); then printf 'Candidate: %s\n' "${unique[@]}" >&2; fi
        exit 1
    fi
    project="${unique[0]}"
fi
if [[ "$project" == '~/'* ]]; then project="$HOME/${project:2}"; fi
valid_project "$project" || { echo 'Existing deployment/config not found. This launcher will not create a new deployment or master key.' >&2; exit 1; }
cd -- "$project"
project="$PWD"
command -v python3 >/dev/null || { echo 'python3 is required inside WSL.' >&2; exit 1; }
echo "Starting existing deployment: $project"
if [[ "$action" == run ]] && ! grep -Fq 'start|run)' scripts/customer.sh; then
    echo 'Legacy startup script detected; using its existing start action (may rebuild).'
    action=start
fi
bash scripts/customer.sh "$action"
bash scripts/customer.sh status
python3 - <<'PY'
import os,time,urllib.request
from pathlib import Path
values={}
for line in Path('config/customer.env').read_text().splitlines():
    if '=' in line and not line.lstrip().startswith('#'):
        key,value=line.split('=',1);values[key.strip()]=value.strip()
port=os.environ.get('SAAS_CUSTOMER_WEB_PORT',values.get('SAAS_CUSTOMER_WEB_PORT','4173'))
if not port.isdigit() or not 1<=int(port)<=65535:raise SystemExit('Invalid local web port')
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):return None
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
origin='http://127.0.0.1:'+port
deadline=time.monotonic()+120
while True:
    try:
        with opener.open(origin+'/xianyu-intelligent-cs/api/auth/capabilities',timeout=3) as response:
            if response.status==200:break
    except Exception:
        if time.monotonic()>=deadline:raise SystemExit('Local API did not become ready; inspect customer container logs')
    time.sleep(2)
print('__XIANYU_URL__='+origin+'/xianyu-intelligent-cs/')
PY
printf '__XIANYU_PROJECT__=%s\n' "$project"
'@

try {
    Get-Command wsl.exe -ErrorAction Stop | Out-Null
    if (Test-Path -LiteralPath $SettingsPath) {
        $Saved = Get-Content -LiteralPath $SettingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not $PSBoundParameters.ContainsKey('Distro')) { $Distro = [string]$Saved.Distro }
        if (-not $PSBoundParameters.ContainsKey('ProjectPath')) { $ProjectPath = [string]$Saved.ProjectPath }
        if (-not $PSBoundParameters.ContainsKey('WslUser')) { $WslUser = [string]$Saved.WslUser }
    }
    $Installed = @(& wsl.exe --list --quiet | ForEach-Object {
        $Name = ([string]$_).Replace([string][char]0, '').Trim().TrimStart([char]0xFEFF)
        if ($Name) { $Name }
    })
    if ($LASTEXITCODE -ne 0) { throw 'Cannot list WSL distributions.' }
    if (-not $Distro) {
        if ($Installed.Count -eq 1) { $Distro = $Installed[0] }
        else { throw ('Specify -Distro. Installed distributions: ' + ($Installed -join ', ')) }
    }
    if ($Installed -notcontains $Distro) { throw "WSL distribution not found: $Distro" }
    $Inventory = (@(& wsl.exe --list --verbose) -join "`n").Replace([string][char]0, '')
    if ($LASTEXITCODE -ne 0) { throw 'Cannot verify WSL2. Run wsl --list --verbose.' }
    $Pattern = '(?m)^\s*\*?\s*' + [regex]::Escape($Distro) + '\s+.*\s+2\s*$'
    if ($Inventory -notmatch $Pattern) { throw 'The selected distribution must use WSL2.' }
    Write-Host "Distribution: $Distro"
    # Root is used only for starting Docker; application files use the deployment user.
    Invoke-WslScript $EngineScript 'root'
    $Action = 'run'
    if ($Build) { $Action = 'start' }
    $Parameters = "project_encoded='$(Encode-Text $ProjectPath)'`naction_encoded='$(Encode-Text $Action)'`n"
    Invoke-WslScript ($Parameters + $ServiceScript) $WslUser
    if (-not $script:ServiceUrl -or -not $script:ResolvedProject) { throw 'Startup did not report a ready service.' }
    $SettingsDir = Split-Path -Parent $SettingsPath
    New-Item -ItemType Directory -Path $SettingsDir -Force | Out-Null
    @{ Distro=$Distro; ProjectPath=$script:ResolvedProject; WslUser=$WslUser } | ConvertTo-Json | Set-Content -LiteralPath $SettingsPath -Encoding UTF8
    try {
        $Response = Invoke-WebRequest -Uri $script:ServiceUrl -UseBasicParsing -TimeoutSec 10
        if ($Response.StatusCode -ne 200) { throw 'Unexpected page response.' }
    } catch {
        throw 'The service is ready inside WSL, but Windows localhost access failed. Check WSL localhost forwarding; do not reinstall or delete data.'
    }
    Write-Host "Service ready: $script:ServiceUrl"
    if (-not $NoBrowser) {
        try { Start-Process $script:ServiceUrl }
        catch { Write-Warning "Service is ready. Open $script:ServiceUrl manually; no default browser could be launched." }
    }
} catch {
    Write-Host ('Startup failed: ' + $_.Exception.Message) -ForegroundColor Red
    $ExitCode = 1
} finally {
    if (-not $NoPause) { [void](Read-Host 'Press Enter to close') }
}
exit $ExitCode
