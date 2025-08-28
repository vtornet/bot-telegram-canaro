import os
import re
import requests
import logging
from functools import lru_cache
from io import BytesIO
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any
import xml.etree.ElementTree as ET

# Matplotlib para gráfico de precios
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from telegram.constants import ChatType
import logging
log = logging.getLogger(__name__)

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# =========================
# Configuración (variables de entorno recomendadas)
# =========================
raw_token = os.environ.get("BOT_TOKEN", "")
BOT_TOKEN = raw_token.strip()
if (not BOT_TOKEN) or (":" not in BOT_TOKEN) or (len(BOT_TOKEN) < 30):
    raise RuntimeError("Falta o es inválido BOT_TOKEN. Añade BOT_TOKEN en Railway.")
GROUP_NAME = os.environ.get("GROUP_NAME", "Nuevas criptomonedas e IA")
VS_CURRENCY_DEFAULT = os.environ.get("VS_CURRENCY_DEFAULT", "eur").lower()

# Límite multimedia por usuario/día (igual que tu bot original)
LIMITE_DIARIO = int(os.environ.get("LIMITE_DIARIO", "5"))

# Rate-limit del comando /precio (minutos) — se aplica por chat+moneda (admins exentos)
PRICE_RATE_LIMIT_MINUTES = int(os.environ.get("PRICE_RATE_LIMIT_MINUTES", "60"))

# Feeds RSS para /noticias (separados por coma).
NEWS_RSS_FEEDS_RAW = os.environ.get(
    "NEWS_RSS_FEEDS",
    "'https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml,https://cointelegraph.com/rss'"
)
# Limpiamos las comillas que necesita Railway y separamos por coma
NEWS_RSS_FEEDS = [url.strip().strip("'") for url in NEWS_RSS_FEEDS_RAW.split(',')]
NEWS_MAX_ITEMS = int(os.environ.get("NEWS_MAX_ITEMS", "10"))  # nº máximo de ítems a mostrar

# HTTP
REQUEST_TIMEOUT = 15
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "TelegramBot/1.0 (+python-telegram-bot)"})

# Estado en memoria
multimedia_usuarios: Dict[int, Dict[int, Dict[str, Any]]] = {}
# Estructura: ultimo_precio[chat_id][coin_id] = {"hora": dt, "mensaje_id": int}
ultimo_precio: Dict[int, Dict[str, Dict[str, Any]]] = {}

# Logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# =========================
# Moderación
# =========================
palabras_prohibidas = [
    "idiota", "imbécil", "puta", "puto", "gilipollas", "maldito", "cabrón", "cabrona",
    "pendejo", "pendeja", "coño", "joder", "carajo", "culero", "pelotudo", "verga",
    "polla", "pollas", "chingar", "chingada", "maricón", "zorra", "subnormal",
    "chupapito", "chupapitos", "mamahuevos", "mamaguevo", "mamaguevos",
    "chupa pitos", "chupa pito", "mama huevos", "mama huevo", "rozame el ano",
    "cabron", "imbecil", "gilí", "pringado", "capullo", "soplapollas", "tontolaba",
    "meapilas", "mindundi", "caraculo", "comemierda", "toca huevos", "pinche",
    "naco", "güey", "pendejazo", "mamón", "cagón", "traga mierda", "chupamedias",
    "boludo", "mogólico", "forro", "conchudo", "bobo", "chupaculo", "cabeza de termo",
    "weón", "culiao", "saco wea", "conchesumadre", "maraco", "picao a la araña",
    "longi", "huevón", "gonorrea", "carechimba", "careverga", "marrano", "malparido",
    "zarrapastroso", "mamagüevo", "pajuo", "mardito", "mariquito", "carapicha",
    "jalabolas", "perolito", "chúpame", "chupame", "cojudo", "pavo",
    "chibolo de mierda", "conchatumare", "pariguayo", "bocón", "chopo", "lambón",
    "bellaco", "mamabicho", "pendejete", "cafre", "singao", "fajao", "descarao",
    "chivatón", "caremondá", "careculo", "carapinga", "caraverga", "verguero",
    "mierdero", "tarado", "imbécilazo", "estúpido de mierda", "merluzo"
]

patrones_spam = [
    r"http[s]?://[^ ]*(\.cn|\.ru|binancegift|airdrops?|bonus|freecrypto)",
    r"gana\s+dinero\s+r[aá]pido",
    r"hazte\s+rico",
    r"multiplica\s+tu\s+inversi[oó]n",
    r"env[ií]a\s+(usdt|btc|eth)\s+a\s+esta\s+direcci[oó]n",
    r"airdrop",
    r"criptopumpva", r"@criptopumpva",
    r"@criptosenals", r"criptosenals",
    r"miren\s+este\s+canal",
    r"anyone\s+sell\s+pi",
    r"retira\s+tu\s+bono",
    r"@criptoppumps", r"criptoppumps",
    r"trumpdropwalletbot",
]

