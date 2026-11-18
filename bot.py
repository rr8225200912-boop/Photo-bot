import asyncio
import hashlib
import logging
import os
import re
import tempfile

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from builder import BuildError, build_apk

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["BOT_TOKEN"]
OWNER = int(os.environ.get("OWNER_ID", "0") or 0)
lock = asyncio.Lock()

HELP = (
    "আমাকে একটা .zip ফাইল পাঠান। ভেতরে index.html থাকলে সেটাকে অ্যাপ বানিয়ে APK পাঠিয়ে দেব।\n"
    "অ্যাপের নাম দিতে চাইলে zip পাঠানোর সময় ক্যাপশনে নাম লিখুন।\n"
    "বিল্ডে ৩ থেকে ১০ মিনিট লাগতে পারে।"
)


async def need_owner(update: Update) -> bool:
    """Returns True if the user must be stopped."""
    uid = update.effective_user.id
    if not OWNER:
        await update.message.reply_text(
            f"আপনার Telegram ID: {uid}\n"
            f"Railway-র Variables-এ OWNER_ID = {uid} বসিয়ে বটটা Redeploy করুন। তারপর আবার /start দিন।"
        )
        return True
    return uid != OWNER


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await need_owner(update):
        return
    await update.message.reply_text("স্বাগতম! " + HELP)


async def on_doc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await need_owner(update):
        return
    m = update.message
    d = m.document
    if not (d.file_name or "").lower().endswith(".zip"):
        await m.reply_text("শুধু .zip ফাইল পাঠান।")
        return
    if d.file_size and d.file_size > 20 * 1024 * 1024:
        await m.reply_text("zip ২০ MB-এর বেশি হলে Telegram বট ডাউনলোড করতে পারে না।")
        return
    name = (m.caption or "").strip() or os.path.splitext(d.file_name)[0]
    name = name[:40]
    if lock.locked():
        await m.reply_text("আগের একটা বিল্ড চলছে। ওটা শেষ হলে এটা শুরু হবে।")
    async with lock:
        await m.reply_text("⏳ বিল্ড শুরু হয়েছে। অপেক্ষা করুন, শেষ হলে APK পাঠাব।")
        with tempfile.TemporaryDirectory() as tmp:
            zp = os.path.join(tmp, "in.zip")
            f = await d.get_file()
            await f.download_to_drive(zp)
            try:
                apk = await build_apk(zp, name, tmp)
            except BuildError as e:
                await m.reply_text("❌ বিল্ড হয়নি।\n\n" + str(e))
                return
            except Exception as e:
                logging.exception("build failed")
                await m.reply_text(f"❌ অপ্রত্যাশিত সমস্যা: {e}")
                return
            fname = (re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-") or "app") + ".apk"
            with open(apk, "rb") as fh:
                await m.reply_document(
                    fh, filename=fname, read_timeout=180, write_timeout=180,
                    caption="✅ APK তৈরি। খুলে ইনস্টল করুন। \"Install unknown apps\" অনুমতি চাইলে অনুমতি দিন।")


async def other(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await need_owner(update):
        return
    await update.message.reply_text(HELP)


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(MessageHandler(filters.Document.ALL, on_doc))
    app.add_handler(MessageHandler(filters.ALL, other))
    app.run_polling()


if __name__ == "__main__":
    main()
