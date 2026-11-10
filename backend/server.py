"""Backend de AlDía: la misma lógica de negocio que ya está probada
(core/*.py), expuesta por stdin/stdout en líneas JSON.

Por qué así: Tauri (Rust) no tiene un Playwright maduro para manejar
WhatsApp Web, y reescribir el recorte de PDFs / cruce de Excel a mano en
Rust tiraría a la basura lo que ya está verificado contra datos reales.
Así que la interfaz (ventana, sidebar, animaciones) es Rust + HTML/CSS/JS,
pero el trabajo pesado lo sigue haciendo este proceso Python, como un
"sidecar" que Rust arranca y con el que habla por stdio.

Protocolo: una línea = un pedido. Entrada: {"id": N, "cmd": "...", "args": {...}}
Salida:  {"id": N, "ok": true, "data": ...}  o  {"id": N, "ok": false, "error": "..."}
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import tempfile
import threading
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# En Windows, stdout/stdin heredan la codificación de la consola (CP1252 o
# similar) si no se los fuerza a UTF-8: cualquier tilde o ñ que cruce el
# puente con Rust sale rota. Esto lo deja fijo pase lo que pase.
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd  # noqa: E402

from app_config import load_config, save_config  # noqa: E402
from core.cortador import fragmentar_pdfs  # noqa: E402
from core.filtrar import filtrar_rechazos  # noqa: E402
from core.historial import cargar_historial, ruta_historial  # noqa: E402
from core.utils import find_column  # noqa: E402
from core.simulador import DEFAULT_PLANTILLA_MENSAJE, simular_agrupacion  # noqa: E402
from core.telefonos import cruzar_telefonos  # noqa: E402
from core.whatsapp_web import PERFIL_POR_DEFECTO, SesionWhatsApp, enviar_whatsapp_web  # noqa: E402


# Cada pedido se procesa en su propio hilo (ver main()): sin esto, un
# comando largo (conectar/enviar WhatsApp) dejaría encolado detrás cualquier
# otro pedido simple (cargar config, listar clientes...) hasta que termine.
# Como varios hilos pueden escribir una línea a la vez, todas las salidas
# por stdout pasan por este lock para que no se entrevere el JSON.
_stdout_lock = threading.Lock()


def _emitir_evento(tipo: str, datos: dict) -> None:
    """Línea suelta (sin "id"): no es la respuesta a nada en particular, es
    progreso en vivo mientras un comando largo (enviar WhatsApp) sigue
    corriendo. Rust la reenvía a la interfaz como evento en caliente."""
    with _stdout_lock:
        sys.stdout.write(json.dumps({"event": tipo, "data": datos}, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _log_callback(mensaje: str) -> None:
    _emitir_evento("log", {"message": mensaje})


def _qr_callback(png_bytes) -> None:
    """Nunca se abre una ventana de navegador (headless): el QR, cuando hace
    falta, se manda como imagen a la interfaz en vez de mostrarse en pantalla."""
    import base64
    datos = base64.b64encode(png_bytes).decode("ascii") if png_bytes else None
    _emitir_evento("qr", {"image_base64": datos})
from core.vincular import vincular_pdfs  # noqa: E402


def _carpeta_trabajo_por_defecto() -> Path:
    docs = Path.home() / "Documents"
    base = docs if docs.is_dir() else Path.home()
    carpeta = base / "AlDía"
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta


def cmd_workspace_dir(args: dict) -> dict:
    return {"carpeta": str(_carpeta_trabajo_por_defecto())}


def _estado_archivo(ruta: str) -> dict:
    """Uno o varios paths separados por ';' (así se guardan hoy varios PDF)."""
    partes = [p.strip() for p in (ruta or "").split(";") if p.strip()]
    validas = [p for p in partes if Path(p).exists()]
    return {
        "cargado": bool(validas),
        "cantidad": len(validas),
        "nombre": Path(validas[0]).name if len(validas) == 1 else None,
    }


def cmd_check_files(args: dict) -> dict:
    """Estado de los 4 archivos de entrada, para pintar las tarjetas."""
    claves = ("base_clientes", "carpeta_pdfs", "mes_pasado", "mes_actual")
    return {clave: _estado_archivo(args.get(clave, "")) for clave in claves}


# ──────────────────────────────────────────────────────────────
#  Flujo: detectar -> fragmentar -> vincular -> cruzar teléfonos
# ──────────────────────────────────────────────────────────────

def cmd_detectar_rechazos(args: dict) -> dict:
    mes_pasado = args.get("mes_pasado") or None
    incluir_todos = bool(args.get("incluir_todos"))
    destino = Path(args["destino"])
    resumen = filtrar_rechazos(
        mes_pasado,
        args["mes_actual"],
        salida_segundo=str(destino.with_name("01_rechazos.xlsx")),
        salida_primero=str(destino.with_name("01_primer_rechazo.xlsx")),
        incluir_todos=incluir_todos,
    )
    return {
        "archivo": resumen["salida_segundo"],
        "archivo_1ros": resumen["salida_primero"],
        "total_actual": resumen["total_actual"],
        "segundo_rechazo": resumen["segundo_rechazo"],
        "primer_rechazo": resumen["primer_rechazo"],
        "enviados_base": resumen["enviados_base"],
    }


def cmd_fragmentar_pdfs(args: dict) -> dict:
    resumen = fragmentar_pdfs(args["carpeta_pdfs"], args["destino"])
    return {"carpeta": resumen["salida_dir"], "cantidad": resumen["generados"]}


def cmd_vincular_cupones(args: dict) -> dict:
    resumen = vincular_pdfs(args["excel_rechazos"], args["destino"], args["carpeta_cupones"])
    return {"archivo": resumen["salida"], "encontrados": resumen["encontrados"], "no_encontrados": resumen["no_encontrados"]}


def cmd_cruzar_telefonos(args: dict) -> dict:
    resumen = cruzar_telefonos(args["excel_vinculado"], args["base_clientes"], args["destino"])
    return {
        "archivo": resumen["salida"],
        "cantidad": resumen["total"],
        "con_telefono": resumen["con_telefono"],
        "sin_telefono": resumen["sin_telefono"],
    }


def cmd_simular_agrupacion(args: dict) -> dict:
    return simular_agrupacion(args["excel_entrada"])


# ──────────────────────────────────────────────────────────────
#  Lista de clientes
# ──────────────────────────────────────────────────────────────

def _archivo_anulados(excel_path: str) -> Path:
    return Path(excel_path).with_name(".anulados.json")


def _cargar_anulados(excel_path: str) -> set:
    ruta = _archivo_anulados(excel_path)
    if not ruta.exists():
        return set()
    try:
        return set(json.loads(ruta.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _guardar_anulados(excel_path: str, anulados: set) -> None:
    _archivo_anulados(excel_path).write_text(
        json.dumps(sorted(anulados), ensure_ascii=False), encoding="utf-8"
    )


def _normalizar(texto: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", texto) if not unicodedata.combining(c)).lower()


def _fila_id(fila: dict) -> str:
    """Identificador estable de una fila: póliza si hay, si no documento.

    El nombre exacto de la columna varía según de dónde venga el Excel
    ("POLIZA" en los de prueba, "Póliza Nro." en los reales de la
    aseguradora), así que busca por coincidencia parcial en vez de pedir
    el nombre exacto.
    """
    for termino in ("poliza", "documento", "cuit"):
        for clave, valor in fila.items():
            if termino in _normalizar(clave) and valor not in (None, ""):
                return str(valor)
    return str(hash(tuple(fila.items())))


def cmd_listar_clientes(args: dict) -> dict:
    """Lee el Excel final (post cruce de teléfonos) y lo devuelve como filas.

    `excel_path` es cualquiera de los tres (final, 1ros o 2dos): la tabla
    de Lista de clientes alterna entre esas vistas.
    """
    ruta = args.get("excel_path") or ""
    if not ruta or not Path(ruta).exists():
        return {"columnas": [], "filas": [], "total": 0}

    # dtype=str: sin esto, pandas "ayuda" convirtiendo el teléfono
    # ("+5492262559559", texto) en número al leerlo — se come el + y agrega
    # un .0 al final.
    df = pd.read_excel(ruta, dtype=str)
    df.columns = df.columns.astype(str).str.strip()
    anulados = _cargar_anulados(ruta)

    filas = []
    for _, fila in df.iterrows():
        registro = {col: ("" if pd.isna(v) else str(v)) for col, v in fila.items()}
        registro["_id"] = _fila_id(registro)
        registro["_anulada"] = registro["_id"] in anulados
        filas.append(registro)

    return {"columnas": list(df.columns), "filas": filas, "total": len(filas)}


def cmd_anular_poliza(args: dict) -> dict:
    ruta = args["excel_path"]
    fila_id = str(args["fila_id"])
    anulados = _cargar_anulados(ruta)
    if args.get("anular", True):
        anulados.add(fila_id)
    else:
        anulados.discard(fila_id)
    _guardar_anulados(ruta, anulados)
    return {"anulados": sorted(anulados)}


def cmd_actualizar_telefono(args: dict) -> dict:
    """Corrige a mano el teléfono de una fila (la base de clientes a veces
    tiene uno viejo o mal cargado) y lo deja escrito en el Excel mismo, que
    es de donde lee tanto esta tabla como el envío real de WhatsApp."""
    ruta = args["excel_path"]
    fila_id = str(args["fila_id"])
    nuevo = str(args.get("telefono") or "").strip()
    if not Path(ruta).exists():
        raise FileNotFoundError(f"No encontré '{ruta}'")

    df = pd.read_excel(ruta, dtype=str)  # mismo motivo que en listar_clientes
    df.columns = df.columns.astype(str).str.strip()
    col_tel = find_column(df, "TELEFONO", "TELÉFONO", "CEL", "MOVIL", "MÓVIL")
    if not col_tel:
        raise ValueError("No encontré columna de teléfono en este Excel.")

    fila_idx = None
    for i, fila in df.iterrows():
        registro = {col: ("" if pd.isna(v) else str(v)) for col, v in fila.items()}
        if _fila_id(registro) == fila_id:
            fila_idx = i
            break
    if fila_idx is None:
        raise ValueError("No encontré esa fila (¿se volvió a generar el Excel?).")

    import openpyxl
    wb = openpyxl.load_workbook(ruta)
    ws = wb.active
    encabezados = [str(c.value).strip() if c.value else c.value for c in ws[1]]
    col_num = encabezados.index(col_tel) + 1
    ws.cell(row=fila_idx + 2, column=col_num, value=nuevo)
    wb.save(ruta)

    return {"ok": True, "telefono": nuevo}


def cmd_vista_previa_mensaje(args: dict) -> dict:
    nombre = args.get("nombre") or "Cliente"
    polizas = args.get("polizas") or []
    if isinstance(polizas, str):
        polizas = [p.strip() for p in polizas.split(",") if p.strip()]
    plantilla = args.get("plantilla") or DEFAULT_PLANTILLA_MENSAJE
    str_polizas = ", ".join(f"*{p}*" for p in polizas) if polizas else "*(sin póliza)*"
    palabra_poliza = "tus pólizas" if len(polizas) > 1 else "tu póliza"
    palabra_cupon = "los cupones" if len(polizas) > 1 else "el cupón"
    try:
        texto = plantilla.format(
            nombre=nombre, polizas=str_polizas,
            palabra_poliza=palabra_poliza, palabra_cupon=palabra_cupon,
            cupones=palabra_cupon,
        )
    except Exception:
        texto = plantilla
    return {"mensaje": texto}


def cmd_imprimir_listado(args: dict) -> dict:
    """Exporta el listado a HTML y lo abre con el programa default (sin
    depender de Qt: el backend corre sin interfaz, QPrintDialog no sirve)."""
    ruta = args["excel_path"]
    titulo = args.get("titulo") or "Listado"
    df = pd.read_excel(ruta)
    df.columns = df.columns.astype(str).str.strip()

    esc = lambda v: html.escape(str(v))
    encabezado = "".join(f"<th>{esc(c)}</th>" for c in df.columns)
    filas_html = "".join(
        "<tr>" + "".join(f"<td>{esc(v)}</td>" for v in fila) + "</tr>"
        for fila in df.itertuples(index=False)
    )
    documento = f"""<html><head><meta charset="utf-8">
    <style>
      body {{ font-family: Arial, sans-serif; }}
      h2 {{ text-align: center; }}
      table {{ border-collapse: collapse; width: 100%; }}
      th, td {{ border: 1px solid #999; padding: 4px 8px; font-size: 12px; }}
      th {{ background: #eee; }}
    </style></head><body>
    <h2>{esc(titulo)}</h2>
    <table><thead><tr>{encabezado}</tr></thead><tbody>{filas_html}</tbody></table>
    </body></html>"""

    tmp = Path(tempfile.gettempdir()) / f"aldia_listado_{os.getpid()}.html"
    tmp.write_text(documento, encoding="utf-8")
    if sys.platform == "win32":
        os.startfile(str(tmp))  # noqa: S606
    elif sys.platform == "darwin":
        os.system(f'open "{tmp}"')
    else:
        os.system(f'xdg-open "{tmp}"')
    return {"archivo": str(tmp)}


# ──────────────────────────────────────────────────────────────
#  Historial
# ──────────────────────────────────────────────────────────────

def cmd_historial_listar(args: dict) -> dict:
    archivo = args.get("archivo") or None
    historial = cargar_historial(archivo)
    filas = []
    for clave, datos in historial.items():
        filas.append({
            "clave": clave,
            "cliente": datos.get("cliente", ""),
            "telefono": datos.get("telefono", ""),
            "polizas": ", ".join(datos.get("polizas") or []),
            "estado": datos.get("estado", ""),
            "fecha": datos.get("fecha", ""),
        })
    filas.sort(key=lambda f: f["fecha"], reverse=True)
    return {"filas": filas, "total": len(filas), "archivo": str(ruta_historial(archivo))}


# ──────────────────────────────────────────────────────────────
#  WhatsApp
# ──────────────────────────────────────────────────────────────

def cmd_whatsapp_probar(args: dict) -> dict:
    """Abre la sesión (reutiliza el login guardado; si no hay, abre el
    navegador para escanear el QR) y la vuelve a cerrar. Sirve para
    verificar que el envío va a andar, sin mandar nada todavía."""
    perfil = args.get("perfil_dir") or str(PERFIL_POR_DEFECTO)
    _emitir_evento("log", {"message": "Abriendo WhatsApp Web…"})
    sesion = SesionWhatsApp(perfil_dir=perfil, headless=True, log=_log_callback, on_qr=_qr_callback)
    try:
        sesion.abrir(timeout_login=args.get("timeout_login", 180))
    finally:
        sesion.cerrar()
        _qr_callback(None)
    return {"conectado": True}


def cmd_whatsapp_enviar_todos(args: dict) -> dict:
    """El paso 5: manda el WhatsApp con el cupón adjunto a cada cliente del
    Excel ya cruzado con teléfonos. Una sola sesión para todos los envíos."""
    try:
        resumen = enviar_whatsapp_web(
            excel_entrada=args["excel_entrada"],
            log=_log_callback,
            telefono_por_defecto=args.get("telefono_por_defecto") or "+5492494585215",
            historial_file=args.get("historial_file") or None,
            perfil_dir=args.get("perfil_dir") or str(PERFIL_POR_DEFECTO),
            headless=True,
            on_qr=_qr_callback,
            timeout_login=args.get("timeout_login", 180),
            plantilla_mensaje=args.get("plantilla_mensaje") or None,
        )
    finally:
        _qr_callback(None)
    return resumen


HANDLERS = {
    "ping": lambda args: {"pong": True},
    "load_config": lambda args: load_config(),
    "save_config": lambda args: (save_config(args) or {"ok": True}),
    "check_files": cmd_check_files,
    "workspace_dir": cmd_workspace_dir,
    "detectar_rechazos": cmd_detectar_rechazos,
    "fragmentar_pdfs": cmd_fragmentar_pdfs,
    "vincular_cupones": cmd_vincular_cupones,
    "cruzar_telefonos": cmd_cruzar_telefonos,
    "simular_agrupacion": cmd_simular_agrupacion,
    "listar_clientes": cmd_listar_clientes,
    "anular_poliza": cmd_anular_poliza,
    "actualizar_telefono": cmd_actualizar_telefono,
    "vista_previa_mensaje": cmd_vista_previa_mensaje,
    "imprimir_listado": cmd_imprimir_listado,
    "historial_listar": cmd_historial_listar,
    "whatsapp_probar": cmd_whatsapp_probar,
    "whatsapp_enviar_todos": cmd_whatsapp_enviar_todos,
    "plantilla_default": lambda args: {"plantilla": DEFAULT_PLANTILLA_MENSAJE},
}


def _procesar_pedido(pedido: dict) -> None:
    pedido_id = pedido.get("id")
    try:
        handler = HANDLERS.get(pedido["cmd"])
        if handler is None:
            raise ValueError(f"comando desconocido: {pedido['cmd']}")
        datos = handler(pedido.get("args") or {})
        respuesta = {"id": pedido_id, "ok": True, "data": datos}
    except Exception as exc:  # el frontend necesita el motivo, no un crash mudo
        respuesta = {
            "id": pedido_id,
            "ok": False,
            "error": str(exc),
            "trace": traceback.format_exc(),
        }
    with _stdout_lock:
        sys.stdout.write(json.dumps(respuesta, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def main() -> None:
    for linea in sys.stdin:
        linea = linea.strip()
        if not linea:
            continue
        try:
            pedido = json.loads(linea)
        except Exception:
            continue
        threading.Thread(target=_procesar_pedido, args=(pedido,), daemon=True).start()


if __name__ == "__main__":
    main()
