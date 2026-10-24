"""
Bot de ofertas "Lo Que Todo Hombre Necesita" (modo automatico).

Cada vez que GitHub Actions lo ejecuta (cada 15 minutos, de 8:00 a 23:00 hora
de CDMX) busca en Mercado Libre los productos de busquedas.txt, guarda su
historial de precios y, cuando detecta una bajada real, la publica de inmediato
en el canal con tu link de afiliado (matt_word / matt_tool).

Secrets que usa (se configuran en GitHub):
  TELEGRAM_TOKEN     token de @BotFather
  CHANNEL_ID         @usuario del canal
  ML_CLIENT_ID       id de tu app de Mercado Libre
  ML_CLIENT_SECRET   clave secreta de tu app de Mercado Libre
  ML_ACCESS_TOKEN    (lo llena el bot solo)
  ML_REFRESH_TOKEN   (lo llena el bot solo)
  ML_TOKEN_EXPIRA    (lo llena el bot solo)
  GH_PAT             token de GitHub para que el bot pueda renovar los de arriba

Uso:
  python bot.py                      ronda normal
  python bot.py --configurar CODIGO  conecta tu app de Mercado Libre (una vez)
"""

import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ------------------------------- ajustes ---------------------------------

# Tus codigos de afiliado de Mercado Libre (se agregan a cada link)
MATT_WORD = "rodriguezgael20231019205545"
MATT_TOOL = "55310688"

ZONA_MX = timezone(timedelta(hours=-6))       # Ciudad de Mexico
HORA_INICIO, HORA_FIN = 8, 23                 # horario en que busca y publica
MINUTOS_ENTRE_BUSQUEDAS = 12
RESULTADOS_POR_BUSQUEDA = 50
PRECIO_MINIMO = 120                           # ignora productos muy baratos

BAJADA_VS_HISTORIAL = 0.15                    # 15 % debajo de su precio normal
DESCUENTO_ML_SIN_HISTORIAL = 0.30             # 30 % de descuento marcado por ML
OBSERVACIONES_MINIMAS = 4                     # para confiar en el historial
DIAS_HISTORIAL = 14
DIAS_SIN_REPETIR = 7                          # no repetir el mismo producto
PUBLICACIONES_POR_RONDA = 4                   # para no saturar el canal
PUBLICACIONES_POR_DIA = 40
REPUTACIONES_OK = ("5_green", "4_light_green")

REDIRECT_URI = "https://www.google.com/"
CARPETA = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_ESTADO = os.path.join(CARPETA, "estado.json")
ARCHIVO_BUSQUEDAS = os.path.join(CARPETA, "busquedas.txt")

EMOJI = {"gym": "💪", "escuela": "🎒", "relojes": "⌚", "carro": "🚗",
         "perfumes": "🧴", "ropa": "👕", "gaming": "🎮", "setup": "🎥",
         "cuidado": "🧔"}


def ahora():
    return datetime.now(ZONA_MX)


def log(texto):
    print(f"[{ahora():%H:%M}] {texto}", flush=True)


# ------------------------------- estado -----------------------------------

def cargar_estado():
    base = {"ultima_busqueda": "", "historial": {}, "avisados": {},
            "reputaciones": {}, "avisos_hoy": {"fecha": "", "n": 0},
            "publicadas": []}
    if os.path.exists(ARCHIVO_ESTADO):
        with open(ARCHIVO_ESTADO, encoding="utf-8") as f:
            base.update(json.load(f))
    return base


def guardar_estado(estado):
    with open(ARCHIVO_ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1, sort_keys=True)


def leer_busquedas():
    busquedas = []
    with open(ARCHIVO_BUSQUEDAS, encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea or linea.startswith("#") or "|" not in linea:
                continue
            categoria, consulta = (p.strip() for p in linea.split("|", 1))
            busquedas.append((categoria.lower(), consulta))
    return busquedas


# ------------------------------- HTTP -------------------------------------

def pedir(url, datos=None, encabezados=None, intentos=2):
    cuerpo = urllib.parse.urlencode(datos).encode() if datos is not None else None
    req = urllib.request.Request(url, data=cuerpo, headers=encabezados or {})
    req.add_header("User-Agent", "BotOfertasLQTHN/1.0")
    for intento in range(intentos):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) or intento == intentos - 1:
                raise
        except urllib.error.URLError:
            if intento == intentos - 1:
                raise
        time.sleep(2)


