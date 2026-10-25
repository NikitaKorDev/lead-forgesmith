<#
.SYNOPSIS
    Best-effort alert for a failed or degraded Forgesmith stage.

.DESCRIPTION
    Called by run.ps1. Never throws and never changes the stage exit code.
    Configure one of:
      FORGESMITH_NTFY_URL       e.g. https://ntfy.sh/your-topic
      FORGESMITH_ALERT_WEBHOOK  generic JSON webhook
    With neither set, alerts append to state\alerts.log.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Stage,
    [Parameter(Mandatory = $true)][int]$ExitCode,
    [string]$LogPath = '',
    [string]$StopReason = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$stateDir = Join-Path $root 'state'
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null

$subject = "forgesmith: $Stage exit $ExitCode"
$detail = "stage=$Stage exit=$ExitCode stop=$StopReason log=$LogPath"
$message = "$subject`n$detail"

try {
    if ($env:FORGESMITH_NTFY_URL) {
        Invoke-RestMethod -Method Post -Uri $env:FORGESMITH_NTFY_URL `
            -Headers @{ Title = $subject; Tags = 'warning' } `
            -Body $message | Out-Null
    }
    elseif ($env:FORGESMITH_ALERT_WEBHOOK) {
        $payload = @{
            text   = $message
            stage  = $Stage
            exit   = $ExitCode
            stop   = $StopReason
            log    = $LogPath
        } | ConvertTo-Json -Compress
        Invoke-RestMethod -Method Post -Uri $env:FORGESMITH_ALERT_WEBHOOK `
            -ContentType 'application/json' -Body $payload | Out-Null
    }
    else {
        Add-Content -Path (Join-Path $stateDir 'alerts.log') `
            -Value "[$(Get-Date -Format o)] $detail" -Encoding UTF8
    }
}
catch {
    try {
        Add-Content -Path (Join-Path $stateDir 'alerts.log') `
            -Value "[$(Get-Date -Format o)] alert delivery failed: $($_.Exception.Message)" `
            -Encoding UTF8
    } catch {
        # Never let alerting break the pipeline.
    }
}

exit 0
