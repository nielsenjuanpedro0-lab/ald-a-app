"""Descarga de rechazos y cuponeras desde la Oficina Virtual de SMG.

Pantalla de origen: Visor de Archivos (oficina.smg.com.ar/Dijkstra/VisorArchivos/Index),
sección "Rechazos de Tarjetas" (cod_tipo_archivo=7). Cada archivo publicado
tiene un nombre del estilo "Rechazos VISA 28-10-2026" (Excel con el listado)
o "Cuponera VISA 28-10-2026" (PDF con los cupones), por productor.

El login de la OV tiene reCAPTCHA y un WAF que bloquea logins automatizados,
así que se hace con un Chrome VISIBLE (Playwright): la app completa usuario
y contraseña, la persona resuelve el captcha si aparece y toca Ingresar.
Después, con esa misma sesión, se llama a la API interna de la página
(GetFiles / ViewFile) para listar y bajar los archivos.

Playwright (API sync) solo se puede usar desde el hilo que lo creó, y el
servidor atiende cada pedido en un hilo distinto. Por eso todo lo que toca
el navegador corre en un único hilo propio (_TrabajadorOV) y los comandos le
pasan el trabajo por una cola.
"""
from __future__ import annotations

import hashlib
import html
import queue
import re
import threading
import unicodedata
from concurrent.futures import Future
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from .utils import limpiar_lock_perfil

BASE_URL = "https://oficina.smg.com.ar"
URL_VISOR = f"{BASE_URL}/Dijkstra/VisorArchivos/Index"
URL_GETFILES = f"{BASE_URL}/Dijkstra/VisorArchivos/GetFiles"
URL_VIEWFILE = f"{BASE_URL}/Dijkstra/VisorArchivos/ViewFile"
TIPO_RECHAZOS_TARJETAS = "7"

SEL_USUARIO = "#ctl00_cphBarra_login_UserName"
SEL_PASSWORD = "#ctl00_cphBarra_login_Password"
SEL_INGRESAR = '[name="ctl00$cphBarra$login$_login"]'

PERFILES_DIR = Path.home() / ".rechazos_app" / "perfil_ov"

# "Rechazos VISA 28-10-2026", "Cuponera VISA_2_NUEVA_RENOV 28-10-2026", ...
_RE_NOMBRE = re.compile(
    r"^\s*(?P<tipo>rechazos?|cuponeras?)\s+(?P<medio>.+?)\s+(?P<fecha>\d{1,2}-\d{1,2}-\d{4})\s*$",
    re.IGNORECASE,
)


# ──────────────────────────────────────────────────────────────
#  Parseo (sin navegador: se prueba contra respuestas reales)
# ──────────────────────────────────────────────────────────────

@dataclass
class ArchivoOV:
    id: str
    agente: str
    publicado: str        # dd/mm/aaaa (fecha de alta en la OV)
    tipo: str             # "rechazos" | "cuponera" | "otro"
    medio: str            # "VISA", "MASTERCARD", "CBU", ...
    fecha: str            # aaaa-mm-dd (la del nombre del archivo)
    extension: str        # "pdf" | "xls" | ...
    nombre: str           # texto original del link
    key: str              # parámetro de ViewFile, ya decodificado de HTML

    def a_dict(self) -> dict:
        return asdict(self)


def _sin_tildes(texto: str) -> str:
    return unicodedata.normalize("NFD", texto).encode("ascii", "ignore").decode()


def parsear_nombre(nombre: str) -> tuple[str, str, str]:
    """'Rechazos VISA 28-10-2026' -> ('rechazos', 'VISA', '2026-10-28').

    Lo que no sigue ese formato vuelve como tipo 'otro' (no se descarta en
    silencio: la interfaz lo puede mostrar igual)."""
    m = _RE_NOMBRE.match(_sin_tildes(nombre or ""))
    if not m:
        return "otro", "", ""
    tipo = "rechazos" if m.group("tipo").lower().startswith("rechazo") else "cuponera"
    medio = m.group("medio").strip().upper()
    d, mth, y = (int(x) for x in m.group("fecha").split("-"))
    try:
        fecha = date(y, mth, d).isoformat()
    except ValueError:
        fecha = ""
    return tipo, medio, fecha


