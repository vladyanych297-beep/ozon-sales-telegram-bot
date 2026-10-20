param(
    [Parameter(Mandatory = $true)][string]$PackagePath,
    [Parameter(Mandatory = $true)][string]$AppPath,
    [switch]$SkipRestart
)

$ErrorActionPreference = 'Stop'
$source = [IO.Path]::GetFullPath($PackagePath)
$root = [IO.Path]::GetFullPath($AppPath)
$sourceVersionFile = Join-Path $source 'version.properties'
$versionFile = Join-Path $root 'version.properties'
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $sourceVersionFile)) { throw 'Package version.properties not found' }
if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw "Server folder not found: $root" }
if (-not (Test-Path -LiteralPath $python)) { throw '.venv Python not found' }
$version = (ConvertFrom-StringData (Get-Content -LiteralPath $sourceVersionFile -Raw)).versionName
if (-not $version) { throw 'versionName not found' }

$files = @(
    'version.properties',
    'UPDATE-SERVER.cmd',
    'requirements.txt',
    'requirements-server.txt'
)
$directories = @('ozon_forecast', 'ozon_sales_bot', 'deployment', 'docs', 'updates')
if ($source.TrimEnd('\') -ne $root.TrimEnd('\')) {
    foreach ($relative in $files) {
        $item = Join-Path $source $relative
        if (Test-Path -LiteralPath $item) {
            Copy-Item -LiteralPath $item -Destination (Join-Path $root $relative) -Force
        }
    }
    foreach ($relative in $directories) {
        $item = Join-Path $source $relative
        if (-not (Test-Path -LiteralPath $item -PathType Container)) {
            throw "Package folder not found: $relative"
        }
        Copy-Item -LiteralPath $item -Destination $root -Recurse -Force
    }
}
if (-not (Test-Path -LiteralPath $versionFile)) { throw 'Server version.properties not installed' }
if ($SkipRestart) {
    Write-Output "FILES_INSTALLED=$version"
    exit 0
}

$task = Get-ScheduledTask -TaskName 'Ozon Forecast API' -ErrorAction Stop
if ($task.State -eq 'Running') { Stop-ScheduledTask -InputObject $task }
Start-ScheduledTask -InputObject $task

$deadline = [DateTime]::UtcNow.AddSeconds(45)
do {
    Start-Sleep -Seconds 1
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 5
        if ($health.status -eq 'ok' -and $health.version -eq $version) {
            Write-Output "SERVER_VERSION=$version"
            exit 0
        }
    } catch {
        # The scheduled task may still be starting.
    }
} while ([DateTime]::UtcNow -lt $deadline)

throw "Server did not start as version $version"
