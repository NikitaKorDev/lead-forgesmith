<#
.SYNOPSIS
    Run one Forgesmith pipeline stage as a one-shot compose container.

.DESCRIPTION
    Task Scheduler calls this script. It enforces a per-stage single-flight
    mutex, appends output to state\logs\<stage>-YYYYMMDD.log, writes
    state\last-<stage>.json, and exits with docker's code.

    Exit codes:
      0   stage completed (including window closed / cap / kill switch / budget)
      1   real failure
      2   refused (approval/confirmation)
      75  another tick owns this stage (skip silently)

    There is deliberately no "approve" stage: approval is always human.

.EXAMPLE
    powershell -NoProfile -File ops\run.ps1 send --limit 3 --max-runtime 720
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet(
        'source-shopify',
        'source-olx',
        'enrich',
        'compose',
        'send',
        'backup',
        'digest'
    )]
    [string]$Stage,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$StageArgs = @()
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$stages = @{
    'source-shopify' = @{ Profile = 'app';     Service = 'app';         Command = @('source', 'shopify') }
    'source-olx'     = @{ Profile = 'browser'; Service = 'app-browser'; Command = @('source', 'olx') }
    'enrich'         = @{ Profile = 'browser'; Service = 'app-browser'; Command = @('enrich') }
    'compose'        = @{ Profile = 'app';     Service = 'app';         Command = @('compose') }
    'send'           = @{ Profile = 'live';    Service = 'app-live';    Command = @('send', '--live', '--yes') }
    'backup'         = @{ Profile = 'app';     Service = 'app';         Command = @('backup', '--keep', '22') }
    'digest'         = @{ Profile = 'app';     Service = 'app';         Command = @('status', '--json') }
}

$entry = $stages[$Stage]

$stateDir = Join-Path $root 'state'
$logDir = Join-Path $stateDir 'logs'
New-Item -ItemType Directory -Force -Path $stateDir, $logDir | Out-Null

$lastFile = Join-Path $stateDir ("last-{0}.json" -f $Stage)
$logFile = Join-Path $logDir ("{0}-{1}.log" -f $Stage, (Get-Date -Format 'yyyyMMdd'))

# Separate mutex per stage: enrich and send may overlap (different containers),
# but two ticks of the same stage never do.
$mutex = [Threading.Mutex]::new($false, "Global\forgesmith-$Stage")
$acquired = $false
try {
    $acquired = $mutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    # A previous tick was killed without releasing; this tick now owns it.
    $acquired = $true
}
if (-not $acquired) {
    @{
        stage       = $Stage
        exit        = 75
        at          = (Get-Date).ToString('o')
        stop_reason = 'busy'
    } | ConvertTo-Json -Compress | Set-Content -Path $lastFile -Encoding UTF8
    Write-Host "[$Stage] busy: another tick is already running"
    exit 75
}

$hostNow = (Get-Date).ToUniversalTime().ToString('o')
$dockerArgs = @(
    'compose',
    '--profile', $entry.Profile,
    'run', '--rm', '-T',
    '-e', "FORGESMITH_HOST_NOW=$hostNow",
    $entry.Service,
    'forgesmith'
) + $entry.Command + $StageArgs

try {
    "[$(Get-Date -Format o)] [$Stage] docker $($dockerArgs -join ' ')" | Add-Content -Path $logFile
    $output = & docker @dockerArgs *>&1
    $code = $LASTEXITCODE
    $output | Out-File -FilePath $logFile -Append -Encoding utf8

    # Stop reason lives in the run ledger. Only send needs it for alerting;
    # one extra short container keeps the shell parser trivial.
    $stopReason = $null
    if ($Stage -eq 'send' -and $code -eq 0) {
        try {
            $raw = & docker compose --profile app run --rm -T app forgesmith status --json 2>$null
            $payload = $raw | ConvertFrom-Json
            $stopReason = $payload.runs.send.stop_reason
        } catch {
            $stopReason = $null
        }
    }

    @{
        stage       = $Stage
        exit        = $code
        at          = (Get-Date).ToString('o')
        stop_reason = $stopReason
    } | ConvertTo-Json -Compress | Set-Content -Path $lastFile -Encoding UTF8

    if ($Stage -eq 'digest' -and $code -eq 0) {
        $output | Set-Content -Path (Join-Path $stateDir 'digest.txt') -Encoding UTF8
    }

    $badReasons = @(
        'transport_error', 'archive_failed', 'relay_unavailable',
        'policy_rejection_streak', 'aborted'
    )
    $needsAlert = ($code -in 1, 2) -or ($code -eq 0 -and $badReasons -contains $stopReason)
    if ($needsAlert) {
        if ($null -eq $stopReason) { $stopReason = '' }
        & (Join-Path $PSScriptRoot 'alert.ps1') -Stage $Stage -ExitCode $code -LogPath $logFile -StopReason $stopReason
    }

    exit $code
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
