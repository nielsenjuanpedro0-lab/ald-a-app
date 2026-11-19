#!/usr/bin/env bash
# Equivalente macOS de build_exe.ps1: congela server.py con Nuitka para que
# la Mac donde se instale AlDía no necesite tener Python instalado.
# Deja el resultado en backend/dist_nuitka/server.dist/server.bin, que es la
# carpeta que tauri.conf.json empaqueta como recurso.
#
# Uso (desde la raíz del repo o desde backend/):
#   PYTHON=/ruta/a/python3.12 backend/build_mac.sh
#
# Requiere: Python 3.12 con `pip install -r requirements.txt nuitka`, y las
# Command Line Tools de Xcode (clang) para compilar.
set -euo pipefail
cd "$(dirname "$0")"

# Por defecto usa el entorno .venv-mac de la raíz del repo si existe.
if [[ -z "${PYTHON:-}" && -x ../.venv-mac/bin/python ]]; then
  PYTHON="$(cd .. && pwd)/.venv-mac/bin/python"
fi
PY="${PYTHON:-python3}"
"$PY" -c "import nuitka, playwright" 2>/dev/null || {
  echo "El Python '$PY' no tiene nuitka/playwright. Pasá PYTHON=/ruta/a/python3.12 con requirements.txt instalado." >&2
  exit 1
}

rm -rf dist_nuitka/server.dist

"$PY" -m nuitka --standalone --assume-yes-for-downloads \
  --output-dir=dist_nuitka \
  --include-package=core \
  --include-package=playwright \
  --include-package=pdfplumber \
  --nofollow-import-to=matplotlib,PySide6,shiboken6,scipy,tkinter,PyQt5,PyQt6 \
  --nofollow-import-to=pyautogui,pymsgbox,pyscreeze,mouseinfo,pygetwindow,objc,Foundation,AppKit,Quartz,PyObjCTools \
  server.py

# pyautogui (motor viejo de respaldo, ver core/whatsapp.py) arrastra pyobjc,
# que Nuitka solo acepta en --mode=app; en el empaquetado siempre está
# Playwright, así que el respaldo no hace falta y se deja afuera.
#
# El driver de Playwright (node + package/) lo incluye solo el plugin de
# Nuitka para playwright: pasarlo además con --include-data-dir, como en
# build_exe.ps1, en macOS choca ("conflicts with exe") y aborta el build.
#
# Por las dudas se asegura el bit de ejecución: el node que trae el
# driver de Playwright tiene que poder ejecutarse o WhatsApp no arranca.
chmod +x dist_nuitka/server.dist/playwright/driver/node
chmod +x dist_nuitka/server.dist/server.bin
echo "Backend listo: $(pwd)/dist_nuitka/server.dist/server.bin"