def parsear_fila(fila: dict) -> ArchivoOV | None:
    """Una fila de la grilla de GetFiles: {"id":..., "cell":[id, fecha, agente, icono, link]}."""
    celdas = fila.get("cell") or []
    if len(celdas) < 5:
        return None
    link = celdas[-1]
    m_key = re.search(r'href="ViewFile\?key=([^"]+)"', link)
    if not m_key:
        return None
    nombre = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", link))).strip()
    m_ext = re.search(r"icon-ext-(\w+)", celdas[3] or "")
    tipo, medio, fecha = parsear_nombre(nombre)
    return ArchivoOV(
        id=str(fila.get("id") or celdas[0]).strip(),
        agente=str(celdas[2]).strip(),
        publicado=str(celdas[1]).strip(),
        tipo=tipo,
        medio=medio,
        fecha=fecha,
        extension=(m_ext.group(1).lower() if m_ext else ""),
        nombre=nombre,
        # El href viene con &amp; y demás escapes de HTML, pero el %2b/%2f ya
        # es el encoding de URL que espera ViewFile: no hay que decodificarlo.
        key=html.unescape(m_key.group(1)),
    )


def aplanar_arbol(arbol: list) -> list[dict]:
    """El árbol de la página (organizador -> productores) a una lista plana
    de productores con su organizador, que es lo que muestra la interfaz.

    El árbol real (jsTree) no siempre trae un dict en cada nodo — algunas
    cuentas tienen hojas sueltas como texto plano. Un nodo así no es un
    productor real (no tiene id/li_attr), así que se saltea en vez de
    romper toda la conexión por un nodo raro."""
    productores = []
    vistos = set()

    def visitar(nodo, organizador):
        if not isinstance(nodo, dict):
            return
        attr = nodo.get("li_attr")
        attr = attr if isinstance(attr, dict) else {}
        hijos = nodo.get("children")
        hijos = hijos if isinstance(hijos, list) else []
        if attr.get("esFinal") or not hijos:
            codigo = str(nodo.get("id", "")).strip()
            if codigo and codigo not in ("-1", "0") and codigo not in vistos:
                vistos.add(codigo)
                nombre = attr.get("nombre") or re.sub(r"^\s*\d+\s*-\s*", "", str(nodo.get("text", "")))
                productores.append({
                    "codigo": codigo,
                    "nombre": str(nombre).strip(),
                    "organizador": organizador,
                })
        for h in hijos:
            visitar(h, organizador if organizador else (attr.get("nombre") or str(nodo.get("text", ""))).strip())

    for raiz in arbol or []:
        if not isinstance(raiz, dict):
            continue
        attr = raiz.get("li_attr")
        attr = attr if isinstance(attr, dict) else {}
        org = (attr.get("nombre") or raiz.get("text") or "").strip()
        hijos_raiz = raiz.get("children")
        if isinstance(hijos_raiz, list) and hijos_raiz:
            for h in hijos_raiz:
                visitar(h, org)
        else:
            visitar(raiz, "")
    productores.sort(key=lambda p: (p["organizador"], p["nombre"]))
    return productores


def _productores_de_select(page) -> list[dict]:
    """Respaldo para cuentas con pocos productores: la OV no les arma el
    árbol de JavaScript (jsTree) — en su lugar les pone un <select> HTML
    común con las opciones, sin ningún organizador. "arbolProductores" en
    esa versión de la página es solo el id de la sección que lo contiene,
    no la variable que esperábamos leer."""
    try:
        opciones = page.eval_on_selector_all(
            "#AgenteSeleccionado option",
            "els => els.map(e => ({valor: e.value, texto: e.textContent}))",
        )
    except Exception:
        return []
    productores = []
    for o in opciones or []:
        codigo = str(o.get("valor") or "").strip()
        if not codigo:
            continue
        texto = str(o.get("texto") or "").strip()
        nombre = texto.split("-", 1)[1].strip() if "-" in texto else texto
        productores.append({"codigo": codigo, "nombre": nombre, "organizador": ""})
    return productores


