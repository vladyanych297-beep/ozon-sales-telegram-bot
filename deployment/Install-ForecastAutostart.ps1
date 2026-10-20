[CmdletBinding()]
param(
    [string]$ProjectPath = (Split-Path -Parent $PSScriptRoot),
    [string]$NgrokUrl = 'https://quadrant-manger-tidbit.ngrok-free.dev',
    [string]$NgrokConfig = (Join-Path $env:LOCALAPPDATA 'ngrok\ngrok.yml'),
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$rootPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$pythonPath = Join-Path $rootPath '.venv\Scripts\python.exe'
$pythonwPath = Join-Path $rootPath '.venv\Scripts\pythonw.exe'
$ngrokPath = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\ngrok.exe'
foreach ($path in @($pythonPath, $pythonwPath, $ngrokPath, $NgrokConfig,
                   (Join-Path $rootPath '.env.forecast'))) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing file: $path" }
}
$NgrokConfig = (Resolve-Path -LiteralPath $NgrokConfig).Path
Push-Location -LiteralPath $rootPath
try {
    & $pythonPath -m ozon_forecast.windows_start api --app-dir $rootPath --check
    if ($LASTEXITCODE -ne 0) { throw 'API startup validation failed' }
    & $pythonPath -m ozon_forecast.windows_start tunnel --app-dir $rootPath --ngrok-url $NgrokUrl --ngrok-config $NgrokConfig --check
    if ($LASTEXITCODE -ne 0) { throw 'Tunnel startup validation failed' }
    & $ngrokPath config check --config $NgrokConfig
    if ($LASTEXITCODE -ne 0) { throw 'ngrok configuration validation failed' }
} finally { Pop-Location }
if ($CheckOnly) { Write-Output 'PREFLIGHT_OK'; return }

# Never stop an existing manual server, tunnel, Telegram bot, or VPN.
if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Port 8000 is busy. Stop the manual forecast API with Ctrl+C, then run this installer again.'
}
if (Get-CimInstance Win32_Process -Filter "Name = 'ngrok.exe'") {
    # Store processes may report the package path instead of the execution alias.
    throw 'An ngrok process is running. Stop the relevant tunnel manually before installing.'
}
$taskNames = @('Ozon Forecast API', 'Ozon Forecast Tunnel')
foreach ($taskName in $taskNames) {
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) {
        if ($existing.State -eq 'Running') { throw "Task is running: $taskName. Stop it before updating." }
        if ($existing.Actions.Count -ne 1 -or $existing.Actions[0].Execute -ne $pythonwPath -or
            $existing.Actions[0].Arguments -notlike '-m ozon_forecast.windows_start *') {
            throw "Unrelated task has the same name: $taskName. Nothing was overwritten."
        }
    }
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$apiArgs = '-m ozon_forecast.windows_start api --app-dir "{0}"' -f $rootPath
$tunnelArgs = '-m ozon_forecast.windows_start tunnel --app-dir "{0}" --ngrok-url "{1}" --ngrok-config "{2}"' -f $rootPath, $NgrokUrl, $NgrokConfig
$definitions = @(
    @{ Name = $taskNames[0]; Arguments = $apiArgs },
    @{ Name = $taskNames[1]; Arguments = $tunnelArgs }
)
foreach ($definition in $definitions) {
    $action = New-ScheduledTaskAction -Execute $pythonwPath -Argument $definition.Arguments -WorkingDirectory $rootPath
    Register-ScheduledTask -TaskName $definition.Name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Ozon forecast: automatic start after Windows sign-in; no changes to the bot or VPN.' -Force | Out-Null
}
foreach ($taskName in $taskNames) { Start-ScheduledTask -TaskName $taskName }
Write-Output 'AUTOSTART_INSTALLED'
Get-ScheduledTask -TaskName $taskNames | Select-Object TaskName, State
Write-Output 'Starts after this Windows user signs in. Keep automatic Windows sign-in and existing VPN startup unchanged.'
