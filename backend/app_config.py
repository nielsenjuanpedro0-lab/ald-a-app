from __future__ import annotations
import json
from pathlib import Path

CONFIG_FILE = Path.home() / ".rechazos_app_config.json"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(cfg: dict):
    try:
        current = load_config()
        current.update(cfg)
        # Escritura atómica (mismo patrón que core/historial.py): si Windows
        # mata el proceso a mitad del guardado, queda el .json viejo intacto
        # en vez de un archivo truncado que resetea toda la configuración
        # (carpeta de trabajo, base predeterminada, plantilla, etc.).
        tmp = CONFIG_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
        tmp.replace(CONFIG_FILE)
    except Exception:
        pass