def _fecha(a: ArchivoOV) -> date:
    return date.fromisoformat(a.fecha)


def _publicado(a: ArchivoOV) -> date | None:
    try:
        return datetime.strptime(a.publicado, "%d/%m/%Y").date()
    except ValueError:
        return None


def clave_tanda(medio: str, fecha: str) -> str:
    return f"{medio}|{fecha}"


def mes_anterior(mes: str) -> str:
    """'2026-11' -> '2026-10'; '2026-01' -> '2025-12'."""
    y, m = (int(x) for x in mes.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def armar_tandas(archivos: list[ArchivoOV]) -> list[dict]:
    """Agrupa los archivos en tandas: una tanda es un medio + una fecha de
    débito ("Rechazos VISA 21-11-2026" de todos los productores elegidos).
    Cada medio puede tener más de una tanda por mes (VISA el 21 y el 28, CBU
    el 13 y el 28...). El mes de la tanda es el de su fecha, no el de
    publicación."""
    tandas: dict[str, dict] = {}
    for a in archivos:
        if a.tipo != "rechazos" or not a.fecha:
            continue
        k = clave_tanda(a.medio, a.fecha)
        t = tandas.setdefault(k, {"clave": k, "medio": a.medio, "fecha": a.fecha, "mes": a.fecha[:7],
                                  "publicado": a.publicado, "productores": set()})
        t["productores"].add(a.agente)
        pub = _publicado(a)
        try:
            pub_t = datetime.strptime(t["publicado"], "%d/%m/%Y").date()
        except ValueError:
            pub_t = None
        if pub and (pub_t is None or pub > pub_t):
            t["publicado"] = a.publicado
    salida = [{**t, "productores": len(t["productores"])} for t in tandas.values()]
    salida.sort(key=lambda t: (t["fecha"], t["medio"]), reverse=True)
    return salida


def anteriores(archivos: list[ArchivoOV], elegidas: list[tuple[str, str]]) -> list[ArchivoOV]:
    """Contra qué se compara para detectar segundos rechazos: para cada
    productor y medio, la tanda INMEDIATAMENTE ANTERIOR a la más vieja de las
    elegidas (sin importar el mes). Se busca por productor porque no todos
    tienen archivo en todas las tandas: si a uno le falta la última, se usa
    la que sí tenga."""
    minima: dict[str, str] = {}
    for medio, fecha in elegidas:
        if medio not in minima or fecha < minima[medio]:
            minima[medio] = fecha
    agentes_por_medio: dict[str, set] = {}
    for a in archivos:
        if a.tipo == "rechazos" and (a.medio, a.fecha) in set(elegidas):
            agentes_por_medio.setdefault(a.medio, set()).add(a.agente)
    previa: dict[tuple[str, str], str] = {}
    for a in archivos:
        if (a.tipo != "rechazos" or a.medio not in minima or not a.fecha
                or a.agente not in agentes_por_medio.get(a.medio, ()) or a.fecha >= minima[a.medio]):
            continue
        k = (a.agente, a.medio)
        if k not in previa or a.fecha > previa[k]:
            previa[k] = a.fecha
    return [a for a in archivos
            if a.tipo == "rechazos" and previa.get((a.agente, a.medio)) == a.fecha]


def _cuponera_de(c: ArchivoOV, medio: str, fecha: str, agentes: set) -> bool:
    # "Rechazos VISA 21-11" viene con "Cuponera VISA_2_NUEVA_RENOV 21-11": la
    # cuponera puede tener un sufijo en el medio. Se empareja por productor,
    # fecha y prefijo de medio (CBU/VISA/MASTERCARD no se pisan entre sí).
    return (c.tipo == "cuponera" and c.fecha == fecha and c.agente in agentes
            and (c.medio == medio or c.medio.startswith(medio + "_")))


def clasificar(archivos: list[ArchivoOV], claves: list[str]) -> dict:
    """Qué bajar para las tandas elegidas:

    - rechazos de esas tandas                          -> "Rechazos — mes actual"
    - rechazos de la tanda anterior (por productor y    -> "Rechazos — mes pasado"
      medio, ver anteriores()) para 2dos rechazos
    - cuponeras de esas tandas                         -> "Cupones (PDF)"
    """
    elegidas = [tuple(k.split("|", 1)) for k in claves]

    def unicos(lista):
        # La OV a veces publica el mismo archivo dos veces: se deduplica por key.
        vistos, out = set(), []
        for a in lista:
            if a.key not in vistos:
                vistos.add(a.key)
                out.append(a)
        return out

    actual, cuponeras = [], []
    for medio, fecha in elegidas:
        rech = [a for a in archivos if a.tipo == "rechazos" and a.medio == medio and a.fecha == fecha]
        actual += rech
        agentes = {a.agente for a in rech}
        cuponeras += [c for c in archivos if _cuponera_de(c, medio, fecha, agentes)]
    pasado = anteriores(archivos, elegidas) if elegidas else []
    return {"mes_actual": unicos(actual), "mes_pasado": unicos(pasado), "cuponeras": unicos(cuponeras)}


def _nombre_seguro(texto: str) -> str:
    return re.sub(r"[^\w.-]+", "_", _sin_tildes(texto)).strip("_")


def nombre_local(a: ArchivoOV) -> str:
    ext = a.extension or ("pdf" if a.tipo == "cuponera" else "xls")
    # Sufijo corto derivado de la key: el mismo productor puede tener dos
    # archivos con igual medio y fecha (ej. dos cuponeras VISA del mismo día).
    sufijo = hashlib.sha1(a.key.encode()).hexdigest()[:6]
    return _nombre_seguro(f"{a.agente}_{a.tipo}_{a.medio}_{a.fecha}") + f"_{sufijo}.{ext}"


def unir_excels(rutas: list[str], destino: str) -> str:
    """Varios Excel de rechazos (uno por productor y medio) en uno solo, que
    es lo que espera el paso "Detectar rechazos". Usa el mismo lector que
    ese paso (busca solo la fila de encabezado con POLIZA)."""
    import pandas as pd
    from .filtrar import _read_excel_smart

    tablas = []
    for r in rutas:
        try:
            df, _ = _read_excel_smart(r)
        except Exception:
            # Algunos "xls" de sistemas viejos son en realidad una tabla HTML.
            df = pd.read_html(r)[0]
        df.columns = [str(c).strip() for c in df.columns]
        tablas.append(df)
    if not tablas:
        raise ValueError("No hay archivos de rechazos para unir.")
    unido = pd.concat(tablas, ignore_index=True, sort=False)
    unido = unido.loc[:, [c for c in unido.columns if not str(c).startswith("Unnamed")]]
    Path(destino).parent.mkdir(parents=True, exist_ok=True)
    unido.to_excel(destino, index=False)
    return destino


def cuerpo_getfiles(agentes: list[str], pagina: int, filas: int) -> str:
    """Mismo formulario que arma la página (jQuery con traditional=true:
    cod_agentes repetido una vez por productor)."""
    partes = [f"cod_agentes={quote(str(a))}" for a in agentes]
    partes += [
        f"cod_tipo_archivo={TIPO_RECHAZOS_TARJETAS}",
        "_search=false",
        f"nd={int(datetime.now().timestamp() * 1000)}",
        f"rows={filas}",
        f"page={pagina}",
        "sidx=fec_alta",
        "sord=desc",
        "cod_grupo=0",
        "ver_organizador=false",
    ]
    return "&".join(partes)


# ──────────────────────────────────────────────────────────────
#  Navegador (hilo propio)
# ──────────────────────────────────────────────────────────────

class _TrabajadorOV:
    """Dueño único de Playwright: todo lo que toca el navegador corre acá."""

    def __init__(self):
        self._cola: queue.Queue = queue.Queue()
        self._hilo = threading.Thread(target=self._loop, name="ov-smg", daemon=True)
        self._hilo.start()
        self._pw = None
        self.ctx = None
        self.page = None
        self.usuario = None

    def ejecutar(self, func, *args, **kwargs):
        fut: Future = Future()
        self._cola.put((fut, func, args, kwargs))
        return fut.result()

    def _loop(self):
        while True:
            fut, func, args, kwargs = self._cola.get()
            try:
                fut.set_result(func(self, *args, **kwargs))
            except BaseException as e:  # noqa: BLE001 — se reporta al que pidió
                fut.set_exception(e)


_trabajador: _TrabajadorOV | None = None
_trabajador_lock = threading.Lock()


def _t() -> _TrabajadorOV:
    global _trabajador
    with _trabajador_lock:
        if _trabajador is None:
            _trabajador = _TrabajadorOV()
        return _trabajador


def _viva(t: _TrabajadorOV) -> bool:
    try:
        return t.page is not None and not t.page.is_closed() and bool(t.page.url)
    except Exception:
        return False


def _cerrar(t: _TrabajadorOV):
    for obj, metodo in ((t.ctx, "close"), (t._pw, "stop")):
        try:
            if obj is not None:
                getattr(obj, metodo)()
        except Exception:
            pass
    t.ctx = t.page = t._pw = None
    t.usuario = None


def _ventana(t: _TrabajadorOV, estado: dict) -> None:
    """Mueve/minimiza la ventana real de Chrome por CDP. Si falla (cambia
    algo en Chrome), no es grave: la ventana solo queda visible."""
    try:
        cdp = t.ctx.new_cdp_session(t.page)
        win = cdp.send("Browser.getWindowForTarget")["windowId"]
        if estado.get("windowState") == "normal":
            cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {"windowState": "normal"}})
        cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": estado})
        cdp.detach()
    except Exception:
        pass


