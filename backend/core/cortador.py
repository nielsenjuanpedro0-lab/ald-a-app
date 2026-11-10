from __future__ import annotations

from pathlib import Path


def fragmentar_pdfs(carpeta: str, salida_dir: str | None = None, numeros_ignorar=None, log=None):
    numeros_ignorar = numeros_ignorar or {"50003196"}
    
    # pypdf es nativo y ya está instalado en Python 3.14
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        try:
            from PyPDF2 import PdfReader, PdfWriter
        except ImportError as e:
            raise ImportError("Falta la librería 'pypdf' para fragmentar PDFs.") from e

    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None

    from .utils import buscar_pdf_maestro, limpiar_nombre_archivo, emit

    if pdfplumber is None:
        emit(log, "⚠ pdfplumber no está instalado: leo los cupones con pypdf. "
                  "La póliza o el nombre pueden salir distinto que en otra máquina. "
                  "Instalalo con: pip install pdfplumber")

    rutas = [p.strip() for p in str(carpeta).split(";") if p.strip()]
    masters = []
    salida_path = None

    for r in rutas:
        p_path = Path(r)
        if not p_path.exists():
            continue
        if p_path.is_file() and p_path.suffix.lower() == ".pdf":
            masters.append(p_path)
            if not salida_path:
                salida_path = Path(salida_dir) if salida_dir else p_path.parent
        elif p_path.is_dir():
            found = buscar_pdf_maestro(p_path)
            masters.extend(found)
            if not salida_path:
                salida_path = Path(salida_dir) if salida_dir else p_path

    if not salida_path:
        salida_path = Path(salida_dir) if salida_dir else Path.cwd()

    salida_path.mkdir(parents=True, exist_ok=True)

    if not masters:
        raise FileNotFoundError(f"No se encontró ningún archivo PDF válido en: {carpeta}")

    total = 0
    outputs = []
    import re

    for pdf_maestro in masters:
        emit(log, f"Procesando PDF maestro: {pdf_maestro.name}")
        try:
            reader = PdfReader(str(pdf_maestro))
            plumber = pdfplumber.open(str(pdf_maestro)) if pdfplumber else None
        except Exception as err:
            emit(log, f"⚠️ Error al abrir {pdf_maestro.name}: {err}")
            continue

        num_paginas = len(plumber.pages) if plumber else len(reader.pages)

        for i in range(num_paginas):
            try:
                if plumber:
                    texto = plumber.pages[i].extract_text() or ""
                else:
                    texto = reader.pages[i].extract_text() or ""

                numeros_poliza = []
                nombre_asegurado = "REVISAR"

                candidatos_poliza = re.findall(r"(?i)P[OÓ]LIZA[\s:]*(\d{5,9})", texto)
                candidatos = candidatos_poliza if candidatos_poliza else re.findall(r"\b\d{5,8}\b", texto)

                for n in candidatos:
                    if n not in numeros_ignorar and n not in numeros_poliza:
                        numeros_poliza.append(n)

                # Extraer nombre del asegurado (multilinea o misma linea)
                lines = [l.strip() for l in texto.split("\n") if l.strip()]
                keywords_excluir = {"POLIZA", "PÓLIZA", "PLIZA", "SECCIÓN", "SECCION", "COMPROBANTE", "IMPORTE", "FECHA", "ENDOSO", "RED", "PAGO", "RAPIPAGO", "FÁCIL", "FACIL", "VENCIMIENTO"}
                nombre_asegurado = "REVISAR"

                for idx, line in enumerate(lines):
                    line_up = line.upper()
                    if "PARA EL ASEGURADO" in line_up or "COMPROBANTE PARA" in line_up:
                        continue
                    if "ASEGURADO" in line_up or "TOMADOR" in line_up:
                        # Misma línea: "Asegurado: JUAN PEREZ"
                        if ":" in line:
                            partes = line.split(":", 1)
                            candidato = partes[1].strip()
                            if candidato and not any(k in candidato.upper() for k in keywords_excluir):
                                nombre_asegurado = candidato
                                break

                        # Siguiente línea: "Asegurado:" en una línea y "JUAN PEREZ" en la siguiente
                        if idx + 1 < len(lines):
                            next_l = lines[idx + 1].strip()
                            if next_l and not any(k in next_l.upper() for k in keywords_excluir):
                                nombre = next_l
                                if idx + 2 < len(lines):
                                    next_l2 = lines[idx + 2].strip()
                                    if next_l2 and not any(k in next_l2.upper() for k in keywords_excluir):
                                        nombre += f" {next_l2}"
                                nombre_asegurado = nombre
                                break

                str_nros = "-".join(numeros_poliza) if numeros_poliza else f"SN_{i + 1}"
                nombre_raw = f"{str_nros}_{nombre_asegurado}"
                nombre_final = limpiar_nombre_archivo(nombre_raw) + ".pdf"
                
                escritor = PdfWriter()
                escritor.add_page(reader.pages[i])
                out_file = salida_path / nombre_final
                with open(out_file, "wb") as f:
                    escritor.write(f)
                
                total += 1
                outputs.append(str(out_file.resolve()))
                emit(log, f"  ✓ Pág {i + 1} → {nombre_final}")
            except Exception as page_err:
                emit(log, f"  ❌ Error en Pág {i + 1}: {page_err}")

        if plumber:
            try:
                plumber.close()
            except Exception:
                pass

    if total == 0:
        emit(log, "⚠️ ADVERTENCIA: No se generó ningún cupón individual.")

    return {"generados": total, "salida_dir": str(salida_path.resolve()), "outputs": outputs}