# ------------------------------- Telegram ---------------------------------

class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def llamar(self, metodo, **datos):
        datos = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                 for k, v in datos.items() if v is not None}
        r = pedir(self.base + metodo, datos)
        if not r.get("ok"):
            raise RuntimeError(f"Telegram {metodo}: {r}")
        return r["result"]

    def foto_o_texto(self, chat_id, foto, texto, **extra):
        if foto:
            try:
                return self.llamar("sendPhoto", chat_id=chat_id, photo=foto,
                                   caption=texto, parse_mode="HTML", **extra)
            except Exception as e:
                log(f"Foto rechazada ({e}); mando solo texto.")
        return self.llamar("sendMessage", chat_id=chat_id, text=texto,
                           parse_mode="HTML", disable_web_page_preview=True, **extra)


def html(texto):
    return str(texto).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def pesos(valor):
    return f"${valor:,.0f}"


# ------------------------------- Mercado Libre ----------------------------

class MercadoLibre:
    API = "https://api.mercadolibre.com"

    def __init__(self):
        self.client_id = os.environ.get("ML_CLIENT_ID", "").strip()
        self.secret = os.environ.get("ML_CLIENT_SECRET", "").strip()
        self.access = os.environ.get("ML_ACCESS_TOKEN", "").strip()
        self.refresh = os.environ.get("ML_REFRESH_TOKEN", "").strip()
        try:
            self.expira = float(os.environ.get("ML_TOKEN_EXPIRA", "0") or 0)
        except ValueError:
            self.expira = 0

    # --- tokens ---
    def canjear_codigo(self, codigo):
        r = pedir(self.API + "/oauth/token", {
            "grant_type": "authorization_code", "client_id": self.client_id,
            "client_secret": self.secret, "code": codigo.strip(),
            "redirect_uri": REDIRECT_URI}, {"Accept": "application/json"})
        self._guardar_tokens(r)

    def renovar(self):
        if not (self.refresh and self.client_id and self.secret):
            return False
        r = pedir(self.API + "/oauth/token", {
            "grant_type": "refresh_token", "client_id": self.client_id,
            "client_secret": self.secret, "refresh_token": self.refresh},
            {"Accept": "application/json"})
        self._guardar_tokens(r)
        return True

    def _guardar_tokens(self, r):
        self.access = r["access_token"]
        self.refresh = r.get("refresh_token", self.refresh)
        self.expira = time.time() + int(r.get("expires_in", 21600))
        guardar_secret("ML_ACCESS_TOKEN", self.access)
        guardar_secret("ML_REFRESH_TOKEN", self.refresh)
        guardar_secret("ML_TOKEN_EXPIRA", str(int(self.expira)))
        log("Tokens de Mercado Libre renovados.")

    def preparar(self):
        if self.refresh and time.time() > self.expira - 1800:
            self.renovar()

    # --- consultas ---
    def get(self, ruta):
        enc = {"Authorization": f"Bearer {self.access}"} if self.access else {}
        try:
            return pedir(self.API + ruta, encabezados=enc)
        except urllib.error.HTTPError as e:
            if e.code == 401 and self.renovar():
                return pedir(self.API + ruta,
                             encabezados={"Authorization": f"Bearer {self.access}"})
            raise

    def buscar(self, consulta):
        q = urllib.parse.urlencode({"q": consulta, "limit": RESULTADOS_POR_BUSQUEDA,
                                    "condition": "new"})
        return self.get(f"/sites/MLM/search?{q}").get("results", [])

    def reputacion(self, seller_id):
        datos = self.get(f"/users/{seller_id}")
        return (datos.get("seller_reputation") or {}).get("level_id") or ""


def guardar_secret(nombre, valor):
    """Actualiza un secret del repositorio con el CLI de GitHub (usa GH_PAT)."""
    if not os.environ.get("GH_TOKEN"):
        log(f"Sin GH_PAT: no se guardo {nombre}.")
        return
    subprocess.run(["gh", "secret", "set", nombre, "--body", valor],
                   check=True, capture_output=True)