def _esconder_ventana(t: _TrabajadorOV) -> None:
    _ventana(t, {"windowState": "minimized"})


def _mostrar_ventana(t: _TrabajadorOV) -> None:
    _ventana(t, {"windowState": "normal", "left": 120, "top": 80, "width": 1200, "height": 860})
    try:
        t.page.bring_to_front()
    except Exception:
        pass


def _esperar_login(page, segundos: float) -> None:
    """Espera a llegar al Visor. Si la OV vuelve al login con un mensaje (va
    en #result: es el cartelito que aparece sobre la contraseña), corta
    enseguida con ese mensaje. Si se acaba el tiempo, TimeoutError."""
    import time
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        if page.is_closed():
            raise RuntimeError("Se cerró la ventana de la Oficina Virtual antes de terminar el login.")
        if "/Dijkstra/VisorArchivos/Index" in page.url:
            return
        if "Login.aspx" in page.url:
            try:
                msg = (page.locator("#result").inner_text(timeout=500) or "").strip()
            except Exception:
                msg = ""
            if msg:
                raise RuntimeError(f"La Oficina Virtual no dejó entrar: {msg}")
        page.wait_for_timeout(500)
    raise TimeoutError


def _conectar(t: _TrabajadorOV, usuario: str, password: str, log, timeout_login: float) -> dict:
    from playwright.sync_api import sync_playwright

    from .whatsapp_web import SesionWhatsApp  # reusa la descarga automática de Chromium

    if t.usuario != usuario or not _viva(t):
        _cerrar(t)
        # Un perfil de Chrome por usuario de la OV: cada productor/organizador
        # mantiene su propia sesión y no se pisan entre sí.
        perfil = PERFILES_DIR / _nombre_seguro(usuario.lower())
        perfil.mkdir(parents=True, exist_ok=True)
        limpiar_lock_perfil(perfil)
        t._pw = sync_playwright().start()
        # Chrome normal (con ventana), pero arranca fuera de la pantalla y se
        # minimiza enseguida: no molesta mientras trabaja. Solo se muestra si
        # la OV pide algo que hay que resolver a mano (ver _mostrar_ventana).
        kwargs = dict(user_data_dir=str(perfil), headless=False,
                      viewport={"width": 1200, "height": 820}, accept_downloads=True,
                      args=["--window-position=-32000,-32000"])
        try:
            t.ctx = t._pw.chromium.launch_persistent_context(**kwargs)
        except Exception as e:
            if "Executable doesn't exist" not in str(e):
                raise
            t._pw.stop()
            SesionWhatsApp(log=log)._instalar_chromium()
            t._pw = sync_playwright().start()
            t.ctx = t._pw.chromium.launch_persistent_context(**kwargs)
        t.page = t.ctx.pages[0] if t.ctx.pages else t.ctx.new_page()
        t.usuario = usuario
        _esconder_ventana(t)

    page = t.page
    log("Entrando a la Oficina Virtual…")
    page.goto(URL_VISOR, wait_until="domcontentloaded", timeout=60000)

    if "/Dijkstra/VisorArchivos/Index" not in page.url:
        # La sesión guardada en el perfil ya no sirve: login de nuevo.
        if not password:
            raise ValueError("Falta la contraseña de la Oficina Virtual.")
        page.wait_for_selector(SEL_USUARIO, timeout=30000)
        page.fill(SEL_USUARIO, usuario)
        page.fill(SEL_PASSWORD, password)
        log("Iniciando sesión…")
        page.click(SEL_INGRESAR, timeout=10000)
        try:
            # 60s, no 25: un login con verificación invisible (reCAPTCHA v3)
            # contra un servidor real puede tardar bastante sin que haga
            # falta ninguna ayuda humana — mostrar la ventana antes de tiempo
            # era la causa de que "pida verificación" mucho más seguido de lo
            # que en realidad hace falta.
            _esperar_login(page, 60)
        except TimeoutError:
            # Sin error visible y sin entrar: la OV quedó esperando algo (ej.
            # el desafío del captcha). Se muestra la ventana para resolverlo.
            _mostrar_ventana(t)
            log("La Oficina Virtual pide una verificación: resolvela en la ventana que se abrió.")
            try:
                _esperar_login(page, timeout_login)
            except TimeoutError as e:
                raise RuntimeError("No se completó el login. Revisá usuario y contraseña y volvé a "
                                   "tocar Conectar.") from e
            finally:
                _esconder_ventana(t)
        page.wait_for_load_state("domcontentloaded")

    arbol = page.evaluate("() => (typeof arbolProductores !== 'undefined') ? arbolProductores : null")
    productores = aplanar_arbol(arbol or [])
    if not productores:
        productores = _productores_de_select(page)
    log(f"Conectado a la OV como {usuario}: {len(productores)} productores disponibles.")
    return {"usuario": usuario, "productores": productores}


