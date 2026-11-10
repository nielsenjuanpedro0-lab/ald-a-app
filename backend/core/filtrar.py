from __future__ import annotations

from pathlib import Path

import pandas as pd

from .utils import emit, find_column, str_poliza


def limpiar_clave(valor_raw) -> str:
    if pd.isna(valor_raw):
        return ""
    import re

    valor = str(valor_raw).split(".")[0]
    digits = re.sub(r"[^\d]", "", valor)
    return digits.lstrip("0")


def limpiar_monto(valor_raw) -> int | None:
    """Importe en centavos, para comparar sin errores de redondeo.

    Viene como "$ 169991,36", "$ 1.234,56", "$ 8534" o ya como número.
    """
    if pd.isna(valor_raw):
        return None
    if isinstance(valor_raw, (int, float)):
        return round(float(valor_raw) * 100)
    import re

    txt = re.sub(r"[^\d,.\-]", "", str(valor_raw))
    if not txt:
        return None
    if "," in txt:
        # Formato argentino: el punto es de miles y la coma es decimal
        txt = txt.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", txt):
        # "1.234" o "1.234.567": puntos de miles, sin decimales
        txt = txt.replace(".", "")
    try:
        return round(float(txt) * 100)
    except ValueError:
        return None


# Hasta 1 centavo de diferencia se toma como el mismo importe (redondeos)
TOLERANCIA_CENTAVOS = 1


def _read_excel_smart(file_path: str) -> tuple[pd.DataFrame, str | None]:
    """Reads Excel file dynamically checking skiprows=0..6 to find header row containing POLIZA column."""
    for skip in [5, 0, 1, 2, 3, 4, 6]:
        try:
            df = pd.read_excel(file_path, skiprows=skip)
            df.columns = df.columns.astype(str).str.strip()
            col_pol = find_column(df, "POLIZA", "PÓLIZA", "PLIZA")
            if col_pol is not None:
                return df, col_pol
        except Exception:
            pass

    df = pd.read_excel(file_path)
    df.columns = df.columns.astype(str).str.strip()
    col_pol = find_column(df, "POLIZA", "PÓLIZA", "PLIZA")
    return df, col_pol


