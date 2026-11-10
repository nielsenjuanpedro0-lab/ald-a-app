# AlDía

Avisa a los asegurados por WhatsApp cuando se les rechaza un cupón de
pago, con el comprobante correcto adjunto — sin buscar nada a mano.

## Cómo está armado

- **Interfaz**: Rust ([Tauri](https://tauri.app)) + HTML/CSS/JS en `src/`.
  Es la misma interfaz que se diseñó y aprobó como mockup: sidebar
  redondeado, tarjetas de carga, flujo animado.
- **Backend**: Python en `backend/`, con el mismo código ya probado contra
  datos reales (`backend/core/`): lectura de Excel, recorte de PDFs, cruce
  de teléfonos, envío por WhatsApp Web. Rust lo arranca como un proceso
  aparte y le habla por stdin/stdout en líneas JSON (`backend/server.py`).

  La razón de mantener esta parte en Python y no reescribirla en Rust:
  no hay un Playwright maduro para Rust, y reescribir el recorte de PDF o
  el cruce de Excel a mano tiraría a la basura lo que ya está verificado.

## Requisitos para desarrollar

- [Node.js](https://nodejs.org) 18+
- [Rust](https://www.rust-lang.org/tools/install) (toolchain MSVC en Windows)
- Python 3.12+ con las dependencias de `backend/requirements.txt`
  (`pip install -r backend/requirements.txt`)

## Correr en desarrollo

```
npm install
npm run dev
```

## Compilar el instalador

```
npm run build
```

El resultado queda en `src-tauri/target/release/bundle/`.

## Actualizaciones automáticas

AlDía se fija solo si hay una versión nueva (al abrir) consultando un
archivo `latest.json` publicado en GitHub Releases. Si hay una nueva,
le aparece un cartel al usuario para actualizar con un clic — no hace
falta que vuelvan a descargar el .zip a mano.

Para publicar una versión nueva:

1. Subí la versión en `src-tauri/tauri.conf.json` (campo `"version"`).
2. Corré `.\scripts\release.ps1` — compila, firma, y te arma
   `src-tauri/target/release/bundle/latest.json`.
3. Creá una Release nueva en GitHub (repo `aldia-app`), tag `vX.Y.Z`.
4. Subí como adjuntos el instalador (`...-setup.exe`) y `latest.json`
   (después de pegarle el link real de descarga del instalador en el
   campo `url` — el script te lo recuerda).

La clave privada que firma las actualizaciones vive en
`~/.tauri/aldia-update.key`, fuera del repo. **No se comparte ni se
sube a ningún lado** — si se pierde, hay que generar una nueva y todos
los usuarios en versiones viejas tendrían que reinstalar a mano esa vez.

## Estado actual

Conectado de verdad: elegir los 4 archivos (diálogo nativo de Windows),
guardarlos en la configuración, y los primeros cuatro pasos del flujo
(detectar rechazos, fragmentar PDFs, vincular cupones, cruzar teléfonos)
corren contra el backend Python real.

Todavía en mockup visual, sin conectar: el paso 5 (enviar por WhatsApp,
con la sesión de WhatsApp Web y sus reintentos), y las pantallas de
Configuración, Lista de clientes e Historial.
