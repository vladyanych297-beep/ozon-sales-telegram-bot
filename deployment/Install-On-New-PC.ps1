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
    throw 'Запустите PowerShell от имени администратора и повторите установку.'
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
        throw "В $Path не найден параметр $Name. Проверьте версию AvoVPN."
    }
    $updated = [regex]::Replace($content, $pattern, "`${1}$Value")
    Write-Utf8NoBom -Path $Path -Value $updated
}

function Get-PythonLauncher {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) { return $launcher.Source }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) { return $python.Source }
    return $null
}

if (-not (Test-Path -LiteralPath $sourceApp)) {
    throw "Не найдена папка пакета: $sourceApp"
}
if (-not (Test-Path -LiteralPath (Join-Path $sourceApp '.env'))) {
    throw 'В папке app нет заполненного .env. Скопируйте его туда и повторите установку.'
}
if (-not (Test-Path -LiteralPath $vpnExecutable)) {
    throw 'AvoVPN не установлен в C:\Program Files\avoVPN. Сначала установите приложение.'
}
if (-not (Test-Path -LiteralPath $vpnUserConfig)) {
    throw 'Профиль AvoVPN не настроен. Запустите AvoVPN, добавьте ключ, закройте приложение и повторите установку.'
}
if (Get-Process -Name 'avoVPN' -ErrorAction SilentlyContinue) {
    throw 'Закройте AvoVPN через его меню и повторите установку, чтобы настройки сохранились корректно.'
}

$resolvedDestination = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $resolvedDestination) {
    if (-not $Force) {
        throw "Папка уже существует: $resolvedDestination. Для обновления запустите скрипт с параметром -Force."
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
Write-Host 'AvoVPN запущен. Ожидаю подключение сети…'

$deadline = (Get-Date).AddMinutes(5)
do {
    $kernelRunning = [bool](Get-Process -Name 'sing-box' -ErrorAction SilentlyContinue)
    if ($kernelRunning) { break }
    Start-Sleep -Seconds 3
} while ((Get-Date) -lt $deadline)
if (-not $kernelRunning) {
    throw 'AvoVPN не подключился за 5 минут. Проверьте профиль и включите соединение вручную.'
}

$pythonLauncher = Get-PythonLauncher
if (-not $pythonLauncher) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw 'Python не найден, и winget недоступен. Установите Python 3.12 вручную.'
    }
    & $winget.Source install --id Python.Python.3.12 --exact --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Не удалось установить Python: код $LASTEXITCODE" }
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    $pythonLauncher = Get-PythonLauncher
    if (-not $pythonLauncher) { throw 'Python установлен, но команда ещё недоступна. Перезагрузите Windows и повторите установку.' }
}

$virtualPython = Join-Path $resolvedDestination '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $virtualPython)) {
    if ([IO.Path]::GetFileName($pythonLauncher) -ieq 'py.exe') {
        & $pythonLauncher -3.12 -m venv (Join-Path $resolvedDestination '.venv')
    } else {
        & $pythonLauncher -m venv (Join-Path $resolvedDestination '.venv')
    }
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать виртуальное окружение Python.' }
}

& $virtualPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'Не удалось обновить pip.' }
& $virtualPython -m pip install -r (Join-Path $resolvedDestination 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости бота.' }

$launcherScript = Join-Path $resolvedDestination 'deployment\Start-BotStack.ps1'
$powerShellArguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcherScript`" -AppDirectory `"$resolvedDestination`""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $powerShellArguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

Write-Host ''
Write-Host 'Установка завершена.'
Write-Host "Папка бота: $resolvedDestination"
Write-Host "Задача автозапуска: $taskName"
Write-Host 'AvoVPN и бот будут запускаться после входа этого пользователя в Windows.'
