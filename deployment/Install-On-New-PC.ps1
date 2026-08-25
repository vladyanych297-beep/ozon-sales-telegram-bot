[CmdletBinding()]
param(
    [string]$Destination = (Join-Path $env:LOCALAPPDATA 'OzonSalesBot'),
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$sourceRoot = Split-Path -Parent $PSScriptRoot
$sourceApp = Join-Path $sourceRoot 'app'
$vpnExecutable = 'C:\Program Files\avoVPN\avoVPN.exe'
$vpnRoot = Join-Path $env:APPDATA 'avoVPN'
$vpnUserConfig = Join-Path $vpnRoot 'data\user.yaml'
$vpnBuildConfig = Join-Path $vpnRoot 'build\bin\data\user.yaml'
$vpnAutostartFlag = Join-Path $vpnRoot 'autostart_enabled'
$taskName = 'Ozon Sales Telegram Bot'

$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentPrincipal = [Security.Principal.WindowsPrincipal]::new($currentIdentity)
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run PowerShell as Administrator and start the installer again.'
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Value)
    $encoding = [Text.UTF8Encoding]::new($false)
    [IO.File]::WriteAllText($Path, $Value, $encoding)
}

function Set-YamlScalar {
    param([string]$Path, [string]$Name, [string]$Value)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $content = [IO.File]::ReadAllText($Path)
    $pattern = "(?m)^(\s*" + [regex]::Escape($Name) + "\s*:\s*).*$"
    if (-not [regex]::IsMatch($content, $pattern)) {
        throw "Setting $Name was not found in $Path. Check the AvoVPN version."
    }
    $updated = [regex]::Replace($content, $pattern, "`${1}$Value")
    Write-Utf8NoBom -Path $Path -Value $updated
}

function Get-PythonLauncher {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher -and $launcher.Source -notlike '*\WindowsApps\*') { return $launcher.Source }

    $knownPythonPaths = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:ProgramFiles 'Python312\python.exe')
    )
    foreach ($knownPythonPath in $knownPythonPaths) {
        if (Test-Path -LiteralPath $knownPythonPath) { return $knownPythonPath }
    }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python -and $python.Source -notlike '*\WindowsApps\*') { return $python.Source }
    return $null
}

if (-not (Test-Path -LiteralPath $sourceApp)) {
    throw "Package app directory was not found: $sourceApp"
}
if (-not (Test-Path -LiteralPath (Join-Path $sourceApp '.env'))) {
    throw 'The app directory has no configured .env file. Copy it there and run the installer again.'
}
if (-not (Test-Path -LiteralPath $vpnExecutable)) {
    throw 'AvoVPN is not installed in C:\Program Files\avoVPN. Install it first.'
}
if (-not (Test-Path -LiteralPath $vpnUserConfig)) {
    throw 'The AvoVPN profile is not configured. Configure it, exit AvoVPN, and run the installer again.'
}
$runningVpn = Get-Process -Name 'avoVPN' -ErrorAction SilentlyContinue
if ($runningVpn) {
    Write-Host 'AvoVPN is still running in the background. Closing it before setup.'
    $runningVpn | Stop-Process -Force
    $runningVpn | Wait-Process -Timeout 10 -ErrorAction SilentlyContinue
    if (Get-Process -Name 'avoVPN' -ErrorAction SilentlyContinue) {
        throw 'AvoVPN could not be closed automatically. Restart Windows and run the installer again.'
    }
}

$resolvedDestination = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $resolvedDestination) {
    if (-not $Force) {
        throw "Destination already exists: $resolvedDestination. Run the installer with -Force to update it."
    }
} else {
    New-Item -ItemType Directory -Path $resolvedDestination -Force | Out-Null
}

Get-ChildItem -LiteralPath $sourceApp -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $resolvedDestination -Recurse -Force
}
New-Item -ItemType Directory -Path (Join-Path $resolvedDestination 'deployment') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Start-BotStack.ps1') -Destination (Join-Path $resolvedDestination 'deployment') -Force

Set-YamlScalar -Path $vpnUserConfig -Name 'autoStartKernel' -Value 'true'
Set-YamlScalar -Path $vpnBuildConfig -Name 'starthidden' -Value 'true'
Write-Utf8NoBom -Path $vpnAutostartFlag -Value 'true'

Start-Process -FilePath $vpnExecutable -WindowStyle Hidden
Write-Host 'AvoVPN started. Waiting for the VPN connection.'

$deadline = (Get-Date).AddMinutes(5)
do {
    $kernelRunning = [bool](Get-Process -Name 'sing-box' -ErrorAction SilentlyContinue)
    if ($kernelRunning) { break }
    Start-Sleep -Seconds 3
} while ((Get-Date) -lt $deadline)
if (-not $kernelRunning) {
    throw 'AvoVPN did not connect within 5 minutes. Check the profile and connect manually.'
}

$pythonLauncher = Get-PythonLauncher
if (-not $pythonLauncher) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw 'Python was not found and winget is unavailable. Install Python 3.12 manually.'
    }
    & $winget.Source install --id Python.Python.3.12 --exact --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Python installation failed with exit code $LASTEXITCODE" }
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    $pythonLauncher = Get-PythonLauncher
    if (-not $pythonLauncher) { throw 'Python was installed but is not available yet. Restart Windows and run the installer again.' }
}

$virtualPython = Join-Path $resolvedDestination '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $virtualPython)) {
    if ([IO.Path]::GetFileName($pythonLauncher) -ieq 'py.exe') {
        & $pythonLauncher -3.12 -m venv (Join-Path $resolvedDestination '.venv')
    } else {
        & $pythonLauncher -m venv (Join-Path $resolvedDestination '.venv')
    }
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the Python virtual environment.' }
}

& $virtualPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'Failed to upgrade pip.' }
& $virtualPython -m pip install -r (Join-Path $resolvedDestination 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Failed to install bot dependencies.' }

$launcherScript = Join-Path $resolvedDestination 'deployment\Start-BotStack.ps1'
$powerShellArguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcherScript`" -AppDirectory `"$resolvedDestination`""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $powerShellArguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

Write-Host ''
Write-Host 'Installation completed.'
Write-Host "Bot directory: $resolvedDestination"
Write-Host "Startup task: $taskName"
Write-Host 'AvoVPN and the bot will start after this Windows user signs in.'
