from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import unicodedata
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import sys

import pandas as pd

from .historial import (
    cargar_historial,
    construir_clave,
    periodo_actual,
    registrar_envio,
    ruta_historial,
)
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

MODIFIER = "command" if sys.platform == "darwin" else "ctrl"

# Marca que dejamos en el portapapeles antes de copiar. Si sigue ahí después de
# copiar, es que no había nada seleccionado (el cuadro de mensaje está vacío).
_SENTINELA = "__RECHAZOS_SIN_SELECCION__"

# Textos que muestra WhatsApp Web cuando el número no existe o está mal.
_MARCAS_NUMERO_INVALIDO = (
    "no es un numero de telefono valido",
    "numero de telefono compartido a traves de la url",
    "phone number shared via url is invalid",
    "no esta en whatsapp",
    "is not on whatsapp",
    "numero de telefono no valido",
    "invalid phone number",
)

# Estados posibles de un envío
ENVIADO_VERIFICADO = "ENVIADO Y VERIFICADO"
ENVIADO_SIN_VERIFICAR = "ENVIADO (SIN VERIFICAR)"
QUEDO_BORRADOR = "NO ENVIADO — QUEDÓ BORRADOR"
NUMERO_INVALIDO = "ERROR — NÚMERO INVÁLIDO EN WHATSAPP"


def _normalizar(texto: str) -> str:
    """Minúsculas, sin acentos y con espacios colapsados, para comparar."""
    texto = unicodedata.normalize("NFD", str(texto)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", texto).strip().lower()


def _clipboard_set_texto(texto: str):
    if os.name == "nt":
        # Vía archivo temporal: un -Value inline se rompe con comillas o saltos de línea.
        tmp = Path(tempfile.gettempdir()) / "_rechazos_clip.txt"
        tmp.write_text(texto, encoding="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            ["powershell", "-command", f"Get-Content -Raw -Encoding UTF8 '{tmp}' | Set-Clipboard"],
            check=True,
            creationflags=flags,
        )
    else:
        try:
            import pyperclip

            pyperclip.copy(texto)
        except Exception:
            process = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            process.communicate(texto.encode("utf-8"))


def _clipboard_get_texto() -> str:
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            out = subprocess.run(
                ["powershell", "-command", "Get-Clipboard -Raw"],
                capture_output=True,
                text=True,
                creationflags=flags,
                timeout=15,
            )
            return out.stdout or ""
        except Exception:
            return ""
    try:
        import pyperclip

        return pyperclip.paste() or ""
    except Exception:
        try:
            out = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=15)
            return out.stdout or ""
        except Exception:
            return ""


def _copiar_archivo_portapapeles(ruta: str):
    if os.name == "nt":
        # Nada de interpolar la ruta dentro de las comillas simples del
        # comando: un apellido con apóstrofe (D'Angelo, O'Connor) generaba un
        # PDF con ' en el nombre y rompía la sintaxis de PowerShell — el
        # adjunto le fallaba justo a esos clientes. Se pasa por variable de
        # entorno, así no hay nada para escapar.
        env = {**os.environ, "RECHAZOS_RUTA_ARCHIVO": ruta}
        subprocess.run(
            ["powershell", "-command", "Set-Clipboard -Path $env:RECHAZOS_RUTA_ARCHIVO"],
            check=True,
            env=env,
        )
    elif sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'set the clipboard to (POSIX file "{ruta}")'], check=True)
    else:
        # En Linux no hay forma estándar de poner un archivo en el portapapeles;
        # el motor Playwright no lo necesita porque adjunta por el input oculto.
        raise RuntimeError(
            "Adjuntar por portapapeles solo funciona en Windows y macOS. "
            "En Linux usá el motor Playwright (Configuración → Motor de envío)."
        )


def _leer_seleccion(pyautogui) -> str:
    """Selecciona todo donde esté el foco y devuelve lo copiado.

    Si el foco está en el cuadro de mensaje devuelve el borrador; si el cuadro
    está vacío devuelve "" (queda la sentinela y la descartamos). Si el foco
    está en la página, devuelve el texto de la página, que es lo que nos deja
    detectar la pantalla de número inválido.
    """
    _clipboard_set_texto(_SENTINELA)
    time.sleep(0.3)
    pyautogui.hotkey(MODIFIER, "a")
    time.sleep(0.3)
    pyautogui.hotkey(MODIFIER, "c")
    time.sleep(0.6)
    copiado = _clipboard_get_texto()
    # Soltar la selección para no pisar el texto con la próxima tecla.
    pyautogui.press("end")
    time.sleep(0.2)
    if copiado.strip() == _SENTINELA:
        return ""
    return copiado


