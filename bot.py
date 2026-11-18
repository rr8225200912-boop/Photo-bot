import asyncio
import logging
import os
import secrets
import shutil
import tempfile
from collections import OrderedDict
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)

import core

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

TOKEN = os.environ["BOT_TOKEN"]
# Optional: only these chat IDs may use the bot (comma separated). Empty = everyone.
ALLOWED = {int(x) for x in os.getenv("ALLOWED_IDS", "").replace(" ", "").split(",") if x}

MAX_DOWNLOAD = 20 * 1024 * 1024  # Telegram Bot API download limit
MAX_UPLOAD = 49 * 1024 * 1024    # Telegram Bot API upload limit

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
dp = Dispatcher()
slots = asyncio.Semaphore(2)  # at most 2 files processed at the same time

# Pending files (only IDs and the report text are kept in memory; no files stay on disk)
JOBS: "OrderedDict[str, dict]" = OrderedDict()
MAX_JOBS = 200


def allowed(chat_id: int) -> bool:
    return not ALLOWED or chat_id in ALLOWED


def add_job(**data) -> str:
    job_id = secrets.token_hex(4)
    JOBS[job_id] = data
    while len(JOBS) > MAX_JOBS:
        JOBS.popitem(last=False)
    return job_id


def keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧹 সব মেটাডাটা মুছুন", callback_data=f"clean:{job_id}")],
        [
            InlineKeyboardButton(text="📄 পূর্ণ রিপোর্ট", callback_data=f"full:{job_id}"),
            InlineKeyboardButton(text="✖️ বাতিল", callback_data=f"x:{job_id}"),
        ],
    ])


def classify(m: Message):
    """Returns (file_id, name, kind, size) or None."""
    if m.video:
        obj, kind = m.video, "video"
    elif m.animation:
        obj, kind = m.animation, "video"
    elif m.video_note:
        obj, kind = m.video_note, "video"
    elif m.document:
        obj = m.document
        mime = (obj.mime_type or "").lower()
        ext = Path(obj.file_name or "").suffix.lower()
        if mime.startswith("video/") or ext in core.VIDEO_EXT:
            kind = "video"
        elif mime.startswith("image/") or ext in core.IMAGE_EXT:
            kind = "photo"
        else:
            return None
    else:
        return None
    name = getattr(obj, "file_name", None) or ("video.mp4" if kind == "video" else "photo.jpg")
    return obj.file_id, name, kind, obj.file_size or 0


def safe_ext(name: str, kind: str) -> str:
    ext = Path(name).suffix.lower()
    allowed_ext = core.VIDEO_EXT if kind == "video" else core.IMAGE_EXT
    return ext if ext in allowed_ext else (".mp4" if kind == "video" else ".jpg")


def too_big(e: Exception) -> bool:
    return "too big" in str(e).lower()


# ------------------------------------------------------------------ commands
HELP = (
    "👋 <b>Metadata Cleaner</b>\n\n"
    "আমাকে একটা <b>ভিডিও</b> বা <b>ছবি</b> পাঠান।\n"
    "১️⃣ আমি দেখাব ভেতরে কী কী লুকানো তথ্য আছে (লোকেশন, ফোনের মডেল, তারিখ ইত্যাদি)\n"
    "২️⃣ এক ট্যাপে সব মুছে পরিষ্কার ফাইল ফেরত দেব\n\n"
    "⚠️ <b>ছবি অবশ্যই ফাইল হিসেবে পাঠান</b> (📎 → File/Document)। "
    "সাধারণভাবে পাঠালে টেলিগ্রাম নিজেই ছবির মেটাডাটা মুছে ফেলে, তখন আসল তথ্য দেখা যায় না।\n\n"
    "📦 সর্বোচ্চ সাইজ: ২০ MB"
)


@dp.message(CommandStart())
@dp.message(Command("help"))
async def start(m: Message):
    if allowed(m.chat.id):
        await m.answer(HELP)


@dp.message(F.photo)
async def compressed_photo(m: Message):
    if not allowed(m.chat.id):
        return
    await m.reply(
        "📎 এই ছবিটা টেলিগ্রাম কমপ্রেস করে ফেলেছে, তাই এর আসল মেটাডাটা আর নেই।\n"
        "আসল তথ্য দেখতে ছবিটা <b>File/Document</b> হিসেবে পাঠান (📎 → File)।"
    )


