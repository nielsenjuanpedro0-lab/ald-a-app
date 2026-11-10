from __future__ import annotations

import html
import os
import sys
from pathlib import Path

import pandas as pd
from PySide6.QtGui import QPageLayout, QTextDocument
from PySide6.QtPrintSupport import QPrintDialog, QPrinter, QPrinterInfo
from PySide6.QtWidgets import QWidget


def _abrir_archivo(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        os.system(f'open "{path}"')
    else:
        os.system(f'xdg-open "{path}"')


def _construir_html_tabla(df: pd.DataFrame, titulo: str) -> str:
    # html.escape en todo lo que viene del Excel: un asegurado llamado
    # "GOMEZ <ANA>" se imprimía como "GOMEZ" porque Qt se comía el <ANA> como
    # si fuera una etiqueta.
    esc = lambda v: html.escape(str(v))

    encabezado = "".join(f"<th>{esc(col)}</th>" for col in df.columns)
    filas = []
    for _, fila in df.iterrows():
        celdas = "".join(f"<td>{esc(fila[col])}</td>" for col in df.columns)
        filas.append(f"<tr>{celdas}</tr>")

    return f"""
    <html>
    <head>
    <style>
        body {{ font-family: Arial, sans-serif; }}
        h2 {{ text-align: center; margin-bottom: 2px; }}
        p.meta {{ text-align: center; color: #444; margin-top: 0; }}
        table {{ border-collapse: collapse; width: 100%; font-size: 8pt; }}
        th, td {{ border: 1px solid #333; padding: 3px 5px; text-align: left; }}
        th {{ background: #E5E7EB; }}
    </style>
    </head>
    <body>
        <h2>{esc(titulo)}</h2>
        <p class="meta">{len(df)} registros</p>
        <table>
            <tr>{encabezado}</tr>
            {"".join(filas)}
        </table>
    </body>
    </html>
    """


def imprimir_listado(
    excel_path: str,
    titulo: str = "Listado de Rechazos",
    parent: QWidget | None = None,
    log=None,
) -> bool:
    """Abre el diálogo de impresión nativo con el excel formateado como tabla, tal cual una planilla impresa."""
    if not Path(excel_path).exists():
        raise FileNotFoundError(f"No encontré '{excel_path}'")

    df = pd.read_excel(excel_path)
    df.columns = df.columns.astype(str).str.strip()

    cols_excluir = [
        c for c in df.columns
        if any(k in c.upper() for k in ["LINK", "CUPÓN", "CUPON", "TELEFONO", "TELÉFONO", "CEL", "MOVIL", "MÓVIL"])
    ]
    if cols_excluir:
        df = df.drop(columns=cols_excluir)

    df = df.fillna("")
    for col in df.columns:
        df[col] = df[col].astype(str).str.replace(r"\.0$", "", regex=True)

    doc = QTextDocument()
    doc.setHtml(_construir_html_tabla(df, titulo))

    if QPrinterInfo.availablePrinters():
        printer = QPrinter(QPrinter.HighResolution)
        printer.setPageOrientation(QPageLayout.Landscape)

        dialog = QPrintDialog(printer, parent)
        dialog.setWindowTitle("Imprimir listado")
        if dialog.exec() != QPrintDialog.Accepted:
            return False

        doc.print_(printer)
        if log:
            log(f"Listado impreso: {titulo} ({len(df)} registros)")
        return True

    # No hay impresora instalada/configurada en Windows: Qt no puede abrir el diálogo
    # nativo ("Cannot be used on non-native printers"). Generar PDF y abrirlo en su lugar.
    salida_pdf = Path(excel_path).with_name(f"{Path(excel_path).stem}_imprimir.pdf")
    printer = QPrinter(QPrinter.HighResolution)
    printer.setPageOrientation(QPageLayout.Landscape)
    printer.setOutputFormat(QPrinter.PdfFormat)
    printer.setOutputFileName(str(salida_pdf))
    doc.print_(printer)
    _abrir_archivo(str(salida_pdf))
    if log:
        log(f"No se detectó impresora instalada. Generé PDF para imprimir: {salida_pdf.name} ({len(df)} registros)")
    return True
