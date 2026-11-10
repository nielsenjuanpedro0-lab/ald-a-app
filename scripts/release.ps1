# Arma una release de AlDía: compila, firma, y deja listo el latest.json
# que necesita el updater. Después hay que subir a mano el instalador y
# latest.json a una Release de GitHub (ver README.md).
#
# Uso:
#   .\scripts\release.ps1
#
# La clave privada vive en ~/.tauri/aldia-update.key — no se commitea, no
# se comparte. Si se pierde, las versiones viejas no van a poder validar
# futuras actualizaciones y hay que generar una clave nueva (y los
# usuarios tendrían que reinstalar a mano esa vez).

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$keyPath = "$HOME\.tauri\aldia-update.key"

if (-not (Test-Path $keyPath)) {
    Write-Error "No encontré la clave privada en $keyPath. Sin ella no se pueden firmar actualizaciones."
}

$env:TAURI_SIGNING_PRIVATE_KEY_PATH = $keyPath
$env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = ""

$confPath = Join-Path $root "src-tauri\tauri.conf.json"
$conf = Get-Content $confPath -Raw | ConvertFrom-Json
$version = $conf.version
Write-Output "Compilando AlDía v$version ..."

Push-Location $root
try {
    npm run build
} finally {
    Pop-Location
}

$bundleDir = Join-Path $root "src-tauri\target\release\bundle"
$nsis = Get-ChildItem (Join-Path $bundleDir "nsis") -Filter "*-setup.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $nsis) {
    Write-Error "No encontré el instalador NSIS en $bundleDir\nsis. ¿Falló el build?"
}
$sigFile = "$($nsis.FullName).sig"
if (-not (Test-Path $sigFile)) {
    Write-Error "No encontré la firma $sigFile — revisá que TAURI_SIGNING_PRIVATE_KEY_PATH esté bien."
}
$signature = Get-Content $sigFile -Raw

$latest = [ordered]@{
    version  = $version
    notes    = "Ver detalles en el repositorio."
    pub_date = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    platforms = [ordered]@{
        "windows-x86_64" = [ordered]@{
            signature = $signature.Trim()
            url       = "REEMPLAZAR: link de descarga del .exe en la Release de GitHub"
        }
    }
}
$latestPath = Join-Path $bundleDir "latest.json"
$latest | ConvertTo-Json -Depth 5 | Set-Content -Path $latestPath -Encoding utf8

Write-Output ""
Write-Output "Listo. Para publicar la actualización:"
Write-Output "  1. Crea una Release nueva en GitHub con el tag v$version"
Write-Output "  2. Subi como adjuntos:"
Write-Output "       - $($nsis.FullName)"
Write-Output "       - $latestPath   (antes de subirlo, reemplazá 'url' por el link real del .exe ya subido)"
Write-Output "  3. Listo: la próxima vez que alguien abra AlDía, le va a aparecer el cartel de actualizar."
