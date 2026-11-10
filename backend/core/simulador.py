from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from .telefonos import formatear_telefono
from .utils import (
    buscar_pdf_por_poliza,
    clave_agrupacion,
    emit,
    find_column,
    limpiar_nombre_persona,
    primer_nombre_persona,
    str_poliza,
)


DEFAULT_PLANTILLA_MENSAJE = (
    "Hola *{nombre}*,\n\n"
    "Te contactamos porque desde la compañía nos informan que se registró "
    "un rechazo en el débito de {palabra_poliza}: {polizas}.\n\n"
    "Adjuntamos {palabra_cupon} para su regularización. ¡Muchas gracias!"
)


def generar_mensaje_unificado(primer_nombre: str, lista_polizas: list, plantilla: str | None = None) -> str:
    str_polizas = ", ".join([f"*{p}*" for p in lista_polizas])
    palabra_poliza = "tus pólizas" if len(lista_polizas) > 1 else "tu póliza"
    palabra_cupon = "los cupones" if len(lista_polizas) > 1 else "el cupón"

    plantilla_usar = plantilla.strip() if plantilla and plantilla.strip() else DEFAULT_PLANTILLA_MENSAJE

    try:
        return plantilla_usar.format(
            nombre=primer_nombre,
            polizas=str_polizas,
            palabra_poliza=palabra_poliza,
            palabra_cupon=palabra_cupon,
            cupones=palabra_cupon,
        )
    except Exception:
        return (
            f"Hola *{primer_nombre}*,\n\n"
            f"Te contactamos porque desde la compañía nos informan que se registró "
            f"un rechazo en el débito de {palabra_poliza}: {str_polizas}.\n\n"
            f"Adjuntamos {palabra_cupon} para su regularización. ¡Muchas gracias!"
        )


def simular_agrupacion(excel_entrada: str, log=None):
    if not Path(excel_entrada).exists():
        raise FileNotFoundError(f"No se encontró '{excel_entrada}'")

    df = pd.read_excel(excel_entrada)
    df.columns = df.columns.str.strip()
    col_tel = find_column(df, "TELEFONO", "TELÉFONO", "CEL", "MOVIL", "MÓVIL")
    col_pol = find_column(df, "POLIZA", "PÓLIZA")
    col_cli = find_column(df, "CLIENTE", "ASEGURADO")
    col_cuit = find_column(df, "CUIT", "CUIL")

    if not all([col_tel, col_pol, col_cli, col_cuit]):
        raise ValueError("Faltan columnas requeridas: Teléfono, Póliza, Cliente, CUIT/CUIL")

    df["_clave_agrupacion"] = clave_agrupacion(df, col_cuit, col_cli)
    grupos = df.groupby("_clave_agrupacion")

    resultado = []
    for idx, (clave, filas) in enumerate(grupos, start=1):
        # pandas relee "+5491122334455" como número: hay que re-armar el formato
        tel = formatear_telefono(filas.iloc[0].get(col_tel))
        cliente = limpiar_nombre_persona(str(filas.iloc[0].get(col_cli)))
        cuit = filas.iloc[0].get(col_cuit)
        lista_polizas = [str_poliza(p) for p in filas[col_pol].unique()]
        pdfs = []
        for p in lista_polizas:
            ruta = buscar_pdf_por_poliza(p, Path(excel_entrada).parent)
            if ruta and ruta not in pdfs:
                pdfs.append(ruta)
        primer_nombre = primer_nombre_persona(cliente)
        mensaje = generar_mensaje_unificado(primer_nombre, lista_polizas)
        emit(log, f"[{idx}] {cliente} | {cuit}")
        resultado.append(
            {
                "cliente": cliente,
                "cuit": cuit,
                "telefono": tel,
                "polizas": lista_polizas,
                "pdfs": pdfs,
                "mensaje": mensaje,
            }
        )

    return {"clientes": len(resultado), "detalle": resultado, "outputs": []}