def _post_getfiles(t: _TrabajadorOV, agentes: list[str], pagina: int, filas: int) -> dict:
    resp = t.page.request.post(
        URL_GETFILES,
        data=cuerpo_getfiles(agentes, pagina, filas),
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": URL_VISOR,
        },
        timeout=120000,
    )
    if not resp.ok:
        raise RuntimeError(f"La OV respondió {resp.status} al listar archivos.")
    try:
        return resp.json()
    except Exception as e:
        raise RuntimeError("La sesión de la OV venció. Volvé a conectar.") from e


def _listar(t: _TrabajadorOV, agentes: list[str], desde: date, log) -> list[ArchivoOV]:
    if not _viva(t):
        raise RuntimeError("No hay sesión abierta con la OV. Tocá Conectar primero.")
    archivos: list[ArchivoOV] = []
    filas_por_pagina = 500
    pagina = 1
    while True:
        datos = _post_getfiles(t, agentes, pagina, filas_por_pagina)
        filas = datos.get("rows") or []
        mas_viejo = None
        for f in filas:
            a = parsear_fila(f)
            try:
                publicado = datetime.strptime(f["cell"][1].strip(), "%d/%m/%Y").date()
            except Exception:
                publicado = None
            if publicado:
                mas_viejo = publicado
            # Esta página trae 500 archivos de golpe: la última que se pide
            # casi siempre se pasa del corte "desde" con archivos de hace
            # años (con nombres viejos, tipo "Rechazos de CBU" en vez de
            # "Rechazos CBU" — sin este filtro terminaban mezclados con los
            # recientes y aparecían como si fueran otro medio distinto).
            if a and (publicado is None or publicado >= desde):
                archivos.append(a)
        total_paginas = int(datos.get("total") or 1)
        log(f"Buscando archivos en la OV… ({len(archivos)} revisados)")
        # Vienen ordenados por fecha de publicación, de nuevo a viejo: cuando
        # la página ya llega a antes del rango que interesa, no hace falta
        # seguir (la sección tiene miles de archivos de años anteriores).
        if not filas or pagina >= total_paginas or (mas_viejo and mas_viejo < desde):
            break
        pagina += 1
    return archivos