def filtrar_rechazos(
    mes_pasado: str | None,
    mes_actual: str,
    salida_segundo: str = "listado.xlsx",
    salida_primero: str = "primer_rechazo_ignorado.xlsx",
    incluir_todos: bool = False,
    log=None,
):
    emit(log, "PROCESANDO RECHAZOS (TODOS LOS REGISTROS DEL MES ACTUAL)" if incluir_todos else "FILTRANDO SEGUNDOS RECHAZOS")

    if not mes_actual or not Path(mes_actual).exists():
        raise FileNotFoundError(f"No encontré el archivo del mes actual '{mes_actual}'")

    df_actual, col_pol_actual = _read_excel_smart(mes_actual)

    tiene_mes_pasado = bool(mes_pasado) and Path(mes_pasado).exists()

    # Sin mes pasado no hay con qué comparar: no se puede clasificar 1er/2do rechazo,
    # así que todo el mes actual se trata como bloque único para notificar.
    if not tiene_mes_pasado:
        df_salida = df_actual.copy()
        df_salida.to_excel(salida_segundo, index=False)
        df_vacio = df_actual.iloc[0:0].copy()
        df_vacio.to_excel(salida_primero, index=False)
        emit(log, f"Procesados {len(df_salida)} rechazos del mes actual para envío (sin mes pasado para clasificar).")
        return {
            "total_actual": len(df_actual),
            "segundo_rechazo": len(df_salida),
            "primer_rechazo": 0,
            "enviados_base": len(df_salida),
            "salida_segundo": str(Path(salida_segundo).resolve()),
            "salida_primero": str(Path(salida_primero).resolve()),
            "outputs": [str(Path(salida_segundo).resolve())],
        }

    df_pasado, col_pol_pasado = _read_excel_smart(mes_pasado)

    if not col_pol_pasado or not col_pol_actual:
        raise ValueError("No se pudo encontrar la columna de póliza en alguno de los archivos.")

    emit(log, f"Procesando {len(df_pasado)} rechazos del mes pasado...")
    emit(log, f"Procesando {len(df_actual)} rechazos del mes actual...")

    df_actual["_pol_clean"] = df_actual[col_pol_actual].apply(limpiar_clave)

    # 2do rechazo = la MISMA cuota rechazada otra vez: misma póliza y mismo
    # importe. Si la póliza se repite con otro importe es otra cuota, así que
    # es un 1er rechazo (caso De la Hoz: $169.991,36 en sep y $173.196,62 en oct).
    col_imp_pasado = find_column(df_pasado, "IMPORTE", "MONTO")
    col_imp_actual = find_column(df_actual, "IMPORTE", "MONTO")

    if col_imp_pasado and col_imp_actual:
        importes_pasado: dict[str, list[int]] = {}
        for pol, imp in zip(df_pasado[col_pol_pasado], df_pasado[col_imp_pasado]):
            clave, centavos = limpiar_clave(pol), limpiar_monto(imp)
            if clave and centavos is not None:
                importes_pasado.setdefault(clave, []).append(centavos)

        def es_segundo(fila) -> bool:
            centavos = limpiar_monto(fila[col_imp_actual])
            if centavos is None:
                return False
            return any(abs(centavos - c) <= TOLERANCIA_CENTAVOS
                       for c in importes_pasado.get(fila["_pol_clean"], []))

        mask_segundo = pd.Series([es_segundo(f) for _, f in df_actual.iterrows()],
                                 index=df_actual.index, dtype=bool)

        misma_poliza = df_actual["_pol_clean"].isin(importes_pasado.keys())
        otro_importe = df_actual[misma_poliza & ~mask_segundo]
        if len(otro_importe):
            emit(log, f"{len(otro_importe)} póliza(s) repetidas con otro importe (otra cuota) → 1er rechazo:")
            col_cli = find_column(df_actual, "CLIENTE", "ASEGURADO")
            for _, fila in otro_importe.iterrows():
                nombre = " ".join(str(fila[col_cli]).split()) if col_cli else ""
                emit(log, f"   • {fila['_pol_clean']} {nombre} — {fila[col_imp_actual]}")
    else:
        emit(log, "⚠ No encontré la columna de importe: comparo solo por número de póliza.")
        polizas_pasado = {limpiar_clave(pol) for pol in df_pasado[col_pol_pasado] if limpiar_clave(pol)}
        mask_segundo = df_actual["_pol_clean"].isin(polizas_pasado)
    df_segundo = df_actual[mask_segundo].copy()
    df_primero = df_actual[~mask_segundo].copy()

    df_segundo.drop(columns=["_pol_clean"], inplace=True)
    df_primero.drop(columns=["_pol_clean"], inplace=True)

    # "Todos los Rechazos" sigue clasificando 1er/2do (para la pestaña Lista de Clientes),
    # pero el archivo que se envía a notificación incluye ambos en vez de solo 2dos.
    df_enviar = df_actual.drop(columns=["_pol_clean"]) if incluir_todos else df_segundo

    df_enviar.to_excel(salida_segundo, index=False)
    df_primero.to_excel(salida_primero, index=False)

    summary = {
        "total_actual": len(df_actual),
        "segundo_rechazo": len(df_segundo),
        "primer_rechazo": len(df_primero),
        "enviados_base": len(df_enviar),
        "salida_segundo": str(Path(salida_segundo).resolve()),
        "salida_primero": str(Path(salida_primero).resolve()),
        "outputs": [
            str(Path(salida_segundo).resolve()),
            str(Path(salida_primero).resolve()),
        ],
    }
    emit(log, f"Procesados para envío: {summary['enviados_base']} ({'Todos los rechazos' if incluir_todos else 'Solo 2dos rechazos'})")
    emit(log, f"2do rechazo: {summary['segundo_rechazo']}")
    emit(log, f"1er rechazo: {summary['primer_rechazo']}")
    emit(log, f"Guardado: {summary['salida_segundo']}")
    return summary
