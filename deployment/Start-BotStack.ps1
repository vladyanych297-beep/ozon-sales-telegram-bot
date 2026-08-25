[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$AppDirectory
)

$ErrorActionPreference = 'Stop'
$vpnExecutable = 'C:\Program Files\avoVPN\avoVPN.exe'
$pythonExecutable = Join-Path $AppDirectory '.venv\Scripts\python.exe'
$logsDirectory = Join-Path $AppDirectory 'logs'
$launcherLog = Join-Path $logsDirectory 'launcher.log'

New-Item -ItemType Directory -Path $logsDirectory -Force | Out-Null

function Write-LauncherLog {
    param([string]$Message)
    $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath $launcherLog -Value $line -Encoding utf8
}

function Test-TcpEndpoint {
    param([string]$HostName, [int]$Port = 443, [int]$TimeoutMilliseconds = 3000)
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $connection = $client.ConnectAsync($HostName, $Port)
        if (-not $connection.Wait($TimeoutMilliseconds)) { return $false }
        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Ensure-VpnStarted {
    if (-not (Get-Process -Name 'avoVPN' -ErrorAction SilentlyContinue)) {
        Write-LauncherLog 'Starting AvoVPN.'
        Start-Process -FilePath $vpnExecutable -WindowStyle Hidden
    }
}

function Wait-ForVpn {
    Write-LauncherLog 'Waiting for AvoVPN and API connectivity.'
    while ($true) {
        Ensure-VpnStarted
        $kernelRunning = [bool](Get-Process -Name 'sing-box' -ErrorAction SilentlyContinue)
        $telegramAvailable = $kernelRunning -and (Test-TcpEndpoint -HostName 'api.telegram.org')
        $ozonAvailable = $kernelRunning -and (Test-TcpEndpoint -HostName 'api-seller.ozon.ru')
        if ($telegramAvailable -and $ozonAvailable) {
            Write-LauncherLog 'VPN is connected; Telegram and Ozon are reachable.'
            return
        }
        Start-Sleep -Seconds 5
    }
}

if (-not (Test-Path -LiteralPath $vpnExecutable)) {
    Write-LauncherLog "AvoVPN was not found: $vpnExecutable"
    exit 10
}
if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    Write-LauncherLog "Bot Python was not found: $pythonExecutable"
    exit 11
}
if (-not (Test-Path -LiteralPath (Join-Path $AppDirectory '.env'))) {
    Write-LauncherLog 'The .env file was not found.'
    exit 12
}

while ($true) {
    Wait-ForVpn

    $existingBot = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -like 'python*' -and $_.CommandLine -like '*ozon_sales_bot.bot*'
    } | Select-Object -First 1

    if ($existingBot) {
        Write-LauncherLog "The bot is already running, PID $($existingBot.ProcessId)."
        while (Get-Process -Id $existingBot.ProcessId -ErrorAction SilentlyContinue) {
            Ensure-VpnStarted
            Start-Sleep -Seconds 15
        }
        Write-LauncherLog 'The existing bot process stopped.'
    } else {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $stdoutLog = Join-Path $logsDirectory "bot-$stamp.out.log"
        $stderrLog = Join-Path $logsDirectory "bot-$stamp.err.log"
        Write-LauncherLog 'Starting the Telegram bot.'
        $bot = Start-Process -FilePath $pythonExecutable -ArgumentList '-m', 'ozon_sales_bot.bot' -WorkingDirectory $AppDirectory -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru
        $bot.WaitForExit()
        Write-LauncherLog "The bot exited with code $($bot.ExitCode). Restarting in 10 seconds."
    }

    Start-Sleep -Seconds 10
}