def _descargar_como_navegador(t: _TrabajadorOV, url: str) -> bytes | None:
    pagina = t.ctx.new_page()
    try:
        with pagina.expect_download(timeout=60000) as info:
            try:
                pagina.goto(url, referer=URL_VISOR, timeout=60000)
            except Exception:
                pass  # una descarga corta la navegación con error: es lo esperado
        return Path(info.value.path()).read_bytes()
    except Exception:
        return None
    finally:
        try:
            pagina.close()
        except Exception:
            pass


def _descargar_uno(t: _TrabajadorOV, a: ArchivoOV, carpeta: Path) -> str:
    url = f"{URL_VIEWFILE}?key={a.key}"
    resp = t.page.request.get(url, headers={"Referer": URL_VISOR}, timeout=180000)
    if resp.ok:
        cuerpo = resp.body()
    else:
        # Segundo intento como lo haría una persona: abrir el link en una
        # pestaña nueva del mismo Chrome (con la sesión de la OV).
        cuerpo = _descargar_como_navegador(t, url)
        if cuerpo is None:
            raise RuntimeError(f"La OV respondió {resp.status} al bajar '{a.nombre}' (productor {a.agente}).")
    inicio = cuerpo[:4000].lower()
    if b"smgstspasivo" in inicio or b"login.aspx" in inicio:
        raise RuntimeError("La sesión de la OV venció. Volvé a conectar.")
    if a.extension == "pdf" and not cuerpo.startswith(b"%PDF"):
        raise RuntimeError(f"La OV no devolvió un PDF para '{a.nombre}'.")
    # El visor muestra ícono "xls" pero los rechazos llegan como .xlsx: la
    # extensión se decide por el contenido real, no por el ícono.
    if cuerpo.startswith(b"PK"):
        ext = "xlsx"
    elif cuerpo.startswith(b"\xd0\xcf\x11\xe0"):
        ext = "xls"
    elif cuerpo.startswith(b"%PDF"):
        ext = "pdf"
    else:
        ext = a.extension or "bin"
    destino = (carpeta / nombre_local(a)).with_suffix("." + ext)
    destino.write_bytes(cuerpo)
    return str(destino)


