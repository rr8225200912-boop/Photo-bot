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

