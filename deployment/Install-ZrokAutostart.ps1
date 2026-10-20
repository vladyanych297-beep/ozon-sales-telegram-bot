[CmdletBinding()]
param(
    [string]$ProjectPath = (Split-Path -Parent $PSScriptRoot),
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9]{4,32}$')]
    [string]$ShareName,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$rootPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$pythonPath = Join-Path $rootPath '.venv\Scripts\python.exe'
$pythonwPath = Join-Path $rootPath '.venv\Scripts\pythonw.exe'
$zrokPath = Join-Path $rootPath 'tools\zrok-1.1.11\zrok.exe'
foreach ($path in @($pythonPath, $pythonwPath, $zrokPath,
                   (Join-Path $rootPath '.env.forecast'))) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing file: $path" }
}
Push-Location -LiteralPath $rootPath
try {
    & $pythonPath -m ozon_forecast.windows_start zrok-tunnel --app-dir $rootPath --zrok-share $ShareName --check
    if ($LASTEXITCODE -ne 0) { throw 'zrok startup validation failed' }
} finally { Pop-Location }

# Only replace our tunnel task; leave the API, Telegram bot and VPN running.
$taskName = 'Ozon Forecast Tunnel'
$existing = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
if ($existing) {
    $expectedOld = '-m ozon_forecast.windows_start tunnel --app-dir "{0}" *' -f $rootPath
    $expectedNew = '-m ozon_forecast.windows_start zrok-tunnel --app-dir "{0}" *' -f $rootPath
    if ($existing.Actions.Count -ne 1 -or $existing.Actions[0].Execute -ne $pythonwPath -or
        ($existing.Actions[0].Arguments -notlike $expectedOld -and
         $existing.Actions[0].Arguments -notlike $expectedNew)) {
        throw 'An unrelated task uses the tunnel task name. Nothing was overwritten.'
    }
}
if ($CheckOnly) { Write-Output 'ZROK_PREFLIGHT_OK'; return }
if ($existing -and $existing.State -eq 'Running') {
    throw 'The existing tunnel task is running. Stop it before updating.'
}
if (Get-CimInstance Win32_Process -Filter "Name = 'zrok.exe'") {
    throw 'zrok is already running. Close the manual tunnel window before installing.'
}
if (Get-CimInstance Win32_Process -Filter "Name = 'ngrok.exe'") {
    throw 'ngrok is still running. Stop the relevant tunnel before installing.'
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$trigger.Delay = 'PT60S'
# No restart-on-failure: a provider denial or revoked account must not be retried.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$arguments = '-m ozon_forecast.windows_start zrok-tunnel --app-dir "{0}" --zrok-share {1}' -f $rootPath, $ShareName
$action = New-ScheduledTaskAction -Execute $pythonwPath -Argument $arguments -WorkingDirectory $rootPath

if ($existing) {
    $backupDir = Join-Path $rootPath 'backups'
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    $backupPath = Join-Path $backupDir ('forecast-tunnel-' + (Get-Date -Format 'yyyyMMdd-HHmmss-ffff') + '.xml')
    $xml = Export-ScheduledTask -TaskName $taskName -TaskPath '\'
    $xml = $xml -replace '(?i)encoding="UTF-16"', 'encoding="UTF-8"'
    [IO.File]::WriteAllText($backupPath, $xml, [Text.UTF8Encoding]::new($false))
}

Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Ozon reserved zrok tunnel: hidden start 60 seconds after sign-in. Does not alter API, bot or VPN.' -Force | Out-Null
$installed = Get-ScheduledTask -TaskName $taskName -TaskPath '\'
if ($installed.Actions.Count -ne 1 -or $installed.Actions[0].Execute -ne $pythonwPath -or
    $installed.Actions[0].Arguments -ne $arguments) {
    throw 'Installed task action does not match the expected action. Check manually.'
}
Start-ScheduledTask -TaskName $taskName -TaskPath '\'
Write-Output 'ZROK_TASK_INSTALLED'
Get-ScheduledTask -TaskName $taskName -TaskPath '\' | Select-Object TaskName, State
Write-Output 'Installation is not a connectivity check. Verify public /health and reboot separately.'