# ------------------------------- deteccion --------------------------------

def foto_grande(url):
    url = (url or "").replace("http://", "https://")
    return re.sub(r"-I\.(jpg|webp)$", r"-O.\1", url)


def evaluar(item, hist, hoy_txt):
    """Devuelve (es_oferta, precio_referencia, motivo)."""
    precio = item["precio"]
    obs = [p for f, p in hist.get("precios", [])
           if f >= (ahora() - timedelta(days=DIAS_HISTORIAL)).strftime("%Y-%m-%d")]
    dias = {f for f, _ in hist.get("precios", [])}
    if len(obs) >= OBSERVACIONES_MINIMAS and len(dias) >= 2:
        normal = statistics.median(obs)
        if precio <= normal * (1 - BAJADA_VS_HISTORIAL):
            pct = round((1 - precio / normal) * 100)
            return True, normal, f"bajó {pct}% frente a su precio de los últimos días"
        return False, normal, ""
    original = item.get("original")
    minimo = hist.get("minimo")
    if original and original > precio and precio <= original * (1 - DESCUENTO_ML_SIN_HISTORIAL):
        if minimo is None or precio <= minimo:
            pct = round((1 - precio / original) * 100)
            return True, original, f"Mercado Libre marca {pct}% de descuento"
    return False, None, ""


def actualizar_historial(hist, item, hoy_txt):
    precios = hist.setdefault("precios", [])
    if not precios or precios[-1] != [hoy_txt, item["precio"]]:
        precios.append([hoy_txt, item["precio"]])
    del precios[:-40]
    hist["minimo"] = min(item["precio"], hist.get("minimo") or item["precio"])
    hist["visto"] = hoy_txt


def ronda_busqueda(ml, tg, estado, canal):
    hoy_txt = ahora().strftime("%Y-%m-%d")
    if estado["avisos_hoy"]["fecha"] != hoy_txt:
        estado["avisos_hoy"] = {"fecha": hoy_txt, "n": 0}

    candidatos, vistos = [], set()
    for categoria, consulta in leer_busquedas():
        try:
            resultados = ml.buscar(consulta)
        except Exception as e:
            log(f"Busqueda '{consulta}' fallo: {e}")
            continue
        for r in resultados:
            iid = r.get("id")
            precio = r.get("price")
            if not iid or iid in vistos or not precio or precio < PRECIO_MINIMO:
                continue
            vistos.add(iid)
            item = {"id": iid, "titulo": r.get("title", ""), "precio": float(precio),
                    "original": float(r["original_price"]) if r.get("original_price") else None,
                    "link": r.get("permalink", ""), "foto": foto_grande(r.get("thumbnail")),
                    "vendedor": (r.get("seller") or {}).get("id"),
                    "oficial": bool(r.get("official_store_id")), "categoria": categoria}
            hist = estado["historial"].setdefault(iid, {})
            es_oferta, ref, motivo = evaluar(item, hist, hoy_txt)
            actualizar_historial(hist, item, hoy_txt)
            if es_oferta and not ya_avisado(estado, iid, item["precio"]):
                item.update(referencia=ref, motivo=motivo,
                            descuento=1 - item["precio"] / ref)
                candidatos.append(item)

    limpiar_historial(estado)
    candidatos.sort(key=lambda c: c["descuento"], reverse=True)
    enviados = 0
    for item in candidatos:
        if (enviados >= PUBLICACIONES_POR_RONDA
                or estado["avisos_hoy"]["n"] >= PUBLICACIONES_POR_DIA):
            break
        if not vendedor_confiable(ml, estado, item):
            continue
        try:
            publicar(tg, estado, canal, item)
        except Exception as e:
            log(f"No se pudo publicar {item['id']}: {e}")
            continue
        enviados += 1
    log(f"Revisados {len(vistos)} productos, {len(candidatos)} posibles ofertas, "
        f"{enviados} publicadas.")


