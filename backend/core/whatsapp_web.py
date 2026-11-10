"""Envío por WhatsApp Web manejando el navegador desde código (Playwright).

Reemplaza al motor viejo de pyautogui. Dos diferencias que importan:

1. **Una sola sesión.** Antes se abría `web.whatsapp.com/send?phone=...` por
   cada cliente, y eso rebootea WhatsApp Web entero (~55s por cliente). Acá se
   abre una vez y después se busca cada chat dentro de la app, sin recargar.

2. **Verificación real.** El motor viejo apretaba Enter a ciegas y daba por
   enviado si Python no explotaba, así que un mensaje que quedaba en borrador
   figuraba como enviado. Acá se espera a que el cuadro de mensaje exista de
   verdad y, después de enviar, se confirma que apareció la burbuja saliente.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import pandas as pd


def _cache_dir_por_defecto() -> Path:
    """La misma carpeta que Playwright usa de toda la vida, por sistema.

    Replica a mano `computeDefaultCacheDirectory()` del driver de Playwright
    (playwright/driver/package/lib/coreBundle.js).
    """
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches"
    return Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))


# Playwright, cuando corre empaquetado con PyInstaller/Nuitka, cambia solo
# (ver playwright/_impl/_transport.py, "For pyinstaller and Nuitka") dónde
# busca el navegador: en vez del lugar de siempre, usa una carpeta ".local-
# browsers" pegada al propio .exe, que vive en el temporal de extracción y
# se puede borrar en cualquier momento. Fijamos nosotros la ruta de toda la
# vida ANTES de que Playwright tenga chance de pisarla, así el navegador
# instalado sobrevive entre sesiones y no se vuelve a bajar en cada arranque.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_cache_dir_por_defecto() / "ms-playwright"))

from .historial import (
    cargar_historial,
    construir_clave,
    periodo_actual,
    registrar_envio,
    ruta_historial,
)
from .selectores import TEXTOS_NUMERO_INVALIDO, cargar_selectores
from .simulador import generar_mensaje_unificado
from .telefonos import es_telefono_valido_arg, formatear_telefono
from .utils import (
    buscar_pdf_por_poliza,
    clave_agrupacion,
    emit,
    find_column,
    limpiar_nombre_persona,
    primer_nombre_persona,
    str_poliza,
)

PERFIL_POR_DEFECTO = Path.home() / ".rechazos_app" / "perfil_whatsapp"
URL_WHATSAPP = "https://web.whatsapp.com/"

ENVIADO_VERIFICADO = "ENVIADO Y VERIFICADO"
NO_SALIO = "NO ENVIADO — NO APARECIÓ EN EL CHAT"
CHAT_NO_ENCONTRADO = "ERROR — NO ENCONTRÉ EL CHAT"
NUMERO_INVALIDO = "ERROR — NÚMERO INVÁLIDO EN WHATSAPP"
ADJUNTO_FALLIDO = "ENVIADO SIN ADJUNTO — REVISAR"


def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFD", str(texto)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", texto).strip().lower()


class SesionWhatsApp:
    """Mantiene abierta una única pestaña de WhatsApp Web."""

    def __init__(self, perfil_dir=None, headless=False, log=None, timeout_ms=45000, on_qr=None):
        self.perfil_dir = Path(perfil_dir) if perfil_dir else PERFIL_POR_DEFECTO
        self.headless = headless
        self.log = log
        self.timeout_ms = timeout_ms
        self.on_qr = on_qr
        self.sel = cargar_selectores()
        self._pw = None
        self.ctx = None
        self.page = None

    # ---------- ciclo de vida ----------

    def _instalar_chromium(self):
        """Descarga el navegador de Playwright (equivalente a `playwright install chromium`).

        En el .exe empaquetado no hay pip ni consola para que el productor
        corra ese comando a mano: se dispara sola la primera vez que falta.
        """
        import playwright.__main__ as pw_cli

        emit(self.log, "Primera vez en esta computadora: descargando el navegador "
                        "de WhatsApp (puede tardar unos minutos)...")
        argv_previo = sys.argv
        sys.argv = ["playwright", "install", "chromium"]
        codigo = 0
        try:
            pw_cli.main()
        except SystemExit as e:
            codigo = e.code or 0
        finally:
            sys.argv = argv_previo
        if codigo != 0:
            raise RuntimeError(
                f"No se pudo descargar el navegador (código {codigo}). "
                "Revisá la conexión a internet e intentá de nuevo."
            )

    def abrir(self, timeout_login: float = 180):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise ImportError(
                "Falta Playwright. Instalalo con:\n"
                "    pip install playwright\n"
                "    python -m playwright install chromium"
            ) from e

        self.perfil_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        kwargs_lanzamiento = dict(
            user_data_dir=str(self.perfil_dir),
            headless=self.headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        if self.headless:
            # Chromium headless se anuncia como tal en el user-agent
            # ("HeadlessChrome") y WhatsApp Web lo detecta y se queda
            # trabado "conectando" sin sincronizar. Se lo hace pasar por un
            # Chrome de escritorio normal.
            kwargs_lanzamiento["user_agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
        try:
            # Perfil persistente: el QR se escanea una sola vez y la sesión queda.
            self.ctx = self._pw.chromium.launch_persistent_context(**kwargs_lanzamiento)
        except Exception as e:
            if "Executable doesn't exist" not in str(e):
                raise
            self._pw.stop()
            self._instalar_chromium()
            self._pw = sync_playwright().start()
            self.ctx = self._pw.chromium.launch_persistent_context(**kwargs_lanzamiento)
        if self.headless:
            # navigator.webdriver queda en True en todo navegador automatizado
            # por Playwright; es la otra pata de la detección de bots.
            self.ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_default_timeout(self.timeout_ms)
        emit(self.log, "Abriendo WhatsApp Web...")
        self.page.goto(URL_WHATSAPP, wait_until="domcontentloaded")
        self._esperar_login(timeout_login)

    def _esperar_login(self, timeout_login: float, timeout_sync: float = 600):
        """Espera a que aparezca la lista de chats. Si hay QR, avisa y espera.

        Dos plazos con paciencia distinta a propósito: mientras nadie escaneó
        el QR, alcanza con `timeout_login` (si no hay nadie mirando, esperar
        para siempre tampoco sirve). Pero apenas el QR desaparece —lo
        escanearon— WhatsApp arranca la primera sincronización, que en una
        cuenta con muchos chats puede tardar varios minutos: eso NO es un
        error, así que a partir de ahí se pasa a `timeout_sync`, bastante más
        generoso. Antes había un solo plazo fijo y una cuenta que tardaba en
        sincronizar terminaba marcada como "error" aunque el QR ya hubiera
        sido escaneado y la sesión fuera a quedar bien igual.
        """
        import time

        limite = time.time() + timeout_login
        vio_qr = False
        avisado_sync = False
        while time.time() < limite:
            if self._existe("lista_chats", timeout=2000):
                emit(self.log, "✓ Sesión de WhatsApp lista.")
                return
            hay_qr = self._existe("qr", timeout=1000)
            if hay_qr:
                if not vio_qr:
                    emit(self.log, "⚠ Escaneá el código QR con el celular. Lo mostramos en la app.")
                vio_qr = True
                if self.on_qr:
                    try:
                        self.on_qr(self._loc("qr").screenshot())
                    except Exception:
                        pass
            elif vio_qr and not avisado_sync:
                avisado_sync = True
                limite = time.time() + timeout_sync
                if self.on_qr:
                    self.on_qr(None)
                emit(self.log, "✓ QR escaneado. Sincronizando WhatsApp "
                                "(puede tardar varios minutos la primera vez, es normal)...")
            time.sleep(2)
        if self.on_qr:
            self.on_qr(None)
        raise TimeoutError(
            "No se pudo iniciar sesión en WhatsApp Web. "
            "Si aparecía el QR, escanealo y volvé a correr el paso."
        )

    def cerrar(self):
        for cerrar in (getattr(self.ctx, "close", None), getattr(self._pw, "stop", None)):
            try:
                if cerrar:
                    cerrar()
            except Exception:
                pass

    def esta_viva(self) -> bool:
        """Si el navegador se cerró solo, se colgó o se quedó sin internet,
        cualquier operación tira — se usa para decidir si hay que abrir una
        sesión nueva en vez de reusar esta."""
        if not self.page:
            return False
        try:
            self.page.evaluate("1")
            return True
        except Exception:
            return False

    # ---------- helpers de selectores ----------

    def _loc(self, clave: str, timeout: int | None = None):
        """Primer selector de la lista que aparezca en pantalla."""
        for css in self.sel.get(clave, []):
            try:
                loc = self.page.locator(css).first
                loc.wait_for(state="visible", timeout=timeout or 4000)
                return loc
            except Exception:
                continue
        return None

    def _existe(self, clave: str, timeout: int = 3000) -> bool:
        return self._loc(clave, timeout=timeout) is not None

    def _hay_cartel_numero_invalido(self) -> bool:
        try:
            cuerpo = _normalizar(self.page.inner_text("body"))
        except Exception:
            return False
        return any(t in cuerpo for t in TEXTOS_NUMERO_INVALIDO)

    # ---------- abrir un chat ----------

    def abrir_chat(self, telefono: str, timeout_chat: float = 30) -> str:
        """Abre el chat del número. Devuelve 'OK', 'NO_ENCONTRADO' o 'INVALIDO'.

        Primero intenta por el buscador (rápido, sin recargar). Si el número no
        tiene chat previo, cae al link, que sí recarga pero funciona siempre.
        """
        if self._abrir_por_buscador(telefono):
            return "OK"
        return self._abrir_por_link(telefono, timeout_chat)

    def _abrir_por_buscador(self, telefono: str) -> bool:
        digitos = re.sub(r"\D", "", telefono)
        buscador = self._loc("buscador", timeout=5000)
        if buscador is None:
            return False
        try:
            buscador.click()
            # Limpiar lo que haya quedado de la búsqueda anterior
            # ControlOrMeta: en macOS el atajo es Cmd+A, no Ctrl+A
            self.page.keyboard.press("ControlOrMeta+A")
            self.page.keyboard.press("Backspace")
            buscador.type(digitos, delay=20)
            self.page.wait_for_timeout(1800)

            resultado = self._loc("resultado_busqueda", timeout=3000)
            if resultado is None:
                self.page.keyboard.press("Escape")
                return False
            resultado.click()
            self.page.wait_for_timeout(600)
            listo = self._loc("composer", timeout=6000) is not None
            if not listo:
                self.page.keyboard.press("Escape")
            return listo
        except Exception:
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    def _abrir_por_link(self, telefono: str, timeout_chat: float) -> str:
        digitos = re.sub(r"\D", "", telefono)
        try:
            self.page.goto(
                f"{URL_WHATSAPP}send?phone={digitos}",
                wait_until="domcontentloaded",
                timeout=int(timeout_chat * 1000),
            )
        except Exception:
            return "NO_ENCONTRADO"
        # Esperar a que cargue el cuadro de mensaje o el cartel de error
        for _ in range(int(timeout_chat / 2)):
            if self._loc("composer", timeout=2000) is not None:
                return "OK"
            if self._hay_cartel_numero_invalido():
                return "INVALIDO"
            self.page.wait_for_timeout(500)
        return "INVALIDO" if self._hay_cartel_numero_invalido() else "NO_ENCONTRADO"

    # ---------- enviar ----------

    def _contar_filas(self, fragmento: str | None = None) -> int:
        """Filas de mensaje en el chat; si se pasa fragmento, solo las que lo contienen.

        Contar burbujas a secas no alcanzaba: daba por enviado un adjunto que
        seguía abierto en la vista previa. Buscar el contenido que acabamos de
        mandar es la única señal que no miente.
        """
        css = ", ".join(self.sel.get("fila_mensaje", ['[role="row"]']))
        frag = _normalizar(fragmento) if fragmento else ""
        try:
            return self.page.evaluate(
                """([css, frag]) => {
                    // Tiene que dar EXACTAMENTE lo mismo que _normalizar() de
                    // Python, que descarta todo lo no-ASCII. Si acá se dejaba
                    // pasar el '¡' de "¡Muchas gracias!", el fragmento no
                    // matcheaba nunca y todo figuraba como no enviado.
                    const norm = s => (s || '').normalize('NFD')
                        .replace(/[\\u0300-\\u036f]/g, '')
                        .replace(/[^\\x00-\\x7F]/g, '')
                        .replace(/\\s+/g, ' ').trim().toLowerCase();
                    const filas = Array.from(document.querySelectorAll(css));
                    if (!frag) return filas.length;
                    return filas.filter(f => norm(f.innerText).includes(frag)).length;
                }""",
                [css, frag],
            )
        except Exception:
            return 0

    @staticmethod
    def _fragmento(texto: str, largo: int = 45) -> str:
        """Trozo distintivo del final del mensaje, para buscarlo en el chat."""
        t = _normalizar(texto)
        return t[-largo:] if len(t) > largo else t

    def _visible_ya(self, clave: str) -> bool:
        """¿Está visible AHORA? Sin wait_for.

        `_loc` espera por cada selector de la lista, así que preguntar por algo
        que NO está cuesta la suma de todos los timeouts. Dentro de un loop de
        verificación eso son minutos colgado, así que acá se mira y se contesta.
        """
        for css in self.sel.get(clave, []):
            try:
                loc = self.page.locator(css)
                if loc.count() and loc.first.is_visible(timeout=250):
                    return True
            except Exception:
                continue
        return False

    def _overlay_adjunto_abierto(self) -> bool:
        """True mientras la vista previa del adjunto siga en pantalla.

        Las etiquetas del overlay están en el idioma de WhatsApp, así que como
        respaldo se mira el botón de enviar, que se reconoce por su data-icon y
        no depende del idioma: con el cuadro de mensaje vacío (que es el caso
        cuando ya mandamos el texto) solo está visible si hay un adjunto en
        vista previa.
        """
        if self._visible_ya("overlay_adjunto"):
            return True
        return self._visible_ya("boton_enviar")

    def _labels_visibles(self) -> list:
        """aria-labels de los botones visibles ahora mismo."""
        try:
            return self.page.evaluate(
                """() => Array.from(document.querySelectorAll('button[aria-label]'))
                    .filter(b => b.getBoundingClientRect().width > 0)
                    .map(b => b.getAttribute('aria-label'))"""
            )
        except Exception:
            return []

    def _click_primer_boton_nuevo(self, antes: list) -> str | None:
        """Clickea el primer botón que apareció desde `antes`.

        Al abrir el menú del clip, el primer botón nuevo es "Documento" (después
        vienen Fotos y videos, Cámara, Audio...). Sirve en cualquier idioma.
        """
        try:
            return self.page.evaluate(
                """(antes) => {
                    const btns = Array.from(document.querySelectorAll('button[aria-label]'))
                        .filter(b => b.getBoundingClientRect().width > 0);
                    const nuevo = btns.find(b => !antes.includes(b.getAttribute('aria-label')));
                    if (!nuevo) return null;
                    nuevo.click();
                    return nuevo.getAttribute('aria-label');
                }""",
                antes,
            )
        except Exception:
            return None

    def _abrir_menu_documento(self) -> bool:
        """Abre el clip y elige "Documento" para que exista el input de PDFs."""
        # Si el menú ya está abierto, no hay que volver a tocar el clip
        # (lo cerraría).
        antes = []
        if not self._visible_ya("opcion_documento"):
            antes = self._labels_visibles()
            clip = self._loc("boton_adjuntar", timeout=6000)
            if clip is None:
                return False
            try:
                clip.click()
            except Exception:
                return False
            self.page.wait_for_timeout(900)

        opcion = self._loc("opcion_documento", timeout=4000)
        if opcion is not None:
            try:
                opcion.click()
                self.page.wait_for_timeout(700)
                return True
            except Exception:
                pass

        # WhatsApp en otro idioma: el primer botón nuevo del menú es Documento.
        etiqueta = self._click_primer_boton_nuevo(antes)
        if etiqueta is None:
            return False
        emit(self.log, f"(menú en otro idioma: usé la opción '{etiqueta}')")
        self.page.wait_for_timeout(700)
        return True

    def _click_enviar(self) -> bool:
        for css in self.sel.get("boton_enviar", []):
            try:
                loc = self.page.locator(css).last
                loc.wait_for(state="visible", timeout=3000)
            except Exception:
                continue
            try:
                loc.click(timeout=5000)
                return True
            except Exception:
                # El icono puede no ser el que escucha el clic: probamos el padre.
                try:
                    loc.evaluate(
                        "el => (el.closest('[role=\"button\"]') || el.closest('div[aria-label]') || el).click()"
                    )
                    return True
                except Exception:
                    continue
        return False

    def _escribir_multilinea(self, composer, texto: str):
        """Escribe respetando los saltos de línea.

        Enter manda el mensaje, así que entre líneas va Shift+Enter.
        """
        lineas = texto.split("\n")
        for i, linea in enumerate(lineas):
            if linea:
                composer.type(linea, delay=8)
            if i < len(lineas) - 1:
                self.page.keyboard.press("Shift+Enter")

    def enviar_texto(self, texto: str, timeout_envio: float = 25) -> bool:
        composer = self._loc("composer", timeout=8000)
        if composer is None:
            return False
        frag = self._fragmento(texto)
        antes = self._contar_filas(frag)
        composer.click()
        self._escribir_multilinea(composer, texto)
        self.page.wait_for_timeout(300)
        self.page.keyboard.press("Enter")
        # Enviado = apareció en el chat una fila más con este mismo texto.
        pasos = max(int(timeout_envio * 2), 6)
        for _ in range(pasos):
            self.page.wait_for_timeout(500)
            if self._contar_filas(frag) > antes:
                return True
        return False

    def enviar_documento(self, ruta_pdf: str, timeout_envio: float = 45) -> bool:
        """Adjunta y manda el PDF. Mandar el cupón es el motivo de existir de
        toda la app: un cupón que no sale no es un detalle menor, así que acá
        se reintenta una vez antes de darlo por perdido — un hipo puntual (un
        menú que tardó en abrir, un clic que llegó un poco temprano) no debería
        bastar para dejar a un cliente sin su cupón.
        """
        ruta = str(Path(ruta_pdf).resolve())
        # WhatsApp muestra el nombre del archivo en la burbuja del documento:
        # esa es la confirmación de que salió.
        frag_nombre = _normalizar(Path(ruta).stem)[:18]

        for intento in range(2):
            antes_archivo = self._contar_filas(frag_nombre)
            antes_total = self._contar_filas()
            if self._intentar_adjuntar(ruta, frag_nombre, antes_archivo, antes_total, timeout_envio):
                return True
            if intento == 0:
                emit(self.log, "  ⚠ El adjunto no confirmó a la primera, reintento...")
                try:
                    self.page.keyboard.press("Escape")
                except Exception:
                    pass
                self.page.wait_for_timeout(1000)
        return False

    def _intentar_adjuntar(self, ruta: str, frag_nombre: str, antes_archivo: int,
                            antes_total: int, timeout_envio: float) -> bool:
        # El clic en "Documento" dispara el <input type=file> oculto de
        # WhatsApp. Si dejamos que eso abra el diálogo nativo de Windows,
        # Playwright ya no puede tocarlo y el archivo no se adjunta hasta que
        # alguien lo cierre a mano. Por eso interceptamos el file chooser
        # ANTES de hacer el clic, así el diálogo nativo nunca llega a abrirse.
        try:
            with self.page.expect_file_chooser(timeout=10000) as fc_info:
                if not self._abrir_menu_documento():
                    return False
        except Exception:
            return False

        try:
            fc_info.value.set_files(ruta)
        except Exception:
            return False

        # Esperar a que aparezca la vista previa del adjunto
        for _ in range(24):
            if self._overlay_adjunto_abierto():
                break
            self.page.wait_for_timeout(500)
        else:
            return False

        # En la vista previa alcanza con Enter. El clic al botón queda de
        # respaldo por si el foco no quedó en el overlay.
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(1200)
        if self._overlay_adjunto_abierto():
            self._click_enviar()

        # Enviado de verdad = se cerró la vista previa Y apareció en el chat el
        # nombre del archivo (o al menos una fila nueva). Antes alcanzaba con
        # que creciera un contador, y eso daba por bueno un PDF que seguía
        # abierto en el preview sin mandarse.
        pasos = max(int(timeout_envio * 2), 6)
        for _ in range(pasos):
            self.page.wait_for_timeout(500)
            if self._overlay_adjunto_abierto():
                continue
            if self._contar_filas(frag_nombre) > antes_archivo:
                return True
            if self._contar_filas() > antes_total:
                return True
        return False

    def screenshot(self, ruta: str):
        try:
            Path(ruta).parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=ruta, full_page=False)
        except Exception:
            pass


def enviar_whatsapp_web(
    excel_entrada: str,
    log=None,
    telefono_por_defecto: str = "+5492494585215",
    checkpoint_file: str = "04_enviados.json",
    historial_file: str | None = None,
    periodo: str | None = None,
    errores_dir: str = "errores_screenshot",
    perfil_dir: str | None = None,
    headless: bool = False,
    timeout_login: float = 180,
    timeout_chat: float = 30,
    timeout_envio: float = 30,
    pausa_entre_envios: float = 1.5,
    plantilla_mensaje: str | None = None,
    check_cancel_cb=None,
    on_qr=None,
    sesion: "SesionWhatsApp | None" = None,
    **_ignorados,
):
    """Manda los mensajes del Excel usando una sola sesión de WhatsApp Web.

    Si se pasa `sesion` (ya abierta de antes), la reusa y no la cierra al
    terminar — así no hay que volver a levantar Chrome en cada envío. Si no
    se pasa ninguna, se comporta como siempre: abre una propia y la cierra
    al final.

    Devuelve el mismo diccionario que el motor viejo, así la interfaz no cambia.
    """
    if not Path(excel_entrada).exists():
        raise FileNotFoundError(f"No encontré '{excel_entrada}'")

    df = pd.read_excel(excel_entrada)
    df.columns = df.columns.str.strip()
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]

    col_tel = find_column(df, "TELEFONO", "TELÉFONO", "CEL", "MOVIL", "MÓVIL")
    col_pol = find_column(df, "POLIZA", "PÓLIZA")
    col_cli = find_column(df, "CLIENTE", "ASEGURADO")
    col_cuit = find_column(df, "CUIT", "CUIL")
    if not all([col_tel, col_pol, col_cli, col_cuit]):
        raise ValueError("Faltan columnas requeridas en el Excel")

    df["_clave_agrupacion"] = clave_agrupacion(df, col_cuit, col_cli)
    grupos = list(df.groupby("_clave_agrupacion"))

    periodo = periodo or periodo_actual()
    historial = cargar_historial(historial_file)
    ya_periodo = [k for k in historial if k.endswith(f"|{periodo}")]
    emit(log, f"Historial permanente: {ruta_historial(historial_file)}")
    emit(log, f"Ya notificados en {periodo}: {len(ya_periodo)} (histórico: {len(historial)})")

    exitos = nuevos_enviados = previamente_enviados = 0
    fallas = borradores = numeros_invalidos = sin_adjunto = 0
    saltados, para_revisar, reporte_rows = [], [], []

    def espejo_checkpoint():
        try:
            with open(checkpoint_file, "w", encoding="utf-8") as f:
                json.dump(sorted(historial.keys()), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    sesion_propia = sesion is None
    if sesion_propia:
        sesion = SesionWhatsApp(perfil_dir=perfil_dir, headless=headless, log=log, on_qr=on_qr)
    try:
        if sesion_propia:
            sesion.abrir(timeout_login=timeout_login)

        for idx, (_clave_vieja, filas) in enumerate(grupos, start=1):
            if check_cancel_cb:
                try:
                    check_cancel_cb()
                except Exception as e:
                    emit(log, f"Proceso pausado/detenido: {e}")
                    break

            tel_crudo = str(filas.iloc[0].get(col_tel, "")).strip()
            tel = formatear_telefono(tel_crudo)
            cliente = limpiar_nombre_persona(str(filas.iloc[0].get(col_cli)))
            cuit_val = str(filas.iloc[0].get(col_cuit, "")).strip()
            lista_polizas = [str_poliza(p) for p in filas[col_pol].unique()]
            pol_str = ", ".join(lista_polizas)
            now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            clave_hist = construir_clave(cuit_val, lista_polizas, periodo, cliente=cliente)

            def anotar(estado: str, telefono: str = ""):
                reporte_rows.append({
                    "FECHA_HORA": now_str,
                    "ASEGURADO": cliente,
                    "POLIZAS": pol_str,
                    "TELEFONO": telefono or tel or tel_crudo or "SIN TELEFONO",
                    "ESTADO": estado,
                })

            if clave_hist in historial:
                cuando = historial[clave_hist].get("fecha", "")
                detalle = "en una corrida anterior" if cuando in ("", "migrado") else f"el {cuando}"
                emit(log, f"[{idx}/{len(grupos)}] Ya notificado {detalle} — saltando ({cliente})")
                exitos += 1
                previamente_enviados += 1
                anotar("SALTADO (YA NOTIFICADO)")
                continue

            if not es_telefono_valido_arg(tel):
                motivo = "sin teléfono" if not tel_crudo or tel_crudo.lower() in ("nan", "none", "") else f"teléfono inválido ({tel_crudo})"
                emit(log, f"[{idx}/{len(grupos)}] ✗ {cliente} — {motivo}")
                saltados.append(f"{cliente} — {motivo}")
                para_revisar.append(f"{cliente} — {motivo}")
                fallas += 1
                anotar(f"ERROR — {motivo.upper()}", telefono=tel_crudo or "SIN TELEFONO")
                continue

            pdfs = []
            for p in lista_polizas:
                ruta = buscar_pdf_por_poliza(p, Path(excel_entrada).parent)
                if ruta and ruta not in pdfs:
                    pdfs.append(ruta)
            if not pdfs:
                saltados.append(f"{cliente} — PDFs no encontrados")
                para_revisar.append(f"{cliente} — PDF no encontrado")
                fallas += 1
                anotar("ERROR — PDF NO ENCONTRADO")
                continue

            primer_nombre = primer_nombre_persona(cliente)

            try:
                estado_chat = sesion.abrir_chat(tel, timeout_chat=timeout_chat)
                if estado_chat == "INVALIDO":
                    emit(log, f"[{idx}/{len(grupos)}] ✗ {cliente} ({tel}) — WhatsApp dice que el número no existe")
                    numeros_invalidos += 1
                    fallas += 1
                    saltados.append(f"{cliente} — número inexistente ({tel})")
                    para_revisar.append(f"{cliente} — número inexistente ({tel})")
                    anotar(NUMERO_INVALIDO)
                    continue
                if estado_chat != "OK":
                    emit(log, f"[{idx}/{len(grupos)}] ✗ {cliente} ({tel}) — no pude abrir el chat")
                    fallas += 1
                    saltados.append(f"{cliente} — no se pudo abrir el chat")
                    para_revisar.append(f"{cliente} — no se pudo abrir el chat ({tel})")
                    sesion.screenshot(str(Path(errores_dir) / f"chat_{re.sub(r'[^A-Za-z0-9]', '_', cliente)}.png"))
                    anotar(CHAT_NO_ENCONTRADO)
                    continue

                msg = generar_mensaje_unificado(primer_nombre, lista_polizas, plantilla=plantilla_mensaje)
                if not sesion.enviar_texto(msg, timeout_envio=timeout_envio):
                    emit(log, f"[{idx}/{len(grupos)}] ✗ {cliente} ({tel}) — el mensaje no apareció en el chat")
                    borradores += 1
                    fallas += 1
                    saltados.append(f"{cliente} — el mensaje no salió")
                    para_revisar.append(f"{cliente} — el mensaje no salió ({tel})")
                    sesion.screenshot(str(Path(errores_dir) / f"texto_{re.sub(r'[^A-Za-z0-9]', '_', cliente)}.png"))
                    anotar(NO_SALIO)
                    continue

                adjuntos_ok = True
                for pdf in pdfs:
                    if not sesion.enviar_documento(pdf, timeout_envio=max(timeout_envio, 45)):
                        adjuntos_ok = False
                        emit(log, f"[{idx}/{len(grupos)}] ⚠ {cliente}: no pude confirmar el adjunto {Path(pdf).name}")
                        sesion.screenshot(str(Path(errores_dir) / f"adj_{re.sub(r'[^A-Za-z0-9]', '_', cliente)}.png"))

                estado_final = ENVIADO_VERIFICADO if adjuntos_ok else ADJUNTO_FALLIDO
                registrar_envio(
                    historial, clave_hist,
                    cliente=cliente, telefono=tel, polizas=lista_polizas,
                    estado=estado_final, archivo=historial_file,
                )
                espejo_checkpoint()
                exitos += 1
                nuevos_enviados += 1
                anotar(estado_final)
                if adjuntos_ok:
                    emit(log, f"[{idx}/{len(grupos)}] ✓ Enviado y verificado: {cliente} ({tel})")
                else:
                    sin_adjunto += 1
                    para_revisar.append(f"{cliente} — texto enviado, adjunto sin confirmar ({tel})")
                    emit(log, f"[{idx}/{len(grupos)}] ⚠ Texto enviado sin confirmar adjunto: {cliente}")

                sesion.page.wait_for_timeout(int(pausa_entre_envios * 1000))
            except Exception as e:
                fallas += 1
                saltados.append(f"{cliente} — Error: {e}")
                para_revisar.append(f"{cliente} — Error: {e}")
                anotar(f"ERROR: {e}")
                emit(log, f"[{idx}/{len(grupos)}] ✗ {cliente}: {e}")
    finally:
        if sesion_propia:
            sesion.cerrar()

    if reporte_rows:
        try:
            rep_path = Path(excel_entrada).parent / "04_reporte_envios.xlsx"
            pd.DataFrame(reporte_rows).to_excel(rep_path, index=False)
            emit(log, f"📊 Reporte detallado guardado en: {rep_path.name}")
        except Exception as err:
            emit(log, f"No se pudo guardar el reporte Excel: {err}")

    if para_revisar:
        try:
            rev_path = Path(excel_entrada).parent / "04_para_revisar.json"
            with open(rev_path, "w", encoding="utf-8") as f:
                json.dump(para_revisar, f, ensure_ascii=False, indent=2)
            emit(log, f"⚠ {len(para_revisar)} casos para revisar a mano — detalle en {rev_path.name}")
        except Exception as err:
            emit(log, f"No se pudo guardar la lista de revisión: {err}")

    emit(log, f"Resumen — verificados: {nuevos_enviados - sin_adjunto} · sin adjunto: {sin_adjunto} · no salieron: {borradores} · números inválidos: {numeros_invalidos} · otras fallas: {fallas - borradores - numeros_invalidos}")

    return {
        "exitos": exitos,
        "nuevos_enviados": nuevos_enviados,
        "previamente_enviados": previamente_enviados,
        "fallas": fallas,
        "sin_verificar": sin_adjunto,
        "borradores": borradores,
        "numeros_invalidos": numeros_invalidos,
        "saltados": saltados,
        "para_revisar": para_revisar,
        "checkpoint": checkpoint_file,
        "historial": str(ruta_historial(historial_file)),
        "periodo": periodo,
        "telefono_por_defecto": telefono_por_defecto,
        "outputs": [str(Path(checkpoint_file).resolve()), str(Path(errores_dir).resolve())],
    }