# ──────────────────────────────────────────────────────────────
#  API usada por server.py
# ──────────────────────────────────────────────────────────────

_ultimo_listado: dict = {}


def conectar(usuario: str, password: str, log, timeout_login: float = 180) -> dict:
    usuario = (usuario or "").strip()
    if not usuario:
        raise ValueError("Falta el usuario de la Oficina Virtual.")
    return _t().ejecutar(_conectar, usuario, password or "", log, timeout_login)


DIAS_HACIA_ATRAS = 120


def buscar(agentes: list[str], log) -> dict:
    """Lista las tandas de rechazos de esos productores publicadas en los
    últimos meses (se trae un poco más para encontrar la tanda anterior de
    cada una)."""
    if not agentes:
        raise ValueError("Elegí al menos un productor.")
    desde = date.fromordinal(date.today().toordinal() - DIAS_HACIA_ATRAS)
    archivos = _t().ejecutar(lambda t: _listar(t, agentes, desde, log))
    _ultimo_listado.clear()
    _ultimo_listado.update({"agentes": list(agentes), "archivos": archivos})
    tandas = armar_tandas(archivos)
    for t in tandas:
        sel = clasificar(archivos, [t["clave"]])
        t["cuponeras"] = len(sel["cuponeras"])
        t["rechazos"] = len(sel["mes_actual"])
    # Contra qué fechas se compararía cada tanda sola (la interfaz lo muestra).
    for t in tandas:
        t["anteriores"] = sorted({a.fecha for a in anteriores(archivos, [(t["medio"], t["fecha"])])}, reverse=True)
    log(f"{len(tandas)} tandas de rechazos encontradas.")
    return {"tandas": tandas, "medios": sorted({t["medio"] for t in tandas})}


