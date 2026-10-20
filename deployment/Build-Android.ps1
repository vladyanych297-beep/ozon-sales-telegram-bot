param([string]$BuildPath = (Join-Path $env:LOCALAPPDATA 'OzonPlanBuild'))

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$androidRoot = Join-Path $projectRoot 'android'
$defaultSdk = Join-Path $env:LOCALAPPDATA 'Android\Sdk'
if (-not $env:ANDROID_HOME -and (Test-Path -LiteralPath $defaultSdk)) {
    $env:ANDROID_HOME = $defaultSdk
}
if (-not $env:ANDROID_HOME -or -not (Test-Path -LiteralPath $env:ANDROID_HOME)) {
    throw 'Android SDK не найден. Укажите ANDROID_HOME.'
}
$versionData = ConvertFrom-StringData (Get-Content -LiteralPath (Join-Path $projectRoot 'version.properties') -Raw)
$versionName = $versionData.versionName
if (-not $versionName) { throw 'versionName отсутствует в version.properties' }
# Gradle's JVM unit-test classpath can fail on Windows paths containing Cyrillic.
# Copy source to a separate ASCII path; never move/delete the user's checkout.
if ($BuildPath -match '[^\x00-\x7F]') { throw 'BuildPath должен содержать только ASCII-символы.' }
$buildRoot = [IO.Path]::GetFullPath($BuildPath)
$buildSource = Join-Path $buildRoot ('source-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $buildSource -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRoot 'version.properties') -Destination (Join-Path $buildSource 'version.properties')
Get-ChildItem -LiteralPath $androidRoot -File -Recurse | ForEach-Object {
    $relative = $_.FullName.Substring($androidRoot.Length + 1)
    $segments = $relative -split '[\\/]'
    if (-not ($segments -contains 'build' -or $segments -contains '.gradle' -or $segments -contains '.kotlin') -and $_.Name -ne 'local.properties') {
        $destination = Join-Path $buildSource $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
    }
}
& (Join-Path $buildSource 'gradlew.bat') -p $buildSource assembleDebug assembleRelease testDebugUnitTest lintDebug --console=plain
if ($LASTEXITCODE -ne 0) { throw "Android build failed: $LASTEXITCODE" }
$out = Join-Path $projectRoot 'outputs/android'
New-Item -ItemType Directory -Path $out -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $buildSource 'app/build/outputs/apk/debug/app-debug.apk') -Destination (Join-Path $out "ozon-plan-$versionName-debug.apk") -Force
Copy-Item -LiteralPath (Join-Path $buildSource 'app/build/outputs/apk/release/app-release-unsigned.apk') -Destination (Join-Path $out "ozon-plan-$versionName-release-unsigned.apk") -Force
Write-Output "APK: $out"
