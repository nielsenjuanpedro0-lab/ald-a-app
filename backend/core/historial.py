"""Historial permanente de envíos de WhatsApp.

El checkpoint viejo (`04_enviados.json`) vivía dentro de la carpeta de trabajo,
que por defecto es el Temp de Windows: el sistema la limpia sola y cada vez que
se cambiaba la carpeta el historial arrancaba de cero, así que la app volvía a
mandarle el mensaje a gente que ya lo había recibido.

Este módulo guarda el historial en la carpeta del usuario, fuera de Temp, y usa
una clave que incluye el período: un rechazo de septiembre y uno de octubre del
mismo cliente son eventos distintos y ambos se notifican, pero correr dos veces
el mismo mes no duplica nada.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

CARPETA_HISTORIAL = Path.home() / ".rechazos_app"
ARCHIVO_HISTORIAL = CARPETA_HISTORIAL / "historial_envios.json"


def ruta_historial(archivo: str | Path | None = None) -> Path:
    return Path(archivo) if archivo else ARCHIVO_HISTORIAL


def periodo_actual() -> str:
    return datetime.now().strftime("%Y-%m")


def _solo_digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or "")).lstrip("0")


def construir_clave(cuit, polizas, periodo: str | None = None, cliente: str = "") -> str:
    """Clave estable de un evento de rechazo: quién + qué pólizas + qué mes.

    Ojo: la clave vieja era CUIT_NOMBRE. El nombre varía entre corridas (venía
    con el apellido duplicado en varios casos), así que dos corridas generaban
    claves distintas para la misma persona.
    """
    periodo = periodo or periodo_actual()
    ident = _solo_digitos(cuit)
    if not ident:
        ident = re.sub(r"\s+", " ", str(cliente or "")).strip().upper()
    pols = sorted({_solo_digitos(p) for p in (polizas or []) if _solo_digitos(p)})
    return f"{ident}|{'-'.join(pols)}|{periodo}"


def cargar_historial(archivo: str | Path | None = None) -> dict:
    ruta = ruta_historial(archivo)
    if not ruta.exists():
        return {}
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    # Tolerar el formato viejo (lista de claves) por si alguien lo importó.
    if isinstance(data, list):
        return {clave: {} for clave in data}
    return data if isinstance(data, dict) else {}


def guardar_historial(historial: dict, archivo: str | Path | None = None):
    ruta = ruta_historial(archivo)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta.with_suffix(".tmp")
    # Escritura atómica: si la app se cierra a mitad, no queda un JSON roto.
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(historial, f, ensure_ascii=False, indent=2)
    tmp.replace(ruta)


def registrar_envio(
    historial: dict,
    clave: str,
    cliente: str,
    telefono: str,
    polizas,
    estado: str,
    archivo: str | Path | None = None,
) -> dict:
    historial[clave] = {
        "cliente": cliente,
        "telefono": telefono,
        "polizas": list(polizas or []),
        "estado": estado,
        "fecha": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
    }
    guardar_historial(historial, archivo)
    return historial


def leer_checkpoint_viejo(checkpoint_file: str | Path) -> set:
    """Devuelve las claves `CUIT_NOMBRE` de un `04_enviados.json` viejo."""
    ruta_vieja = Path(checkpoint_file)
    if not ruta_vieja.exists():
        return set()
    try:
        with open(ruta_vieja, "r", encoding="utf-8") as f:
            claves = json.load(f)
    except Exception:
        return set()
    if isinstance(claves, list):
        return {str(c) for c in claves}
    if isinstance(claves, dict):
        return {str(c) for c in claves.keys()}
    return set()
