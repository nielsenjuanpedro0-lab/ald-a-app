from __future__ import annotations

from pathlib import Path

import pandas as pd

from .utils import emit, find_column, buscar_pdf_por_poliza, str_poliza, detectar_header


def vincular_pdfs(excel_entrada: str, salida: str = "listado_con_cupones.xlsx", carpeta_pdfs: str | None = None, log=None):
    carpeta = Path(carpeta_pdfs) if carpeta_pdfs else Path(excel_entrada).resolve().parent
    if not Path(excel_entrada).exists():
        raise FileNotFoundError(f"No encontré '{excel_entrada}'")

    df_raw = pd.read_excel(excel_entrada, header=None, nrows=20)
    header_row = detectar_header(df_raw, "POLIZA")
    df = pd.read_excel(excel_entrada, header=header_row)
    df.columns = df.columns.str.strip()

    col_pol = find_column(df, "POLIZA", "PÓLIZA")
    if not col_pol:
        raise ValueError("No encontré columna de póliza.")

    rutas_dict = {}
    display_links = []
    encontrados = 0

    for pol in df[col_pol]:
        ruta = buscar_pdf_por_poliza(pol, carpeta)
        if ruta:
            filename = Path(ruta).name
            rutas_dict[str(pol)] = ruta
            display_links.append(f"Ver Cupón ({filename})")
            encontrados += 1
        else:
            display_links.append("No encontrado")

    no_encontrados = len(df) - encontrados
    df["Link al Cupón"] = display_links
    df.to_excel(salida, index=False)

    # Agregar hipervínculos nativos de Excel con openpyxl sin romper el texto para pandas/GUI
    try:
        import openpyxl
        wb = openpyxl.load_workbook(salida)
        ws = wb.active
        header = [cell.value for cell in ws[1]]
        if "Link al Cupón" in header and col_pol in header:
            link_col_idx = header.index("Link al Cupón") + 1
            pol_col_idx = header.index(col_pol) + 1
            for r in range(2, ws.max_row + 1):
                pol_val = str(ws.cell(row=r, column=pol_col_idx).value or "")
                ruta = buscar_pdf_por_poliza(pol_val, carpeta)
                if ruta:
                    cell = ws.cell(row=r, column=link_col_idx)
                    cell.hyperlink = ruta
        wb.save(salida)
    except Exception as err:
        emit(log, f"Nota: Se guardaron links en formato texto ({err})")

    emit(log, f"PDFs encontrados: {encontrados}")
    emit(log, f"PDFs no encontrados: {no_encontrados}")

    return {
        "encontrados": encontrados,
        "no_encontrados": no_encontrados,
        "salida": str(Path(salida).resolve()),
        "outputs": [str(Path(salida).resolve())],
    }
