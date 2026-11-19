"""Guarda la contraseña de la Oficina Virtual cifrada con DPAPI, atada a la
cuenta de Windows de la persona que usa la app: nada de texto plano en
config.json, sin sumar una dependencia nueva (keyring) solo para esto.

Mac no tiene DPAPI: ahí `disponible()` da False y el llamador sigue pidiendo
la contraseña a mano en cada conexión, como ya hacía antes de esto.
"""
from __future__ import annotations

import sys

if sys.platform == "win32":
    import base64
    import ctypes
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(data: bytes) -> _DATA_BLOB:
        buf = ctypes.create_string_buffer(data, len(data))
        return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    # DPAPI sin esto ("entropía") lo puede descifrar cualquier proceso que
    # corra con tu mismo usuario de Windows, no solo AlDía — es exactamente
    # lo que buscan los robainfo genéricos. No es un secreto (está en el
    # código fuente), pero saca a este dato de "cualquier DPAPI sirve" y lo
    # deja en "hace falta saber esto puntual", que ya filtra a casi todo el
    # malware de catálogo.
    _ENTROPIA = _blob(b"AlDia/OficinaVirtual/v1")

    def disponible() -> bool:
        return True

    def cifrar(texto: str) -> str:
        entrada = _blob(texto.encode("utf-8"))
        salida = _DATA_BLOB()
        if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(entrada), None, ctypes.byref(_ENTROPIA), None, None, 0, ctypes.byref(salida)
        ):
            raise OSError("No se pudo cifrar la contraseña de la Oficina Virtual.")
        try:
            return base64.b64encode(ctypes.string_at(salida.pbData, salida.cbData)).decode("ascii")
        finally:
            ctypes.windll.kernel32.LocalFree(salida.pbData)

    def descifrar(texto_cifrado: str) -> str | None:
        """None si no se pudo recuperar (perfil de Windows distinto, dato
        corrupto): el llamador debe volver a pedirla, nunca romper por esto."""
        try:
            entrada = _blob(base64.b64decode(texto_cifrado))
            salida = _DATA_BLOB()
            if not ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(entrada), None, ctypes.byref(_ENTROPIA), None, None, 0, ctypes.byref(salida)
            ):
                return None
            try:
                return ctypes.string_at(salida.pbData, salida.cbData).decode("utf-8")
            finally:
                ctypes.windll.kernel32.LocalFree(salida.pbData)
        except Exception:
            return None

else:
    def disponible() -> bool:
        return False

    def cifrar(texto: str) -> str:
        raise NotImplementedError("Guardar la contraseña de la OV solo está soportado en Windows.")

    def descifrar(texto_cifrado: str) -> str | None:
        return None
