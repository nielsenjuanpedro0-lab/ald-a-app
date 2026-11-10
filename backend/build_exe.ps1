# Congela server.py (y todo lo que necesita: pandas, openpyxl, pypdf,
# pdfplumber, playwright) en un .exe propio, para que la PC donde se
# instale AlDía no necesite tener Python instalado.
#
# Usa Nuitka, no PyInstaller: el mismo backend armado con PyInstaller hacía
# que Windows Defender lo marcara como amenaza (falso positivo heurístico
# "Behavior:Win32/DefenseEvasion.A!ml" — el combo de ejecutable empaquetado
# + Chromium headless con el truco anti-detección de WhatsApp activa esa
# alarma). Con Nuitka, que compila a código real en vez de empaquetar el
# intérprete, no pasa.
#
# Corre esto de nuevo cada vez que cambie algo en backend/ antes de armar
# un instalador con `npm run tauri build` (tauri.conf.json ya apunta a
# backend/dist_nuitka/server.dist como recurso empaquetado).
#
# Requiere: pip install nuitka (una vez, en este mismo Python). También
# necesita un compilador de C — si ya instalaste Visual Studio Build Tools
# para compilar el lado de Rust, Nuitka lo reusa solo.

$driverPath = python -c "import playwright, os; print(os.path.join(os.path.dirname(playwright.__file__), 'driver'))"

python -m nuitka --standalone --assume-yes-for-downloads `
  --output-dir=dist_nuitka `
  --include-package=core `
  --include-package=playwright `
  --include-package=pdfplumber `
  --include-data-dir="$driverPath=playwright/driver" `
  --nofollow-import-to=matplotlib,PySide6,shiboken6,scipy,tkinter,PyQt5,PyQt6 `
  server.py
