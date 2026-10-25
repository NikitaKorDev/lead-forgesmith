<#
.SYNOPSIS
    Import the committed Task Scheduler definitions under Forgesmith\<stage>.

.DESCRIPTION
    Run once as the logged-on user (no elevation needed with InteractiveToken).
    /F replaces an existing definition, so rerunning after an edit is safe.
    There is intentionally no approve task: approval is manual only.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File ops\install-tasks.ps1
    schtasks /Query /TN "Forgesmith\send" /V /FO LIST
#>
[CmdletBinding()]
param(
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$taskDir = Join-Path $PSScriptRoot 'tasks'

$files = Get-ChildItem -Path $taskDir -Filter 'forgesmith-*.xml' | Sort-Object Name
if (-not $files) {
    throw "No task definitions found in $taskDir"
}

foreach ($file in $files) {
    $stage = $file.BaseName -replace '^forgesmith-', ''
    if ($stage -eq 'approve') {
        throw 'Refusing to schedule approve: approval is human-only.'
    }
    $taskName = "Forgesmith\$stage"
    if ($WhatIf) {
        Write-Host "would import $($file.FullName) as $taskName"
        continue
    }
    & schtasks.exe /Create /XML $file.FullName /TN $taskName /F | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks failed for $taskName (exit $LASTEXITCODE)"
    }
    Write-Host "imported $taskName"
}

if (-not $WhatIf) {
    Write-Host ''
    Write-Host 'Scheduled tasks:'
    & schtasks.exe /Query /FO TABLE 2>$null |
        Select-String -Pattern 'Forgesmith' |
        ForEach-Object { $_.Line }
}
