param(
    [string]$SiteUrl = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$javaHome = Join-Path $root ".tools\jdk17\jdk-17.0.19+10"
$androidHome = Join-Path $root ".tools\android-sdk"
$gradle = Join-Path $root ".tools\gradle\gradle-8.9\bin\gradle.bat"
$project = Join-Path $root "android-app"
$apkSource = Join-Path $project "app\build\outputs\apk\debug\app-debug.apk"
$apkTarget = Join-Path $root "TrustDelivery.apk"

if (-not (Test-Path $gradle) -or -not (Test-Path $javaHome) -or -not (Test-Path $androidHome)) {
    throw "La chaîne Android locale est incomplète dans .tools."
}

$env:JAVA_HOME = $javaHome
$env:ANDROID_HOME = $androidHome
$env:ANDROID_SDK_ROOT = $androidHome
$env:GRADLE_USER_HOME = Join-Path $root ".tools\gradle-home"

$arguments = @(
    "-p", $project,
    ":app:assembleDebug",
    "--no-daemon",
    "--max-workers=1",
    "--no-watch-fs"
)
if ($SiteUrl) {
    $arguments += "-PTRUSTDELIVERY_URL=$SiteUrl"
}

& $gradle @arguments
if ($LASTEXITCODE -ne 0) {
    throw "La compilation Android a échoué."
}

Copy-Item -LiteralPath $apkSource -Destination $apkTarget -Force
& (Join-Path $androidHome "build-tools\35.0.0\apksigner.bat") verify --verbose $apkTarget
if ($LASTEXITCODE -ne 0) {
    throw "La vérification de signature de l'APK a échoué."
}

Write-Host "APK prêt : $apkTarget" -ForegroundColor Green
