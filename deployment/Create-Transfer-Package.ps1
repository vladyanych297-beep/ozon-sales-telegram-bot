[CmdletBinding()]
param(
    [string]$OutputPath = (Join-Path $env:USERPROFILE 'Desktop\ozon-sales-bot-transfer.zip'),
    [switch]$IncludeSecrets
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentFile = Join-Path $projectRoot '.env'
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("ozon-bot-transfer-" + [guid]::NewGuid().ToString('N'))
$packageRoot = Join-Path $temporaryRoot 'ozon-sales-bot-transfer'
$appRoot = Join-Path $packageRoot 'app'

if ($IncludeSecrets -and -not (Test-Path -LiteralPath $environmentFile)) {
    throw 'The .env file was not found. A configured package cannot be created.'
}

try {
    New-Item -ItemType Directory -Path $appRoot -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $packageRoot 'deployment') -Force | Out-Null

    $packageModule = Join-Path $appRoot 'ozon_sales_bot'
    New-Item -ItemType Directory -Path $packageModule -Force | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $projectRoot 'ozon_sales_bot') -Filter '*.py' -File | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $packageModule
    }
    Copy-Item -LiteralPath (Join-Path $projectRoot 'requirements.txt') -Destination $appRoot
    Copy-Item -LiteralPath (Join-Path $projectRoot 'pyproject.toml') -Destination $appRoot
    Copy-Item -LiteralPath (Join-Path $projectRoot '.env.example') -Destination $appRoot
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Install-On-New-PC.ps1') -Destination (Join-Path $packageRoot 'deployment')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Start-BotStack.ps1') -Destination (Join-Path $packageRoot 'deployment')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README-TRANSFER.txt') -Destination $packageRoot

    if ($IncludeSecrets) {
        Copy-Item -LiteralPath $environmentFile -Destination (Join-Path $appRoot '.env')
        Set-Content -LiteralPath (Join-Path $packageRoot 'PACKAGE-CONTAINS-SECRETS.txt') -Value @(
            'The archive contains .env with Telegram and Ozon tokens.'
            'Protect the archive like a password and delete it after installation.'
        ) -Encoding utf8
    }

    $resolvedOutput = [IO.Path]::GetFullPath($OutputPath)
    $outputDirectory = Split-Path -Parent $resolvedOutput
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
    if (Test-Path -LiteralPath $resolvedOutput) {
        Remove-Item -LiteralPath $resolvedOutput -Force
    }
    Compress-Archive -LiteralPath $packageRoot -DestinationPath $resolvedOutput -CompressionLevel Optimal

    Write-Host "Package created: $resolvedOutput"
    if (-not $IncludeSecrets) {
        Write-Warning 'Secrets are not included. Put the configured .env file in the package app directory before installation.'
    } else {
        Write-Warning 'The archive contains secrets. Transfer it securely and delete it after installation.'
    }
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        $resolvedTemporaryRoot = [IO.Path]::GetFullPath($temporaryRoot)
        $resolvedSystemTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
        if ($resolvedTemporaryRoot.StartsWith($resolvedSystemTemp, [StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolvedTemporaryRoot -Recurse -Force
        }
    }
}