def _es_pantalla_numero_invalido(texto: str) -> bool:
    t = _normalizar(texto)
    return any(marca in t for marca in _MARCAS_NUMERO_INVALIDO)


def enviar_whatsapp(
    excel_entrada: str,
    log=None,
    telefono_por_defecto: str = "+5492494585215",
    checkpoint_file: str = "04_enviados.json",
    historial_file: str | None = None,
    periodo: str | None = None,
    errores_dir: str = "errores_screenshot",
    espera_chat: float = 14,
    espera_adjunto: float = 5,
    espera_envios: float = 7,
    espera_cierre: float = 4,
    timeout_chat: float = 60,
    reintentos_enter: int = 3,
    modo_chat: str = "R",
    modo_adj: str = "R",
    reanudar: bool = True,
    noninteractive: bool = True,
    verificar_envio: bool = True,
    motor: str = "playwright",
    perfil_dir: str | None = None,
    check_cancel_cb=None,
    plantilla_mensaje: str | None = None,
):
    # Motor nuevo: una sola sesión de WhatsApp Web manejada desde código.
    # El viejo (pyautogui) queda como respaldo si Playwright no está instalado.
    if motor == "playwright":
        try:
            from .whatsapp_web import enviar_whatsapp_web
        except ImportError as e:
            emit(log, f"No pude cargar el motor Playwright ({e}). Sigo con el motor viejo.")
        else:
            try:
                return enviar_whatsapp_web(
                    excel_entrada=excel_entrada,
                    log=log,
                    telefono_por_defecto=telefono_por_defecto,
                    checkpoint_file=checkpoint_file,
                    historial_file=historial_file,
                    periodo=periodo,
                    errores_dir=errores_dir,
                    perfil_dir=perfil_dir,
                    plantilla_mensaje=plantilla_mensaje,
                    check_cancel_cb=check_cancel_cb,
                )
            except ImportError as e:
                # Playwright no instalado: avisamos y seguimos con el motor viejo.
                emit(log, f"⚠ {e}")
                emit(log, "Sigo con el motor viejo (más lento). Instalá Playwright para usar el rápido.")

    try:
        import pyautogui
    except ImportError as e:
        raise ImportError("Falta pyautogui para enviar por WhatsApp.") from e

    def espejo_checkpoint(historial: dict):
        """Copia del historial en la carpeta de trabajo, solo informativa.

        La fuente de verdad es el historial permanente; esto queda para poder
        mirar desde la carpeta de la corrida qué se mandó.
        """
        try:
            with open(checkpoint_file, "w", encoding="utf-8") as f:
                json.dump(sorted(historial.keys()), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def screenshot_error(cliente: str):
        try:
            Path(errores_dir).mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            nombre_seguro = re.sub(r'[\\/:*?"<>|]', "_", cliente)
            ruta = Path(errores_dir) / f"{ts}_{nombre_seguro}.png"
            pyautogui.screenshot(str(ruta))
            emit(log, f"Captura guardada: {ruta}")
        except Exception as e:
            emit(log, f"No se pudo capturar pantalla: {e}")

    if not Path(excel_entrada).exists():
        raise FileNotFoundError(f"No encontré '{excel_entrada}'")

    factor_chat = {"L": 2.0, "M": 1.5}.get(modo_chat.upper(), 1.0)
    factor_adj = {"L": 2.0, "M": 1.5}.get(modo_adj.upper(), 1.0)
    espera_chat = espera_chat * factor_chat
    espera_adjunto = espera_adjunto * factor_adj
    espera_envios = espera_envios * max(factor_chat, factor_adj)
    timeout_chat = max(timeout_chat * factor_chat, espera_chat + 20)

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
    grupos = df.groupby("_clave_agrupacion")

    periodo = periodo or periodo_actual()
    historial = cargar_historial(historial_file)
    ya_del_periodo = [k for k in historial if k.endswith(f"|{periodo}")]
    emit(log, f"Historial permanente: {ruta_historial(historial_file)}")
    emit(log, f"Ya notificados en el período {periodo}: {len(ya_del_periodo)} (total histórico: {len(historial)})")

    pyautogui.FAILSAFE = True

    exitos = 0
    nuevos_enviados = 0
    previamente_enviados = 0
    fallas = 0
    sin_verificar = 0
    borradores = 0
    numeros_invalidos = 0
    saltados = []
    para_revisar = []
    reporte_rows = []

    def esperar_composer(msg_texto: str) -> str:
        """Espera a que WhatsApp cargue el chat con el mensaje ya escrito.

        Devuelve 'LISTO', 'INVALIDO' o 'TIMEOUT'. Reemplaza el sleep fijo: antes
        se apretaba Enter a ciegas a los 14s y, si el chat todavía no había
        cargado, el mensaje quedaba como borrador.
        """
        msg_norm = _normalizar(msg_texto)
        limite = time.time() + timeout_chat
        # Un primer respiro antes de tocar nada: el navegador todavía está abriendo.
        time.sleep(min(espera_chat, 6.0))
        while time.time() < limite:
            copiado = _leer_seleccion(pyautogui)
            if _es_pantalla_numero_invalido(copiado):
                return "INVALIDO"
            if copiado and _normalizar(copiado) == msg_norm:
                return "LISTO"
            time.sleep(2.0)
        return "TIMEOUT"

    def sigue_en_borrador(msg_texto: str) -> bool:
        copiado = _leer_seleccion(pyautogui)
        return bool(copiado) and _normalizar(copiado) == _normalizar(msg_texto)

    for idx, (clave, filas) in enumerate(grupos, start=1):
        if check_cancel_cb:
            try:
                check_cancel_cb()
            except Exception as e:
                emit(log, f"Proceso pausado/detenido: {e}")
                break

        tel_crudo = str(filas.iloc[0].get(col_tel, "")).strip()
        tel = formatear_telefono(tel_crudo)
        cliente = limpiar_nombre_persona(str(filas.iloc[0].get(col_cli)))
        lista_polizas = [str_poliza(p) for p in filas[col_pol].unique()]
        pol_str = ", ".join(lista_polizas)
        now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

        cuit_val = str(filas.iloc[0].get(col_cuit, "")).strip()
        clave_hist = construir_clave(cuit_val, lista_polizas, periodo, cliente=cliente)

        def anotar(estado: str, telefono: str = ""):
            reporte_rows.append({
                "FECHA_HORA": now_str,
                "ASEGURADO": cliente,
                "POLIZAS": pol_str,
                "TELEFONO": telefono or tel or tel_crudo or "SIN TELEFONO",
                "ESTADO": estado,
            })

        pdfs_encontrados = []
        for p in lista_polizas:
            ruta = buscar_pdf_por_poliza(p, Path(excel_entrada).parent)
            if ruta and ruta not in pdfs_encontrados:
                pdfs_encontrados.append(ruta)
        primer_nombre = primer_nombre_persona(cliente)

        if clave_hist in historial:
            cuando = historial[clave_hist].get("fecha", "")
            detalle = "en una corrida anterior" if cuando in ("", "migrado") else f"el {cuando}"
            emit(log, f"[{idx}] Ya notificado {detalle} — saltando ({cliente})")
            exitos += 1
            previamente_enviados += 1
            anotar("SALTADO (YA NOTIFICADO)")
            continue

        if not es_telefono_valido_arg(tel):
            motivo = "sin teléfono" if not tel_crudo or tel_crudo.lower() in ("nan", "none", "") else f"teléfono inválido ({tel_crudo})"
            emit(log, f"[{idx}] ✗ {cliente} — {motivo}")
            saltados.append(f"{cliente} — {motivo}")
            para_revisar.append(f"{cliente} — {motivo}")
            fallas += 1
            anotar(f"ERROR — {motivo.upper()}", telefono=tel_crudo or "SIN TELEFONO")
            continue

        if not pdfs_encontrados:
            saltados.append(f"{cliente} — PDFs no encontrados")
            para_revisar.append(f"{cliente} — PDF no encontrado")
            fallas += 1
            anotar("ERROR — PDF NO ENCONTRADO")
            continue

        pestaña_abierta = False
        try:
            tel_url = tel.lstrip("+")
            msg_texto = generar_mensaje_unificado(primer_nombre, lista_polizas, plantilla=plantilla_mensaje)
            url = f"https://web.whatsapp.com/send?phone={tel_url}&text={quote(msg_texto)}"

            webbrowser.open(url)
            pestaña_abierta = True

            if not verificar_envio:
                estado_carga = "TIMEOUT"
                time.sleep(espera_chat)
            else:
                estado_carga = esperar_composer(msg_texto)

            if estado_carga == "INVALIDO":
                emit(log, f"[{idx}] ✗ {cliente} ({tel}) — WhatsApp dice que el número no existe")
                numeros_invalidos += 1
                fallas += 1
                saltados.append(f"{cliente} — número inexistente en WhatsApp ({tel})")
                para_revisar.append(f"{cliente} — número inexistente en WhatsApp ({tel})")
                anotar(NUMERO_INVALIDO)
                continue

            if estado_carga == "TIMEOUT" and verificar_envio:
                emit(log, f"[{idx}] ⚠ {cliente}: no pude confirmar que el chat cargó — igual intento enviar")

            # Enviar el texto, reintentando si el Enter no llegó a destino.
            pyautogui.press("enter")
            time.sleep(2.0)

            estado_texto = ENVIADO_SIN_VERIFICAR
            if verificar_envio:
                estado_texto = QUEDO_BORRADOR
                for intento in range(reintentos_enter):
                    if not sigue_en_borrador(msg_texto):
                        estado_texto = ENVIADO_VERIFICADO
                        break
                    emit(log, f"[{idx}] ⚠ {cliente}: el mensaje seguía en el cuadro — reintento {intento + 1}/{reintentos_enter}")
                    pyautogui.press("enter")
                    time.sleep(2.5)

            if estado_texto == QUEDO_BORRADOR:
                # No mandamos los adjuntos si el texto ni siquiera salió.
                emit(log, f"[{idx}] ✗ {cliente} ({tel}) — quedó como BORRADOR, no se envió")
                borradores += 1
                fallas += 1
                saltados.append(f"{cliente} — quedó en borradores")
                para_revisar.append(f"{cliente} — quedó en borradores ({tel})")
                anotar(QUEDO_BORRADOR)
                continue

            for pdf_ruta in pdfs_encontrados:
                _copiar_archivo_portapapeles(os.path.abspath(pdf_ruta))
                time.sleep(1)
                pyautogui.hotkey(MODIFIER, "v")
                time.sleep(espera_adjunto)
                for _ in range(8):
                    pyautogui.press("enter")
                    time.sleep(1.5)

            # Antes se cerraba la pestaña ~1s después del último Enter: si el PDF
            # todavía estaba subiendo, se cerraba con el adjunto sin enviar.
            time.sleep(espera_cierre)

            registrar_envio(
                historial,
                clave_hist,
                cliente=cliente,
                telefono=tel,
                polizas=lista_polizas,
                estado=estado_texto,
                archivo=historial_file,
            )
            espejo_checkpoint(historial)
            exitos += 1
            nuevos_enviados += 1
            anotar(estado_texto)
            if estado_texto == ENVIADO_VERIFICADO:
                emit(log, f"[{idx}] ✓ Enviado y verificado: {cliente} ({tel})")
            else:
                sin_verificar += 1
                para_revisar.append(f"{cliente} — enviado pero SIN verificar ({tel})")
                emit(log, f"[{idx}] ✓ Enviado (sin poder verificar): {cliente} ({tel})")
        except pyautogui.FailSafeException:
            emit(log, "Proceso detenido manualmente (FailSafe).")
            anotar("CANCELADO POR USUARIO (FAILSAFE)")
            break
        except Exception as e:
            screenshot_error(cliente)
            fallas += 1
            saltados.append(f"{cliente} — Error: {e}")
            para_revisar.append(f"{cliente} — Error: {e}")
            anotar(f"ERROR: {e}")
        finally:
            if pestaña_abierta:
                try:
                    time.sleep(1.0)
                    pyautogui.hotkey(MODIFIER, "w")
                except Exception:
                    pass
        time.sleep(espera_envios)

    if reporte_rows:
        try:
            df_rep = pd.DataFrame(reporte_rows)
            rep_path = Path(excel_entrada).parent / "04_reporte_envios.xlsx"
            df_rep.to_excel(rep_path, index=False)
            emit(log, f"📊 Reporte detallado guardado en: {rep_path.name}")
        except Exception as err_rep:
            emit(log, f"No se pudo guardar el reporte Excel: {err_rep}")

    if para_revisar:
        try:
            rev_path = Path(excel_entrada).parent / "04_para_revisar.json"
            with open(rev_path, "w", encoding="utf-8") as f:
                json.dump(para_revisar, f, ensure_ascii=False, indent=2)
            emit(log, f"⚠ {len(para_revisar)} casos para revisar a mano — detalle en {rev_path.name}")
        except Exception as err_rev:
            emit(log, f"No se pudo guardar la lista de revisión: {err_rev}")

    emit(log, f"Resumen — verificados: {nuevos_enviados - sin_verificar} · sin verificar: {sin_verificar} · borradores: {borradores} · números inválidos: {numeros_invalidos} · otras fallas: {fallas - borradores - numeros_invalidos}")

    return {
        "exitos": exitos,
        "nuevos_enviados": nuevos_enviados,
        "previamente_enviados": previamente_enviados,
        "fallas": fallas,
        "sin_verificar": sin_verificar,
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
