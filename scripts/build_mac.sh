#!/usr/bin/env bash
# Arma AlDía para macOS: compila el backend con Nuitka y después la app
# Tauri. El resultado queda en src-tauri/target/release/bundle/ (macos/AlDia.app
# y dmg/AlDia_<versión>_<arquitectura>.dmg).
#
# Uso, desde la raíz del repo:
#   PYTHON=/ruta/a/python3.12 ./scripts/build_mac.sh
#
# El .dmg sale para la arquitectura de la Mac donde se compila (Intel o
# Apple Silicon): el backend de Nuitka no es universal.
#
# Si existe ~/.tauri/aldia-update.key se firman también los artefactos del
# updater (AlDia.app.tar.gz + .sig). Si no, se arma sin ellos.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

export PATH="$HOME/.cargo/bin:$PATH"

"$root/backend/build_mac.sh"

# Se borra el dmg anterior: si el empaquetado falla (ej. "hdiutil: Resource
# busy" porque quedó montado un dmg de AlDía), tauri a veces sale con código 0
# y sin esto quedaba el dmg viejo como si fuera el nuevo.
rm -f "$root"/src-tauri/target/release/bundle/dmg/*.dmg

keyPath="$HOME/.tauri/aldia-update.key"
if [[ -f "$keyPath" ]]; then
  export TAURI_SIGNING_PRIVATE_KEY="$keyPath"
  export TAURI_SIGNING_PRIVATE_KEY_PASSWORD="${TAURI_SIGNING_PRIVATE_KEY_PASSWORD:-}"
  npm run build
else
  echo "Sin clave de updater en $keyPath: se arma sin artefactos de actualización."
  npm run build -- --config '{"bundle":{"createUpdaterArtifacts":false}}'
fi

if ! ls "$root"/src-tauri/target/release/bundle/dmg/*.dmg >/dev/null 2>&1; then
  echo "ERROR: no se generó el dmg. Si hay un AlDia montado en el Finder, expulsalo y volvé a correr." >&2
  exit 1
fi
echo
echo "Listo:"
ls -1 "$root"/src-tauri/target/release/bundle/dmg/*.dmg