# =========================
# Utilidades
# =========================
def escape_md(text: str) -> str:
    chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join('\\' + c if c in chars else c for c in (text or ""))

def escape_html(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def obtener_nombre_usuario(user) -> str:
    return escape_md(user.first_name or (f"@{user.username}" if user.username else "Usuario"))

# =========================
# /start /ayuda /reportar /multimedia /bienvenida /moderación
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nombre = obtener_nombre_usuario(update.effective_user)
    await update.message.reply_text(
        escape_md(f"¡Hola {nombre}! Bienvenid@ a *{GROUP_NAME}*. Usa /ayuda para ver comandos."),
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cuerpo = (
        "/noticias [palabra] — Últimos titulares (filtra por palabra opcional).\n"
        "/precio <moneda> [divisa] [periodo] — Precio y gráfico (ej.: `/precio btc eur 7d`).\n"
        "/reportar <motivo> — Reporta a los administradores.\n"
        "/multimedia — Consulta tus envíos multimedia restantes hoy.\n"
        "/ayuda — Muestra esta lista de comandos."
    )
    await update.message.reply_text(
        escape_md("ℹ️ *Comandos Disponibles:*\n\n" + cuerpo),
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def reportar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    nombre = obtener_nombre_usuario(user)
    motivo_raw = " ".join(context.args).strip() if context.args else "Motivo no especificado"
    motivo = escape_md(motivo_raw)
    menciones_admins = escape_md("@admins")  # ajusta si quieres mencionar usuarios concretos
    await update.message.reply_text(
        f"📣 *{nombre}* ha enviado un reporte\\.\n*Motivo:* {motivo}\nAdmins notificados: {menciones_admins}",
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def multimedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_id = user.id
    hoy = datetime.now(timezone.utc).date()

    if chat_id not in multimedia_usuarios:
        multimedia_usuarios[chat_id] = {}
    if user_id not in multimedia_usuarios[chat_id] or multimedia_usuarios[chat_id][user_id]["fecha"] != hoy:
        multimedia_usuarios[chat_id][user_id] = {"fecha": hoy, "conteo": 0}

    conteo = multimedia_usuarios[chat_id][user_id]["conteo"]
    restante = max(0, LIMITE_DIARIO - conteo)
    msg = f"📷 Multimedia Disponible:\n\nHas enviado {conteo} de {LIMITE_DIARIO} archivos hoy.\nTe quedan {restante}."
    await update.message.reply_text(escape_md(msg), parse_mode=ParseMode.MARKDOWN_V2)

async def controlar_envio_multimedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_id = user.id
    hoy = datetime.now(timezone.utc).date()

    admins = await context.bot.get_chat_administrators(chat_id)
    if user_id in [a.user.id for a in admins]:
        return

    if chat_id not in multimedia_usuarios:
        multimedia_usuarios[chat_id] = {}
    if user_id not in multimedia_usuarios[chat_id] or multimedia_usuarios[chat_id][user_id]["fecha"] != hoy:
        multimedia_usuarios[chat_id][user_id] = {"fecha": hoy, "conteo": 0}

    if multimedia_usuarios[chat_id][user_id]["conteo"] >= LIMITE_DIARIO:
        try:
            await update.message.delete()
        except Exception as e:
            log.warning(f"No se pudo borrar multimedia extra: {e}")
        await context.bot.send_message(
            chat_id, f"⚠️ *{escape_md(obtener_nombre_usuario(user))}*, límite diario de {LIMITE_DIARIO} alcanzado.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
    else:
        multimedia_usuarios[chat_id][user_id]["conteo"] += 1

async def dar_bienvenida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        nombre = obtener_nombre_usuario(member)
        texto = (
            f"¡Bienvenid@ {nombre} a *{GROUP_NAME}* 🚀!\n\n"
            "Normas:\n"
            "• Respeto a todos.\n"
            "• No spam ni publicidad.\n"
            "• Contenido relacionado con cripto/IA.\n"
            f"• Límite multimedia: {LIMITE_DIARIO}/día.\n\n"
            "Escribe /ayuda para ver los comandos."
        )
        await update.message.reply_text(escape_md(texto), parse_mode=ParseMode.MARKDOWN_V2)

async def analizar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    texto = update.message.text.lower()
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id

    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        if user_id in [a.user.id for a in admins]:
            return
    except Exception as e:
        log.warning(f"Admins error: {e}")
        return

    if any(p in texto for p in palabras_prohibidas):
        try:
            await update.message.delete()
            await context.bot.send_message(
                chat_id,
                f"⚠️ *{escape_md(obtener_nombre_usuario(user))}*, mensaje eliminado por lenguaje ofensivo.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            log.warning(f"No se pudo borrar mensaje ofensivo: {e}")
        return

    for patron in patrones_spam:
        if re.search(patron, texto):
            try:
                await update.message.delete()
            except Exception as e:
                log.warning(f"No se pudo borrar spam: {e}")
            return

# =========================
# Precio / CoinGecko
# =========================
PERIODO_MAP = {"1d": 1, "7d": 7, "30d": 30, "90d": 90, "180d": 180, "365d": 365}

def parse_precio_args(args):
    coin_query, vs, days = None, VS_CURRENCY_DEFAULT, 1
    if not args:
        return coin_query, vs, days
    coin_query = args[0].strip().lower()
    if len(args) >= 2 and re.fullmatch(r"[a-z]{3,5}", args[1].strip().lower()):
        vs = args[1].strip().lower()
    if len(args) >= 3 and args[2].strip().lower() in PERIODO_MAP:
        days = PERIODO_MAP[args[2].strip().lower()]
    return coin_query, vs, days

@lru_cache(maxsize=512)
def _search_coingecko(query: str):
    try:
        r = SESSION.get("https://api.coingecko.com/api/v3/search", params={"query": query}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Búsqueda CoinGecko: {e}")
        return None

def resolve_coin_id(coin_query: str) -> str | None:
    if not coin_query:
        return None
    q = coin_query.lower().strip()
    aliases = {
        "pi": "pi-network", "pi-network": "pi-network", "pinetwork": "pi-network",
        "btc": "bitcoin", "eth": "ethereum", "sol": "solana", "ada": "cardano",
        "xrp": "ripple", "doge": "dogecoin", "matic": "polygon"
    }
    if q in aliases:
        return aliases[q]
    data = _search_coingecko(q)
    if not data or "coins" not in data:
        return None
    coins = data["coins"]
    for c in coins:
        if c.get("symbol", "").lower() == q: return c.get("id")
    for c in coins:
        if c.get("id", "").lower() == q: return c.get("id")
    for c in coins:
        if c.get("name", "").lower() == q: return c.get("id")
    return coins[0].get("id") if coins else None

def fetch_coin_detail(coin_id: str):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}"
    params = {"localization": "false", "tickers": "false", "market_data": "true",
              "community_data": "false", "developer_data": "false", "sparkline": "false"}
    r = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

def fetch_market_chart(coin_id: str, vs: str, days: int):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    params = {"vs_currency": vs, "days": str(days)}
    r = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

def plot_chart(times, values, title: str):
    plt.figure(figsize=(8, 4))
    plt.plot(times, values, linewidth=2)
    plt.title(title, fontsize=14)
    plt.xlabel("Fecha/Hora", fontsize=10)
    plt.ylabel("Precio", fontsize=10)
    plt.grid(True, linestyle="--", alpha=0.5)
    plt.tight_layout()
    if (times[-1] - times[0]).days >= 1:
        plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%d-%m'))
    else:
        plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    buf = BytesIO()
    plt.savefig(buf, format="png")
    buf.seek(0)
    plt.close()
    return buf

# (Asegúrate de tener "import os" y "import logging" al principio de tu archivo)

async def precio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f">>> COMANDO RECIBIDO: /precio del usuario {update.effective_user.id} con args: {context.args}")

    CMC_API_KEY = os.environ.get("CMC_API_KEY")
    if not CMC_API_KEY:
        await update.message.reply_text("❌ La clave de API de CoinMarketCap no está configurada.")
        return

    if not context.args:
        await update.message.reply_text("Uso: `/precio <símbolo>` (ej: `/precio btc`)", parse_mode=ParseMode.MARKDOWN_V2)
        return

    symbol = context.args[0].upper()

    url = 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest'
    headers = {
        'Accepts': 'application/json',
        'X-CMC_PRO_API_KEY': CMC_API_KEY,
    }
    params = {
        'symbol': symbol,
        'convert': 'EUR' # Puedes cambiar 'EUR' por 'USD' o la divisa que prefieras
    }

    try:
        print(f">>> PRECIO (CMC): Buscando datos para {symbol}...")
        r = SESSION.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status() # Lanza un error si la petición falla (ej: 4xx, 5xx)
        data = r.json()

        if not data['data'] or symbol not in data['data']:
            await update.message.reply_text(f"❌ No se encontró la moneda con el símbolo: {symbol}")
            return

        coin_data = data['data'][symbol]
        quote = coin_data['quote']['EUR']

        name = coin_data.get('name', 'N/A')
        price = quote.get('price', 0)
        market_cap = quote.get('market_cap', 0)
        volume_24h = quote.get('volume_24h', 0)
        change_24h = quote.get('percent_change_24h', 0)

        tendencia = "📈" if (change_24h or 0) >= 0 else "📉"
        vs_upper = "EUR"

        mensaje = (
            f"*💰 Precio de {escape_md(name)} \\({escape_md(symbol)}\\)*\n\n"
            + f"• *Precio actual:* {escape_md(f'{price:,.6f}')} {vs_upper} {tendencia}\n"
            + f"• *Cambio 24h:* {escape_md(f'{change_24h:.2f}')}\\%\n"
            + f"• *Market Cap:* {escape_md(f'{market_cap:,.0f}')} {vs_upper}\n"
            + f"• *Volumen \\(24h\\):* {escape_md(f'{volume_24h:,.0f}')} {vs_upper}\n"
        )

        print(">>> PRECIO (CMC): Enviando respuesta...")
        await update.message.reply_text(mensaje, parse_mode=ParseMode.MARKDOWN_V2)

    except Exception as e:
        print(f"!!!!!!!! ERROR EN PRECIO (CMC): {e}")
        log.error(f"Precio CMC error: {e}")
        await update.message.reply_text("❌ No se pudo obtener la información. Verifica el símbolo o inténtalo más tarde.")

# =========================
# Noticias por RSS (CoinDesk, Cointelegraph, etc.)
# =========================
def fetch_rss_items(url: str) -> List[Dict[str, str]]:
    items = []
    try:
        r = SESSION.get(url.strip(), timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        # RSS habitual: channel/item
        for item in root.findall(".//item"):
            title = item.findtext("title") or ""
            link = item.findtext("link") or ""
            pub = item.findtext("pubDate") or ""
            items.append({"title": title.strip(), "link": link.strip(), "pubDate": pub.strip()})
        # Atom fallback: entry
        if not items:
            for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
                title = entry.findtext("{http://www.w3.org/2005/Atom}title") or ""
                link_el = entry.find("{http://www.w3.org/2005/Atom}link")
                link = link_el.attrib.get("href") if link_el is not None else ""
                pub = entry.findtext("{http://www.w3.org/2005/Atom}updated") or ""
                items.append({"title": title.strip(), "link": link.strip(), "pubDate": pub.strip()})
    except Exception as e:
        log.warning(f"RSS fallo {url}: {e}")
    return items

def parse_date_maybe(s: str):
    # Intento best-effort; si falla, devolvemos 0 para ordenar al final
    try:
        return datetime.strptime(s[:25], "%a, %d %b %Y %H:%M:%S")
    except Exception:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return datetime.min

async def noticias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyword = " ".join(context.args).strip().lower()
    all_items: List[Dict[str, str]] = []
    for feed in NEWS_RSS_FEEDS:
        all_items.extend(fetch_rss_items(feed))
    # Filtrado por palabra
    if keyword:
        all_items = [it for it in all_items if keyword in (it["title"] or "").lower()]
    # Ordenar por fecha (desc)
    all_items.sort(key=lambda it: parse_date_maybe(it.get("pubDate", "")), reverse=True)
    # Quitar duplicados por link
    seen = set()
    dedup = []
    for it in all_items:
        if it["link"] in seen: continue
        seen.add(it["link"])
        dedup.append(it)
    items = dedup[:NEWS_MAX_ITEMS] or []

    if not items:
        msg = "📰 No encontré titulares en este momento." + (f" (filtro: {keyword})" if keyword else "")
        await update.message.reply_text(escape_md(msg), parse_mode=ParseMode.MARKDOWN_V2)
        return

    # Usamos HTML para evitar problemas de escaped en URLs
    lines = ["<b>📰 Últimos titulares</b>" + (f" — filtro: <i>{escape_html(keyword)}</i>" if keyword else "")]
    for it in items:
        title = escape_html(it["title"])
        link = escape_html(it["link"])
        lines.append(f"• <a href=\"{link}\">{title}</a>")
    html = "\n".join(lines)
    await update.message.reply_text(html, parse_mode=ParseMode.HTML, disable_web_page_preview=False)

# =========================
# App
# =========================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ayuda", ayuda))
    app.add_handler(CommandHandler("reportar", reportar))
    app.add_handler(CommandHandler("multimedia", multimedia))
    app.add_handler(CommandHandler("precio", precio))
    app.add_handler(CommandHandler("noticias", noticias))

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, dar_bienvenida))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), analizar_mensaje))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO, controlar_envio_multimedia))

    app.run_polling()

if __name__ == "__main__":
    main()