def ya_avisado(estado, iid, precio):
    previo = estado["avisados"].get(iid)
    if not previo:
        return False
    hace = (ahora() - datetime.fromisoformat(previo["fecha"])).days
    return hace < DIAS_SIN_REPETIR and precio > previo["precio"] * 0.9


def vendedor_confiable(ml, estado, item):
    if item["oficial"]:
        return True
    vid = str(item["vendedor"] or "")
    if not vid:
        return False
    cache = estado["reputaciones"].get(vid)
    if not cache or cache["fecha"] < (ahora() - timedelta(days=7)).strftime("%Y-%m-%d"):
        try:
            nivel = ml.reputacion(vid)
        except Exception as e:
            log(f"No pude ver la reputacion de {vid}: {e}")
            return False
        cache = {"nivel": nivel, "fecha": ahora().strftime("%Y-%m-%d")}
        estado["reputaciones"][vid] = cache
    return cache["nivel"] in REPUTACIONES_OK


def limpiar_historial(estado):
    limite = (ahora() - timedelta(days=30)).strftime("%Y-%m-%d")
    for iid in [k for k, h in estado["historial"].items() if h.get("visto", "") < limite]:
        del estado["historial"][iid]


# ------------------------------- publicacion -------------------------------

def link_afiliado(permalink):
    """Link limpio del producto + tus codigos de afiliado."""
    base = re.split(r"[?#]", permalink or "", maxsplit=1)[0]
    return base + "?" + urllib.parse.urlencode({"matt_word": MATT_WORD,
                                                "matt_tool": MATT_TOOL})


def texto_canal(item):
    emoji = EMOJI.get(item["categoria"], "🔥")
    return (f"{emoji} <b>{html(item['titulo'])}</b>\n\n"
            f"❌ Antes: <s>{pesos(item['referencia'])}</s>\n"
            f"✅ Ahora: <b>{pesos(item['precio'])}</b>  "
            f"({round(item['descuento'] * 100)}% OFF)\n\n"
            f"#{item['categoria']} #LoQueTodoHombreNecesita\n"
            f"⚡ Los precios pueden cambiar en cualquier momento.")


def publicar(tg, estado, canal, item):
    link = link_afiliado(item["link"])
    boton = {"inline_keyboard": [[{"text": "🛒 Ver oferta", "url": link}]]}
    tg.foto_o_texto(canal, item["foto"], texto_canal(item), reply_markup=boton)
    estado["avisados"][item["id"]] = {"fecha": ahora().isoformat(), "precio": item["precio"]}
    estado["avisos_hoy"]["n"] += 1
    # Registro de las ultimas publicaciones (sirve para elegir productos de los videos)
    estado["publicadas"].append({
        "fecha": ahora().strftime("%Y-%m-%d %H:%M"), "categoria": item["categoria"],
        "titulo": item["titulo"], "antes": round(item["referencia"]),
        "ahora": round(item["precio"]), "descuento": round(item["descuento"] * 100),
        "link": link})
    del estado["publicadas"][:-150]
    log(f"Publicada: {item['titulo']} (-{round(item['descuento'] * 100)}%)")


# ------------------------------- principal --------------------------------

def toca_buscar(estado):
    momento = ahora()
    if not (HORA_INICIO <= momento.hour < HORA_FIN):
        return False
    if not estado["ultima_busqueda"]:
        return True
    previo = datetime.fromisoformat(estado["ultima_busqueda"])
    return momento - previo >= timedelta(minutes=MINUTOS_ENTRE_BUSQUEDAS)


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--configurar":
        MercadoLibre().canjear_codigo(sys.argv[2])
        log("Listo: Mercado Libre conectado.")
        return

    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    canal = os.environ.get("CHANNEL_ID", "").strip()
    if not token or not canal:
        sys.exit("Faltan los secrets TELEGRAM_TOKEN o CHANNEL_ID.")

    estado = cargar_estado()
    if not toca_buscar(estado):
        log("Fuera de horario o muy pronto desde la ultima busqueda.")
        return
    try:
        ml = MercadoLibre()
        ml.preparar()
        estado["ultima_busqueda"] = ahora().isoformat()
        ronda_busqueda(ml, Telegram(token), estado, canal)
    finally:
        guardar_estado(estado)


if __name__ == "__main__":
    main()