def descargar(agentes: list[str], tandas: list[str], carpeta_trabajo: str, log,
              incluir_mes_pasado: bool = True) -> dict:
    """Baja las tandas elegidas y las deja listas para las 4 tarjetas de Inicio."""
    if not tandas:
        raise ValueError("Elegí al menos una tanda.")
    if _ultimo_listado.get("agentes") != list(agentes):
        buscar(agentes, log)
    archivos = _ultimo_listado["archivos"]
    sel = clasificar(archivos, tandas)
    if not incluir_mes_pasado:
        # Modo "avisar a todos los rechazos del mes": el mes anterior no se usa.
        sel["mes_pasado"] = []
    if not sel["mes_actual"]:
        raise ValueError("Esas tandas no tienen archivos de rechazos para los productores elegidos.")

    sello = datetime.now().strftime("%Y-%m-%d_%H%M")
    carpeta = Path(carpeta_trabajo) / "OV" / sello
    carpeta.mkdir(parents=True, exist_ok=True)
    total = sum(len(v) for v in sel.values())
    hechos = 0
    rutas: dict[str, list[str]] = {k: [] for k in sel}
    fallidos: list[str] = []
    for grupo, lista in sel.items():
        for a in lista:
            hechos += 1
            log(f"Descargando de la OV… {hechos}/{total}: {a.nombre} (productor {a.agente})")
            try:
                rutas[grupo].append(_t().ejecutar(_descargar_uno, a, carpeta))
            except Exception as e:
                # Sin un listado del mes elegido no hay nada que procesar: eso
                # sí corta. Uno del mes anterior o una cuponera que la OV no
                # entrega se saltea y se avisa al final.
                if grupo == "mes_actual" or "venció" in str(e):
                    raise
                fallidos.append(f"{a.nombre} (productor {a.agente})")
                log(f"No se pudo bajar {a.nombre} (productor {a.agente}): se saltea.")

    mes_actual = unir_excels(rutas["mes_actual"], str(carpeta / "rechazos_actual.xlsx"))
    mes_pasado = (unir_excels(rutas["mes_pasado"], str(carpeta / "rechazos_anterior.xlsx"))
                  if rutas["mes_pasado"] else "")
    log(f"Listo: {len(rutas['mes_actual'])} listados de rechazos, "
        f"{len(rutas['mes_pasado'])} de la tanda anterior y {len(rutas['cuponeras'])} cuponeras.")
    return {
        "mes_actual": mes_actual,
        "mes_pasado": mes_pasado,
        "carpeta_pdfs": ";".join(rutas["cuponeras"]),
        "cantidades": {k: len(v) for k, v in rutas.items()},
        "fallidos": fallidos,
        "carpeta": str(carpeta),
    }


def desconectar() -> dict:
    if _trabajador is not None:
        _t().ejecutar(_cerrar)
    return {"ok": True}
