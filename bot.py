"""
Bot de ofertas "Lo Que Todo Hombre Necesita" (cola de ofertas).

GitHub Actions lo ejecuta cada hora de 10:00 a 21:00 (hora de CDMX). En cada
ejecucion publica en el canal de Telegram la siguiente oferta pendiente de
ofertas.csv, con tu link de afiliado (matt_word / matt_tool) ya agregado, y la
marca como publicada.

Columnas de ofertas.csv:
  fecha         YYYY-MM-DD desde cuando se puede publicar (vacio = ya)
  categoria     gym, escuela, relojes, carro, perfumes, ropa, gaming, setup, cuidado
  producto      nombre corto
  precio_antes  precio sin descuento
  precio_ahora  precio de oferta
  imagen_url    foto del producto (opcional)
  link          link normal del producto en Mercado Libre
  publicado     lo llena el bot

Secrets: TELEGRAM_TOKEN, CHANNEL_ID
Prueba sin publicar:  python bot.py --prueba
"""

import csv
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# Tus codigos de afiliado de Mercado Libre (se agregan a cada link)
MATT_WORD = "rodriguezgael20231019205545"
MATT_TOOL = "55310688"

ZONA_MX = timezone(timedelta(hours=-6))
HORA_INICIO, HORA_FIN = 10, 22          # publica de 10:00 a 21:59
ARCHIVO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ofertas.csv")
COLUMNAS = ["fecha", "categoria", "producto", "precio_antes", "precio_ahora",
            "imagen_url", "link", "publicado"]
EMOJI = {"gym": "💪", "escuela": "🎒", "relojes": "⌚", "carro": "🚗",
         "perfumes": "🧴", "ropa": "👕", "gaming": "🎮", "setup": "🎥",
         "cuidado": "🧔"}


def ahora():
    return datetime.now(ZONA_MX)


# ---------- CSV (acepta lo que guarda Excel) ----------

def leer_csv():
    with open(ARCHIVO, "rb") as f:
        crudo = f.read()
    for cod in ("utf-8-sig", "cp1252"):
        try:
            texto = crudo.decode(cod)
            break
        except UnicodeDecodeError:
            continue
    primera = texto.splitlines()[0] if texto else ""
    sep = ";" if primera.count(";") > primera.count(",") else ","
    filas = []
    for fila in csv.DictReader(io.StringIO(texto), delimiter=sep):
        limpia = {(k or "").strip().lower(): (v or "").strip() for k, v in fila.items()}
        filas.append({c: limpia.get(c, "") for c in COLUMNAS})
    return filas, sep


def guardar_csv(filas, sep):
    with open(ARCHIVO, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS, delimiter=sep)
        w.writeheader()
        w.writerows(filas)


# ---------- logica ----------

def numero(texto):
    try:
        return float(texto.replace("$", "").replace(",", "").replace(" ", ""))
    except ValueError:
        return None


def pesos(v):
    return f"${v:,.0f}"


def html(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def link_afiliado(link):
    """Link limpio del producto + tus codigos de afiliado.
    Si ya es un link corto de afiliado (meli.la) se deja igual."""
    if "meli.la" in link or "matt_tool=" in link:
        return link
    base = re.split(r"[?#]", link, maxsplit=1)[0]
    return base + "?" + urllib.parse.urlencode({"matt_word": MATT_WORD,
                                                "matt_tool": MATT_TOOL})


def siguiente(filas, hoy):
    for i, f in enumerate(filas):
        if f["publicado"] or not f["producto"] or not f["link"]:
            continue
        if f["fecha"]:
            try:
                if datetime.strptime(f["fecha"], "%Y-%m-%d").date() > hoy:
                    continue
            except ValueError:
                pass
        return i
    return None


def texto(f):
    antes, ahora_ = numero(f["precio_antes"]), numero(f["precio_ahora"])
    cat = f["categoria"].lower()
    lineas = [f"{EMOJI.get(cat, '🔥')} <b>{html(f['producto'])}</b>", ""]
    if antes and ahora_ and antes > ahora_:
        lineas += [f"❌ Antes: <s>{pesos(antes)}</s>",
                   f"✅ Ahora: <b>{pesos(ahora_)}</b>  ({round((1 - ahora_ / antes) * 100)}% OFF)"]
    elif ahora_:
        lineas.append(f"✅ Precio: <b>{pesos(ahora_)}</b>")
    lineas += ["", f"#{cat} #LoQueTodoHombreNecesita" if cat else "#LoQueTodoHombreNecesita",
               "⚡ Los precios pueden cambiar en cualquier momento."]
    return "\n".join(lineas)


def telegram(token, metodo, datos):
    datos = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in datos.items()}
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{metodo}",
                                 data=urllib.parse.urlencode(datos).encode())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def publicar(token, canal, f):
    boton = {"inline_keyboard": [[{"text": "🛒 Ver oferta", "url": link_afiliado(f["link"])}]]}
    if f["imagen_url"]:
        try:
            return telegram(token, "sendPhoto", {"chat_id": canal, "photo": f["imagen_url"],
                                                 "caption": texto(f), "parse_mode": "HTML",
                                                 "reply_markup": boton})
        except Exception as e:
            print(f"Foto rechazada ({e}); se envia solo texto.")
    return telegram(token, "sendMessage", {"chat_id": canal, "text": texto(f),
                                           "parse_mode": "HTML", "reply_markup": boton,
                                           "disable_web_page_preview": "true"})


def main():
    prueba = "--prueba" in sys.argv
    momento = ahora()
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if not prueba and not manual and not (HORA_INICIO <= momento.hour < HORA_FIN):
        print("Fuera de horario.")
        return

    filas, sep = leer_csv()
    i = siguiente(filas, momento.date())
    if i is None:
        print("No hay ofertas pendientes. Agrega mas a ofertas.csv.")
        return
    f = filas[i]
    if prueba:
        print(texto(f))
        print("[Boton]", link_afiliado(f["link"]))
        return

    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    canal = os.environ.get("CHANNEL_ID", "").strip()
    if not token or not canal:
        sys.exit("Faltan los secrets TELEGRAM_TOKEN o CHANNEL_ID.")
    r = publicar(token, canal, f)
    if not r.get("ok"):
        sys.exit(f"Telegram respondio con error: {r}")
    filas[i]["publicado"] = momento.strftime("%Y-%m-%d %H:%M")
    guardar_csv(filas, sep)
    quedan = sum(1 for x in filas if not x["publicado"] and x["producto"])
    print(f"Publicada: {f['producto']}. Quedan {quedan} pendientes.")


if __name__ == "__main__":
    main()
