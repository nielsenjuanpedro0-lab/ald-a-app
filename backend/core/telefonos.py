from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from .utils import emit, find_column, detectar_header
from app_config import load_config


# Un celular argentino en formato internacional es +54 9 + 10 digitos
# (codigo de area de 2 a 4 digitos + abonado, siempre 10 en total).
RE_CELULAR_AR = re.compile(r"^\+549\d{10}$")


def formatear_telefono(tel) -> str | None:
    """Normaliza un telefono argentino a +549XXXXXXXXXX.

    Saca prefijos internacionales (00 / 54 / 549), el 0 de larga distancia y
    el 15 de celular. Si no queda un numero de 10 digitos devuelve None: es
    preferible marcarlo como faltante que mandarle el mensaje a un numero roto.
    """
    if tel is None or (isinstance(tel, float) and pd.isna(tel)):
        return None
    if pd.isna(tel):
        return None

    texto = str(tel).split(".")[0].strip()
    if not texto or texto.lower() in ("nan", "none", "0"):
        return None

    d = re.sub(r"\D", "", texto)
    if not d:
        return None

    # Prefijo de salida internacional (00 54 ...)
    if d.startswith("00"):
        d = d[2:]

    # Prefijo de pais y el 9 de celular
    if d.startswith("54"):
        d = d[2:]
        if d.startswith("9"):
            d = d[1:]

    # Prefijo 0 de larga distancia nacional
    d = d.lstrip("0")

    # Algunas bases guardan el 9 sin el 54 adelante
    if len(d) == 11 and d.startswith("9"):
        d = d[1:]

    # El 15 va despues del codigo de area (2, 3 o 4 digitos)
    if len(d) == 12:
        for largo_area in (2, 3, 4):
            if d[largo_area:largo_area + 2] == "15":
                d = d[:largo_area] + d[largo_area + 2:]
                break

    # 15 + abonado, sin codigo de area: no hay forma de saber de donde es
    if len(d) != 10:
        return None

    return "+549" + d


def es_telefono_valido_arg(tel: str | None) -> bool:
    if not tel:
        return False
    return bool(RE_CELULAR_AR.match(str(tel).strip()))


def cruzar_telefonos(
    listado: str,
    base_clientes: str,
    salida: str = "listado_con_telefonos.xlsx",
    telefono_por_defecto: str | None = None,
    usar_telefono_defecto: bool = False,
    log=None,
):
    if not telefono_por_defecto:
        telefono_por_defecto = load_config().get("telefono_defecto") or "+5492494585215"
    if not Path(listado).exists():
        raise FileNotFoundError(f"No encontré '{listado}'")
    if not Path(base_clientes).exists():
        raise FileNotFoundError(f"No encontré '{base_clientes}'")

    df_raw = pd.read_excel(listado, header=None, nrows=20)
    header_row = detectar_header(df_raw, "CUIT")
    df = pd.read_excel(listado, header=header_row)
    df.columns = df.columns.str.strip()
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]

    col_cuit = find_column(df, "CUIT", "CUIL")
    col_tel = find_column(df, "TELEFONO", "TELÉFONO", "CEL", "MOVIL", "MÓVIL")
    col_cli = find_column(df, "CLIENTE", "ASEGURADO")
    if not col_cuit:
        raise ValueError("No encontré columna CUIT/CUIL en el listado.")

    # Sin engine fijo: pandas elige xlrd para .xls y openpyxl para .xlsx
    df_base_raw = pd.read_excel(base_clientes, header=None, nrows=20)
    header_row_base = detectar_header(df_base_raw, "CUIT")
    df_base = pd.read_excel(base_clientes, header=header_row_base)
    df_base.columns = df_base.columns.str.strip()
    col_cuit_base = find_column(df_base, "CUIT", "NRO_CUIT", "CUIL")
    if not col_cuit_base:
        raise ValueError("No encontré columna CUIT en la base de clientes.")

    cols_tel_base = []
    for prioridad in ["MOVIL", "MÓVIL", "CEL", "FIJO", "ALTERNATIVO", "TEL"]:
        for col in df_base.columns:
            if prioridad in col.upper().replace("Ó", "O") and col not in cols_tel_base:
                cols_tel_base.append(col)

    df_base["_cuit_key"] = df_base[col_cuit_base].astype(str).str.split(".").str[0].str.strip()

    def elegir_tel_base(fila):
        for col in cols_tel_base:
            tel = formatear_telefono(fila.get(col))
            if es_telefono_valido_arg(tel):
                return tel
        return None

    df_base["_tel_base"] = df_base.apply(elegir_tel_base, axis=1)
    lookup = df_base.set_index("_cuit_key")["_tel_base"].to_dict()

    df["_cuit_key"] = df[col_cuit].astype(str).str.split(".").str[0].str.strip()

    sin_telefono = []

    def resolver_tel(fila):
        cliente = str(fila.get(col_cli, "")).strip() if col_cli else str(fila.get(col_cuit, ""))
        crudo = fila.get(col_tel, "") if col_tel else ""

        tel_lista = formatear_telefono(crudo)
        if es_telefono_valido_arg(tel_lista):
            return tel_lista
        if str(crudo).strip() and str(crudo).strip().lower() not in ("nan", "none"):
            emit(log, f"Tel. inválido en el listado ('{crudo}') para {cliente} — busco en la base")

        cuit = str(fila["_cuit_key"]).lower()
        if cuit and cuit != "nan":
            tel_base = lookup.get(fila["_cuit_key"])
            if tel_base and not pd.isna(tel_base) and es_telefono_valido_arg(str(tel_base)):
                return str(tel_base)

        # Sin número usable. Antes esto devolvía el teléfono por defecto, con lo
        # cual el cupón del cliente terminaba en el celular de la oficina.
        if usar_telefono_defecto and es_telefono_valido_arg(telefono_por_defecto):
            emit(log, f"⚠ {cliente}: sin teléfono propio — uso el de defecto ({telefono_por_defecto})")
            return telefono_por_defecto

        sin_telefono.append(cliente)
        emit(log, f"⚠ {cliente}: sin teléfono válido — queda para revisar a mano")
        return None

    col_dest = col_tel if col_tel else "telefono"
    df[col_dest] = df.apply(resolver_tel, axis=1)
    df.drop(columns=["_cuit_key"], inplace=True)
    df.to_excel(salida, index=False)

    con_tel = int(df[col_dest].notna().sum())
    emit(log, f"Teléfonos resueltos: {con_tel} de {len(df)} — sin teléfono: {len(df) - con_tel}")

    return {
        "salida": str(Path(salida).resolve()),
        "total": len(df),
        "con_telefono": con_tel,
        "sin_telefono": int(len(df) - con_tel),
        "clientes_sin_telefono": sin_telefono,
        "columna_telefono": col_dest,
        "columna_cliente": col_cli,
        "columna_cuit": col_cuit,
        "outputs": [str(Path(salida).resolve())],
    }