# ------------------------------------------------------------------ 1) analyze
@dp.message(F.video | F.animation | F.video_note | F.document)
async def on_media(m: Message):
    if not allowed(m.chat.id):
        return
    info = classify(m)
    if not info:
        await m.reply("❌ এটা ছবি বা ভিডিও ফাইল না।")
        return
    file_id, name, kind, size = info
    if size > MAX_DOWNLOAD:
        await m.reply("❌ ফাইল ২০ MB-এর বেশি। ছোট ফাইল পাঠান।")
        return

    status = await m.reply("🔍 মেটাডাটা পড়া হচ্ছে...")
    work = Path(tempfile.mkdtemp(prefix="meta_"))
    try:
        async with slots:
            src = work / ("input" + safe_ext(name, kind))
            await bot.download(file_id, destination=src)
            tags, coords = await core.analyze(src, kind)
        job_id = add_job(
            file_id=file_id, name=name, kind=kind, size=size, busy=False,
            full=core.full_report_text(name, tags, coords),
        )
        await status.edit_text(
            core.build_report(name, kind, size, tags, coords), reply_markup=keyboard(job_id)
        )
    except Exception as e:
        log.exception("analyze failed")
        await status.edit_text(
            "❌ ফাইল ডাউনলোড করা যায়নি (২০ MB-এর বেশি হতে পারে)।" if too_big(e)
            else "❌ কিছু সমস্যা হয়েছে, আবার চেষ্টা করুন।"
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------ buttons
def get_job(cq: CallbackQuery):
    return JOBS.get(cq.data.split(":", 1)[1])


@dp.callback_query(F.data.startswith("full:"))
async def cb_full(cq: CallbackQuery):
    if not allowed(cq.message.chat.id):
        return await cq.answer()
    job = get_job(cq)
    if not job:
        return await cq.answer("পুরনো অনুরোধ। ফাইলটা আবার পাঠান।", show_alert=True)
    await cq.answer()
    await cq.message.answer_document(
        BufferedInputFile(job["full"].encode(), filename="metadata_report.txt")
    )


@dp.callback_query(F.data.startswith("x:"))
async def cb_cancel(cq: CallbackQuery):
    if not allowed(cq.message.chat.id):
        return await cq.answer()
    JOBS.pop(cq.data.split(":", 1)[1], None)
    await cq.answer("বাতিল")
    await cq.message.edit_reply_markup(reply_markup=None)


# ------------------------------------------------------------------ 2) clean
@dp.callback_query(F.data.startswith("clean:"))
async def cb_clean(cq: CallbackQuery):
    msg = cq.message
    if not allowed(msg.chat.id):
        return await cq.answer()
    job_id = cq.data.split(":", 1)[1]
    job = JOBS.get(job_id)
    if not job:
        return await cq.answer("পুরনো অনুরোধ। ফাইলটা আবার পাঠান।", show_alert=True)
    if job["busy"]:
        return await cq.answer("প্রসেস চলছে, একটু অপেক্ষা করুন...")
    job["busy"] = True
    await cq.answer()
    await msg.edit_reply_markup(reply_markup=None)

    status = await msg.answer("⏳ লাইনে আছে...")
    work = Path(tempfile.mkdtemp(prefix="meta_"))
    kind, name = job["kind"], job["name"]
    try:
        async with slots:
            src = work / ("input" + safe_ext(name, kind))
            out_ext = ".mp4" if kind == "video" else src.suffix
            dst = work / ("clean" + out_ext)

            await status.edit_text("📥 ডাউনলোড হচ্ছে...")
            await bot.download(job["file_id"], destination=src)
            before, _ = await core.analyze(src, kind)

            await status.edit_text("🧹 মেটাডাটা মোছা ও যাচাই চলছে...")
            ok, left = await (core.clean_video if kind == "video" else core.clean_photo)(src, dst)

            if not ok:
                await status.edit_text("❌ প্রসেস করা যায়নি। অন্য ফাইল চেষ্টা করুন।")
                job["busy"] = False
                return
            if dst.stat().st_size > MAX_UPLOAD:
                await status.edit_text("❌ পরিষ্কার ফাইল টেলিগ্রামে পাঠানোর জন্য বেশি বড়।")
                job["busy"] = False
                return

            removed = sum(1 for _, t, _ in before if core.categorize(t) != "other")
            lines = [f"🧹 {removed}টা মেটাডাটা ট্যাগ মোছা হয়েছে।"]
            if left:
                lines.append("⚠️ যাচাই: কিছু ট্যাগ রয়ে গেছে: " + core.esc(", ".join(left[:8])))
            else:
                lines.append("✅ যাচাই: ফাইল সম্পূর্ণ পরিষ্কার।")

            await status.edit_text("📤 পাঠানো হচ্ছে...")
            await msg.answer_document(
                FSInputFile(dst, filename=f"clean_{Path(name).stem}{out_ext}"),
                caption="\n".join(lines),
            )
            await status.delete()
            JOBS.pop(job_id, None)
    except Exception as e:
        log.exception("clean failed")
        job["busy"] = False
        try:
            await status.edit_text(
                "❌ ফাইল ডাউনলোড করা যায়নি।" if too_big(e) else "❌ কিছু সমস্যা হয়েছে, আবার চেষ্টা করুন।"
            )
        except TelegramBadRequest:
            pass
    finally:
        # 3) always delete temporary files
        shutil.rmtree(work, ignore_errors=True)


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
