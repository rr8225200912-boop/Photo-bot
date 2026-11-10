import asyncio
import io
import json
import logging
import os
import urllib.parse
import urllib.request
from functools import lru_cache

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from exif_utils import analyze, compass

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("photo-meta-bot")

# ---- config (set these as environment variables, never hardcode them) ----
BOT_TOKEN = os.environ.get("BOT_TOKEN", "9052136582:AAEWNcBMsso_lyA1d3y12kjCFzPrTXQiQE8")
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "7935895229").strip()
ALLOWED = {x.strip() for x in os.environ.get("ALLOWED_CHAT_IDS", OWNER_CHAT_ID).split(",") if x.strip()}
# ALLOWED empty => bot is public. Otherwise only these chat IDs can use it.

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff", ".dng", ".avif")
MAX_BYTES = 20 * 1024 * 1024  # Telegram bot download limit


def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def allowed(update: Update) -> bool:
    if not ALLOWED:
        return True
    chat = update.effective_chat
    return bool(chat) and str(chat.id) in ALLOWED


# ---------------------------------------------------------------- geocoding
@lru_cache(maxsize=512)
def _geocode_sync(lat: float, lon: float):
    qs = urllib.parse.urlencode({"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 18,
                                 "addressdetails": 1, "accept-language": "bn,en"})
    req = urllib.request.Request(f"https://nominatim.openstreetmap.org/reverse?{qs}",
                                 headers={"User-Agent": "photo-meta-bot/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        return data.get("display_name"), data.get("address", {})
    except Exception as e:
        log.warning("geocode failed: %s", e)
        return None, {}


async def geocode(lat: float, lon: float):
    return await asyncio.to_thread(_geocode_sync, round(lat, 5), round(lon, 5))


# ---------------------------------------------------------------- formatting
def split_message(text: str, limit: int = 3800):
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > limit and cur:
            chunks.append(cur)
            cur = ""
        cur += block + "\n\n"
    if cur.strip():
        chunks.append(cur)
    return [c.strip() for c in chunks]


async def build_report(result: dict):
    parts = ["<b>🔎 Photo Metadata Report</b>"]
    for title, rows in result["sections"]:
        body = "\n".join(f"• {esc(k)}: <code>{esc(v)}</code>" if k in ("MD5", "SHA-256")
                         else f"• {esc(k)}: {esc(v)}" for k, v in rows)
        parts.append(f"<b>{title}</b>\n{body}")

    gps = result["gps"]
    if gps:
        lat, lon = gps["lat"], gps["lon"]
        g = [f"• Latitude: <code>{lat:.6f}</code>",
             f"• Longitude: <code>{lon:.6f}</code>",
             f"• Decimal: <code>{lat:.6f}, {lon:.6f}</code>",
             f"• DMS: {esc(gps['dms'])}",
             f"• ডেটার উৎস: {gps['source']}"]
        if "alt" in gps:
            g.append(f"• উচ্চতা: {gps['alt']:.1f} m")
        if "direction" in gps:
            g.append(f"• ক্যামেরা যেদিকে ছিল: {gps['direction']:.0f}° {compass(gps['direction'])} ({gps['direction_ref']})")
        if "speed_kmh" in gps:
            g.append(f"• গতি: {gps['speed_kmh']:.1f} km/h")
        if "h_error" in gps:
            g.append(f"• নির্ভুলতা: ±{gps['h_error']:.0f} m")
        if "dop" in gps:
            g.append(f"• DOP: {gps['dop']:.1f}")
        for key, label in (("datum", "Datum"), ("method", "পদ্ধতি"), ("satellites", "স্যাটেলাইট")):
            if key in gps:
                g.append(f"• {label}: {esc(gps[key])}")
        if not gps["null_island"]:
            addr, a = await geocode(lat, lon)
            if addr:
                g.append(f"• ঠিকানা: {esc(addr)}")
                for key, label in (("country", "দেশ"), ("state", "বিভাগ/স্টেট"), ("city", "শহর"),
                                   ("town", "শহর"), ("village", "গ্রাম"), ("postcode", "পোস্টকোড")):
                    if a.get(key):
                        g.append(f"  – {label}: {esc(a[key])}")
            g.append(
                f'🗺 <a href="https://www.google.com/maps?q={lat},{lon}">Google Maps</a> | '
                f'<a href="https://www.openstreetmap.org/?mlat={lat}&amp;mlon={lon}#map=17/{lat}/{lon}">OSM</a> | '
                f'<a href="https://www.google.com/maps/@?api=1&amp;map_action=pano&amp;viewpoint={lat},{lon}">Street View</a>'
            )
        parts.append("<b>📍 GPS লোকেশন</b>\n" + "\n".join(g))

    if result["warnings"]:
        parts.append("<b>⚠️ নোট</b>\n" + "\n".join(f"• {esc(w)}" for w in result["warnings"]))
    return split_message("\n\n".join(parts))


# ---------------------------------------------------------------- handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await update.message.reply_text(
        "👋 <b>Photo Metadata Bot</b>\n\n"
        "ছবির পূর্ণ মেটাডেটা ও GPS লোকেশন দেখতে ছবিটা <b>File/Document</b> হিসেবে পাঠাও "
        "(📎 Attach → File → ছবিটা বেছে নাও)।\n\n"
        "সাধারণ Photo হিসেবে পাঠালে Telegram কম্প্রেস করে মেটাডেটা মুছে ফেলে।\n\n"
        "আমি দেখাব: ক্যামেরা/ফোন, লেন্স, সময়, এক্সপোজার, GPS (দশমিক ও DMS), ঠিকানা, "
        "Google Maps/OSM/Street View লিংক, ম্যাপ পিন, ফাইল হ্যাশ, আর পুরো raw ডেটা JSON ফাইলে।",
        parse_mode=ParseMode.HTML,
    )


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = update.message
    doc = msg.document
    name = doc.file_name or "image"
    is_image = (doc.mime_type or "").startswith("image/") or name.lower().endswith(IMAGE_EXT)
    if not is_image:
        await msg.reply_text("এটা ছবি ফাইল মনে হচ্ছে না। JPG, HEIC, PNG, WebP, TIFF ইত্যাদি পাঠাও।")
        return
    if doc.file_size and doc.file_size > MAX_BYTES:
        await msg.reply_text("ফাইল ২০MB-এর বেশি, Telegram বট এটা ডাউনলোড করতে পারে না।")
        return

    status = await msg.reply_text("⏳ বিশ্লেষণ করছি...")
    try:
        tg_file = await doc.get_file()
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        raw = buf.getvalue()

        result = await asyncio.to_thread(analyze, raw, name)
        chunks = await build_report(result)

        await status.edit_text(chunks[0], parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        for c in chunks[1:]:
            await msg.reply_text(c, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

        gps = result["gps"]
        if gps and not gps["null_island"]:
            await msg.reply_location(latitude=gps["lat"], longitude=gps["lon"])

        dump = json.dumps(result["raw"], indent=2, ensure_ascii=False, default=str).encode("utf-8")
        await msg.reply_document(document=io.BytesIO(dump), filename=f"{os.path.splitext(name)[0]}_metadata.json",
                                 caption="📄 পুরো raw মেটাডেটা (JSON)")
    except Exception as e:
        log.exception("failed to process %s", name)
        await status.edit_text(f"❌ ছবি পড়া যায়নি ({type(e).__name__}). ফাইলটা নষ্ট হতে পারে বা সাপোর্টেড ফরম্যাট না।")


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await update.message.reply_text(
        "⚠️ Photo হিসেবে পাঠালে Telegram মেটাডেটা মুছে ফেলে।\n"
        "ছবিটা আবার <b>File/Document</b> হিসেবে পাঠাও: 📎 Attach → File।",
        parse_mode=ParseMode.HTML,
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=context.error)


async def post_init(app: Application):
    if OWNER_CHAT_ID:
        try:
            await app.bot.send_message(OWNER_CHAT_ID, "✅ Photo Metadata Bot চালু হয়েছে। ছবি File হিসেবে পাঠাও।")
        except Exception as e:
            log.warning("could not notify owner: %s", e)


def main():
    app = (Application.builder().token(BOT_TOKEN)
           .read_timeout(60).write_timeout(60).connect_timeout(30)
           .post_init(post_init).build())
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_error_handler(on_error)
    log.info("Bot started (access restricted: %s)", bool(ALLOWED))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
