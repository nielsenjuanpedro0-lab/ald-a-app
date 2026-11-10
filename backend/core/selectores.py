"""Selectores del DOM de WhatsApp Web, en un solo lugar.

WhatsApp cambia el HTML sin avisar. Cuando algo deje de andar, lo que hay que
tocar es esto y nada más. Se puede pisar sin editar el código creando
`~/.rechazos_app/selectores.json` con las mismas claves.

Cada clave es una lista: se prueban en orden y se usa el primero que aparezca,
así una versión nueva de WhatsApp no rompe todo de una.
"""

from __future__ import annotations

import json
from pathlib import Path

ARCHIVO_SELECTORES = Path.home() / ".rechazos_app" / "selectores.json"

SELECTORES: dict[str, list[str]] = {
    # Pantalla de login: si esto aparece, hay que escanear el QR
    "qr": [
        'canvas[aria-label*="QR"]',
        '[data-testid="qrcode"]',
        'div[data-ref]',
    ],
    # Sesión iniciada: la lista de chats de la izquierda
    "lista_chats": [
        '#pane-side',
        '[data-testid="chat-list"]',
        'div[aria-label*="Lista de chats"]',
        'div[aria-label*="Chat list"]',
    ],
    # Buscador de chats. Desde oct/2026 es un <input>, ya no un div editable.
    "buscador": [
        'input[data-tab="3"]',
        '#side input[role="textbox"]',
        'input[aria-label*="Buscar un chat"]',
        'input[aria-label*="Search"]',
        'div[contenteditable="true"][data-tab="3"]',
        '[data-testid="chat-list-search"]',
        'div[aria-label*="Cuadro de texto para ingresar la búsqueda"]',
        'div[aria-label*="Search input textbox"]',
    ],
    # Resultado de búsqueda clickeable.
    # Los chats ahora son filas de una grilla, pero NO toda fila es un chat:
    # en la búsqueda los títulos "Chats", "Contactos", "Mensajes" también son
    # role="row" (sin celda adentro), y se clickeaba el título. Fuera de las
    # filas, el primer `cell-frame-container` es "Chats restringidos". Por eso
    # se pide la celda DENTRO de una fila.
    "resultado_busqueda": [
        '#pane-side [role="row"] [data-testid="cell-frame-container"]',
        '#pane-side [role="row"]:has([data-testid="cell-frame-container"])',
        '#pane-side [role="listitem"]',
        '[data-testid="cell-frame-container"]',
    ],
    # Cuadro donde se escribe el mensaje
    "composer": [
        'div[contenteditable="true"][data-tab="10"]',
        'footer div[contenteditable="true"]',
        '[data-testid="conversation-compose-box-input"]',
        'div[aria-label^="Escribir un mensaje"]',
        'div[aria-label*="Escribe un mensaje"]',
        'div[aria-label*="Type a message"]',
    ],
    # Botón de enviar. Ojo: en el preview del adjunto el botón NO es
    # `button[aria-label="Enviar"]` ni `span[data-icon="send"]` (ambos dan 0).
    # Es un div con aria-label "Enviar N seleccionado" y adentro este icono.
    "boton_enviar": [
        'span[data-icon="wds-ic-send-filled"]',
        'div[aria-label^="Enviar"][role="button"]',
        'div[aria-label^="Enviar"]',
        'div[aria-label^="Send"]',
        'button[aria-label="Enviar"]',
        'button[aria-label="Send"]',
        'span[data-icon="send"]',
        '[data-testid="send"]',
    ],
    # Overlay del adjunto abierto: mientras esto exista, el PDF NO se mandó.
    # Solo selectores específicos del adjunto: un `span[data-icon="x-alt"]`
    # genérico también matchea la X del cartel de notificaciones y deja el
    # overlay "abierto" para siempre.
    "overlay_adjunto": [
        'div[aria-label="Quitar archivo adjunto"]',
        'div[aria-label*="Quitar archivo"]',
        'div[aria-label*="Remove file"]',
        'div[aria-label*="Remover arquivo"]',
        'div[aria-label*="Añadir archivo"]',
        'div[aria-label*="Add file"]',
    ],
    # Botón del clip (ahora un "+"), que abre el menú de adjuntar.
    # Va primero el estructural porque no depende del idioma: es el único
    # botón de la barra del mensaje que abre un menú. El data-icon
    # `ic-attach-file` ya no existe desde oct/2026, queda de respaldo.
    "boton_adjuntar": [
        'footer button[data-tab="10"][aria-haspopup="menu"]',
        'span[data-icon="ic-attach-file"]',
        'button[aria-label="Adjuntar"]',
        'button[aria-label="Attach"]',
        'button[aria-label="Anexar"]',
        'button[title="Adjuntar"]',
    ],
    # Opción "Documento" dentro de ese menú.
    # Estos botones NO tienen data-icon, así que no hay forma de reconocerlos
    # por icono. Si ninguna etiqueta coincide, el código cae a un método
    # posicional (el primer botón que aparece al abrir el menú es Documento),
    # que sí funciona en cualquier idioma.
    "opcion_documento": [
        'button[aria-label="Documento"]',
        'button[aria-label="Document"]',
        '[aria-label="Documento"]',
        '[aria-label="Document"]',
    ],
    # Input oculto de documentos.
    #
    # OJO: hasta que no se abre el menú del clip y se elige "Documento", el
    # único input del DOM es el de `accept="image/*"`, que descarta un PDF sin
    # decir nada — se adjuntaba la nada y no aparecía ninguna vista previa.
    # El de documentos aparece recién después y tiene accept="*" multiple.
    "input_documento": [
        'input[type="file"][accept="*"]',
        'input[type="file"][multiple]',
        'input[type="file"]:not([accept*="image"])',
        'input[type="file"][accept*="application"]',
    ],
    # Filas de mensaje del chat abierto.
    #
    # No se intenta distinguir "propio" vs "recibido": esta versión de WhatsApp
    # usa clases ofuscadas (x1n2onr6 xscbp6u...) y el data-id ya no trae el
    # prefijo true_/false_, así que `message-out` y `data-id^="true_"` dan 0.
    # La verificación se hace por CONTENIDO (que aparezca el texto que mandamos
    # o el nombre del PDF), que no depende de clases que cambian.
    "fila_mensaje": [
        '[role="row"]',
        '#main [role="row"]',
    ],
    # Cartel de número inexistente
    "error_numero": [
        'div[data-testid="popup-contents"]',
        'div[role="dialog"]',
    ],
}

# Texto de los carteles de número inválido
TEXTOS_NUMERO_INVALIDO = (
    "no es un numero de telefono valido",
    "numero de telefono compartido a traves de la url",
    "phone number shared via url is invalid",
    "invalid phone number",
    "no esta en whatsapp",
    "is not on whatsapp",
)


def cargar_selectores() -> dict[str, list[str]]:
    """Devuelve los selectores, con lo que haya en el JSON del usuario pisando."""
    sel = {k: list(v) for k, v in SELECTORES.items()}
    if ARCHIVO_SELECTORES.exists():
        try:
            with open(ARCHIVO_SELECTORES, "r", encoding="utf-8") as f:
                propios = json.load(f)
            for clave, valor in (propios or {}).items():
                if isinstance(valor, str):
                    valor = [valor]
                if isinstance(valor, list) and valor:
                    # Los del usuario van primero, los nuestros quedan de respaldo
                    sel[clave] = valor + [s for s in sel.get(clave, []) if s not in valor]
        except Exception:
            pass
    return sel
