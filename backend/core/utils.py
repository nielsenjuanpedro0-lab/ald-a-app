from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

import pandas as pd


def emit(logger, message: str):
    if logger:
        logger(message)


def limpiar_lock_perfil(perfil_dir) -> None:
    """Si un intento anterior de abrir este perfil se colgó y se le cortó
    el tiempo (timeout de `launch_persistent_context`), puede quedar un
    chrome-headless-shell.exe zombie corriendo de fondo, todavía agarrado a
    este mismo perfil — y con él vivo, no alcanza con borrar el LOCK: el
    próximo intento se vuelve a colgar contra el mismo proceso. Por eso
    primero se mata cualquier proceso de este perfil puntual (nunca otros:
    se filtra por el --user-data-dir exacto) y recién después se borran los
    archivos de lock ("Default/LOCK", "SingletonLock", etc., que Chrome
    respeta como si otra instancia siguiera usando el perfil) — igual para
    el perfil de WhatsApp que el de la Oficina Virtual."""
    perfil = Path(perfil_dir)
    if sys.platform == "win32":
        try:
            filtro = str(perfil).replace("'", "''")
            # El --user-data-dir solo aparece en la línea de comando del
            # proceso raíz (el que arranca Chrome): los procesos hijos
            # (renderers, GPU) lo heredan por IPC, no por línea de comando.
            # Si solo se mata la raíz, los hijos quedan huérfanos y vivos
            # igual — hay que matar todo el árbol.
            script = (
                "$all = Get-CimInstance Win32_Process -Filter \"Name='chrome-headless-shell.exe' or Name='chrome.exe'\"; "
                f"$roots = $all | Where-Object {{ $_.CommandLine -like '*{filtro}*' }} | Select-Object -ExpandProperty ProcessId; "
                "$toKill = [System.Collections.Generic.HashSet[int]]::new(); "
                "$queue = [System.Collections.Generic.Queue[int]]::new(); "
                "foreach ($r in $roots) { $toKill.Add($r) | Out-Null; $queue.Enqueue($r) }; "
                "while ($queue.Count -gt 0) { "
                "$p = $queue.Dequeue(); "
                "foreach ($c in ($all | Where-Object { $_.ParentProcessId -eq $p })) { "
                "if ($toKill.Add($c.ProcessId)) { $queue.Enqueue($c.ProcessId) } } }; "
                "foreach ($pid2 in $toKill) { Stop-Process -Id $pid2 -Force -ErrorAction SilentlyContinue }"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            pass
    for nombre in ("SingletonLock", "SingletonCookie", "SingletonSocket", "Default/LOCK"):
        try:
            (perfil / nombre).unlink(missing_ok=True)
        except OSError:
            pass


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def detectar_header(df_raw: pd.DataFrame, palabra: str) -> int:
    palabra_norm = palabra.upper().replace("Ó", "O").replace("Ú", "U")
    for idx, row in df_raw.iterrows():
        for val in row:
            if pd.notna(val) and palabra_norm in str(val).upper().replace("Ó", "O"):
                return idx
    return 0


def find_column(df: pd.DataFrame, *words) -> str | None:
    best_col = None
    for col in df.columns:
        col_norm = str(col).upper().replace("Ó", "O").replace("Ú", "U").replace("É", "E")
        if any(skip in col_norm for skip in ["RAMO", "FECHA", "IMPORTE", "MONTO", "ESTADO", "CUOTA"]):
            continue
        for word in words:
            if word.upper() in col_norm:
                if any(num in col_norm for num in ["NRO", "NUM", "NO", "NR", "NUMERO"]):
                    return col
                if best_col is None:
                    best_col = col
    if best_col is not None:
        return best_col

    for col in df.columns:
        col_norm = str(col).upper().replace("Ó", "O").replace("Ú", "U").replace("É", "E")
        for word in words:
            if word.upper() in col_norm:
                return col
    return None


def str_poliza(value) -> str:
    return str(value).split(".")[0].strip()


def limpiar_nombre_persona(nombre: str) -> str:
    nombre = str(nombre).replace("_", " ").strip()
    palabras = nombre.split()
    visto = []
    for p in palabras:
        if p.upper() not in [v.upper() for v in visto]:
            visto.append(p.capitalize())
    return " ".join(visto)


def primer_nombre_persona(cliente: str) -> str:
    """Devuelve el nombre de pila para el saludo.

    Los listados vienen como "APELLIDO NOMBRE", por eso se prefiere la segunda
    palabra. Nunca lanza IndexError con nombres vacíos o de una sola palabra.
    """
    partes = [p for p in str(cliente).split() if p.lower() not in ("nan", "none", "null")]
    if not partes:
        return "Cliente"
    return partes[1] if len(partes) > 1 else partes[0]


def clave_agrupacion(df: pd.DataFrame, col_cuit: str, col_cli: str) -> pd.Series:
    """Clave CUIT_CLIENTE para agrupar las pólizas de un mismo asegurado.

    El fillna es necesario: en pandas 3 un astype(str) sobre una celda vacía
    sigue siendo NaN, la concatenación propaga el NaN y groupby descarta esas
    filas en silencio — el asegurado quedaba sin mensaje y sin aparecer en el
    reporte de errores.
    """
    cuit = df[col_cuit].astype(str).fillna("").str.strip()
    cliente = df[col_cli].astype(str).fillna("").str.strip()
    return cuit + "_" + cliente


def limpiar_nombre_archivo(nombre: str) -> str:
    nombre = nombre.replace("/", "-").replace("\\", "-")
    nombre = re.sub(r'[*?:"<>|]', "", nombre)
    nombre = re.sub(r"\s+", "_", nombre.strip())
    return nombre.rstrip("._-")[:100]


def buscar_pdf_por_poliza(poliza: str, carpeta: str | Path) -> str | None:
    p = str_poliza(poliza)
    if not p or p == "nan":
        return None
    carpeta = Path(carpeta)
    if not carpeta.exists():
        return None
    coincidencias = [
        str(archivo.resolve())
        for archivo in sorted(carpeta.iterdir())
        if archivo.is_file()
        and archivo.suffix.lower() == ".pdf"
        and p in archivo.name
        and not archivo.name.lower().startswith("polizas")
    ]
    return coincidencias[0] if coincidencias else None


def buscar_pdf_maestro(carpeta: str | Path) -> list[Path]:
    carpeta = Path(carpeta)
    if not carpeta.exists():
        return []
    if carpeta.is_file() and carpeta.suffix.lower() == ".pdf":
        return [carpeta]

    todos_pdfs = sorted([f for f in carpeta.iterdir() if f.is_file() and f.suffix.lower() == ".pdf"])
    if not todos_pdfs:
        return []

    # 1. Archivos que arrancan con "poliza" o "00"
    maestros = [f for f in todos_pdfs if f.name.lower().startswith("poliza") or f.name.lower().startswith("00")]
    if maestros:
        return maestros

    # 2. PDFs con múltiples páginas (> 1 página)
    try:
        try:
            from pypdf import PdfReader
        except ImportError:
            from PyPDF2 import PdfReader  # type: ignore[no-redef]
        multipagina = []
        for f in todos_pdfs:
            try:
                reader = PdfReader(str(f))
                if len(reader.pages) > 1:
                    multipagina.append(f)
            except Exception:
                pass
        if multipagina:
            return multipagina
    except Exception:
        pass

    # 3. Filtrar cualquier archivo que sea un cupón ya recortado (ej: NRO_NOMBRE.pdf o NRO_REVISAR.pdf)
    candidatos = [f for f in todos_pdfs if not re.match(r"^(\d+[\d\-]*_|[S|s][N|n]_)", f.name)]
    if candidatos:
        return candidatos

    return todos_pdfs



def copiar_columnas(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    return df.loc[:, [c for c in cols if c in df.columns]].copy()

