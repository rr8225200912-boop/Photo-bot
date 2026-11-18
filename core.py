"""Metadata reading, cleaning and verification (no Telegram code here)."""
import asyncio
import html
import json
import logging
import os
import re
import shutil
from pathlib import Path

log = logging.getLogger("core")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic", ".heif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".3gp", ".mts", ".mpg", ".mpeg", ".wmv", ".flv"}


# ---------------------------------------------------------------- helpers
async def run(*args):
    try:
        p = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError:
        return 127, b"", f"{args[0]} not installed".encode()
    out, err = await p.communicate()
    return p.returncode, out, err


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def short(v: str, n: int = 60) -> str:
    v = " ".join(str(v).split())
    return v if len(v) <= n else v[: n - 1] + "…"


def fmt_size(b: int) -> str:
    return f"{b / 1024 / 1024:.1f} MB" if b >= 1024 * 1024 else f"{b / 1024:.0f} KB"


# ---------------------------------------------------------------- reading
SKIP_GROUPS = {"System", "File", "ExifTool", "Composite"}
ZERO_DATE = re.compile(r"^0000[:\-]00[:\-]00")


async def exif_read(path: Path):
    """All metadata tags as a list of (group, tag, value) using ExifTool."""
    _, out, _ = await run("exiftool", "-json", "-G1", "-a", "-s", str(path))
    try:
        data = json.loads(out)[0]
    except Exception:
        return []
    tags = []
    for key, val in data.items():
        if ":" not in key:
            continue
        group, tag = key.split(":", 1)
        if group in SKIP_GROUPS:
            continue
        val = str(val)
        if ZERO_DATE.match(val):
            continue
        tags.append((group, tag, val))
    return tags


async def ffprobe_json(path: Path) -> dict:
    code, out, _ = await run(
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", "-show_chapters", str(path),
    )
    try:
        return json.loads(out) if code == 0 else {}
    except Exception:
        return {}


HARMLESS_FF = {"handler_name", "vendor_id", "language", "major_brand", "minor_version", "compatible_brands"}


def ffprobe_tags(info: dict):
    tags = []
    for k, v in (info.get("format", {}).get("tags") or {}).items():
        tags.append(("ffprobe", k, str(v)))
    for s in info.get("streams", []):
        for k, v in (s.get("tags") or {}).items():
            tags.append(("ffprobe", k, str(v)))
    return [t for t in tags if t[1].lower() not in HARMLESS_FF]


async def gps_coords(path: Path, ff_tags=()):
    _, out, _ = await run(
        "exiftool", "-json", "-n", "-GPSLatitude", "-GPSLongitude", "-GPSCoordinates", str(path)
    )
    try:
        d = json.loads(out)[0]
    except Exception:
        d = {}
    lat, lon = d.get("GPSLatitude"), d.get("GPSLongitude")
    if lat is None or lon is None:
        raw = d.get("GPSCoordinates")
        if not raw:
            for _, k, v in ff_tags:  # ffprobe fallback: "+23.8103+090.4125/"
                if k.lower() in ("location", "com.apple.quicktime.location.iso6709"):
                    raw = v
        nums = re.findall(r"[+-]?\d+\.\d+|[+-]\d+", str(raw or ""))
        if len(nums) >= 2:
            lat, lon = nums[0], nums[1]
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    return (lat, lon) if (-90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon)) else None


async def analyze(path: Path, kind: str):
    """Returns (tags, coords)."""
    tags = await exif_read(path)
    ff = []
    if kind == "video":
        ff = ffprobe_tags(await ffprobe_json(path))
        if not tags:
            tags = ff
    return tags, await gps_coords(path, ff)


# ---------------------------------------------------------------- report
CATS = [
    ("gps", "📍 লোকেশন"),
    ("device", "📱 ডিভাইস"),
    ("person", "👤 ব্যক্তিগত তথ্য"),
    ("time", "🕒 তারিখ / সময়"),
    ("software", "💻 সফটওয়্যার"),
]
DEVICE = {
    "make", "model", "lensmodel", "lensmake", "lensid", "lens", "serialnumber", "bodyserialnumber",
    "cameraserialnumber", "internalserialnumber", "lensserialnumber", "hostcomputer", "androidversion",
    "manufacturer", "devicemanufacturer", "devicemodelname", "cameramodelname", "uniquecameramodel",
    "firmwareversion", "firmware",
}
SOFTWARE = {
    "software", "encoder", "creatortool", "processingsoftware", "writingapplication", "producer",
    "historysoftwareagent", "writinglibrary",
}
PERSON_RE = re.compile(
    r"owner|artist|author|copyright|comment|keyword|caption|creator|city|country|province|email|phone|"
    r"address|credit|byline|by-line|rights|prompt|parameters|workflow"
)
PERSON = {"title", "subject", "description", "imagedescription", "source"}
TIME_RE = re.compile(r"date|creation|offsettime|subsectime|timecreated|timezone")


def categorize(tag: str) -> str:
    t = tag.lower()
    if t.startswith(("gps", "location")):
        return "gps"
    if t in DEVICE:
        return "device"
    if t in SOFTWARE:
        return "software"
    if t in PERSON or PERSON_RE.search(t):
        return "person"
    if TIME_RE.search(t):
        return "time"
    return "other"


def build_report(name, kind, size, tags, coords) -> str:
    icon = "🎬" if kind == "video" else "🖼"
    out = [f"{icon} <b>{esc(short(name, 40))}</b> · {fmt_size(size)}", ""]
    sections = {k: [] for k, _ in CATS}
    other, seen = 0, set()
    for _, t, v in tags:
        c = categorize(t)
        if c == "other":
            other += 1
            continue
        if (c, t, v) in seen:
            continue
        seen.add((c, t, v))
        sections[c].append((t, v))

    if coords:
        lat, lon = coords
        out.append("📍 <b>লোকেশন পাওয়া গেছে!</b>")
        out.append(f'<a href="https://maps.google.com/?q={lat:.6f},{lon:.6f}">{lat:.6f}, {lon:.6f}</a> (ম্যাপে দেখুন)')
        out.append("")
        sections["gps"] = [(t, v) for t, v in sections["gps"] if t.lower() == "gpsaltitude"]

    private = sum(len(x) for x in sections.values()) + (1 if coords and not sections["gps"] else 0)
    for key, title in CATS:
        items = sections[key]
        if not items:
            continue
        out.append(f"<b>{title}</b>")
        for t, v in items[:6]:
            out.append(f"• {esc(t)}: {esc(short(v))}")
        if len(items) > 6:
            out.append(f"  …আরও {len(items) - 6}টা")
        out.append("")

    if private == 0:
        out.append("✅ কোনো ব্যক্তিগত মেটাডাটা পাওয়া যায়নি।")
    else:
        out.append(f"⚠️ মোট {private}টা ব্যক্তিগত ট্যাগ পাওয়া গেছে।")
    if other:
        out.append(f"ℹ️ টেকনিক্যাল ট্যাগ: {other}টা (পূর্ণ রিপোর্টে দেখুন)")
    return "\n".join(out)[:3900]


def full_report_text(name, tags, coords) -> str:
    lines = [f"File: {name}", ""]
    if coords:
        lines += [f"GPS: {coords[0]:.6f}, {coords[1]:.6f}", ""]
    lines += [f"[{g}] {t}: {v}" for g, t, v in tags] or ["(no metadata found)"]
    return "\n".join(lines)


# ---------------------------------------------------------------- verification
FLAG_GROUPS = re.compile(
    r"^(EXIF|IFD|XMP|IPTC|MakerNotes|GPS|Photoshop|Keys|UserData|ItemList|Trailer|MPF|ThumbnailIFD|"
    r"PrintIM|Samsung|Apple|Canon|Nikon|Sony|Google|Comment|Meta)"
)
RISKY_NAME = re.compile(
    r"gps|location|latitude|longitude|make|model|software|serial|owner|author|artist|creator|copyright|"
    r"comment|description|title|keyword|subject|lens|device|host|encoder|date|timezone|offsettime|email|"
    r"phone|address|city|country|prompt|parameters|workflow|thumbnail|uuid|userid|camera|android|firmware",
    re.I,
)
EXEMPT = {
    "Orientation", "HandlerDescription", "HandlerVendorID", "HandlerType", "HandlerClass",
    "MediaLanguageCode", "ExifByteOrder",
}


def raw_gps_scan(path: Path) -> bool:
    """Looks inside the file bytes for GPS atoms even if the tools don't list them."""
    needles = (b"\xa9xyz", b"com.apple.quicktime.location", b"GPSLatitude")
    keep = max(len(n) for n in needles)
    tail = b""
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            data = tail + chunk
            if any(n in data for n in needles):
                return True
            tail = data[-keep:]
    return False


async def leftovers(path: Path, video: bool):
    left = set()
    for g, t, _ in await exif_read(path):
        if t in EXEMPT:
            continue
        if FLAG_GROUPS.match(g) or RISKY_NAME.search(t):
            left.add(t)
    if video:
        left |= {k for _, k, _ in ffprobe_tags(await ffprobe_json(path))}
    if await asyncio.to_thread(raw_gps_scan, path):
        left.add("GPS data")
    return sorted(left)


# ---------------------------------------------------------------- cleaning: video
FFMPEG_CLEAN = [
    "-map", "0:V", "-map", "0:a?", "-sn", "-dn",
    "-map_metadata", "-1", "-map_metadata:s:v", "-1", "-map_metadata:s:a", "-1",
    "-map_chapters", "-1",
    "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
    "-movflags", "+faststart",
]
REENCODE = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-b:a", "160k",
]


async def ffmpeg_clean(src: Path, dst: Path, reencode: bool) -> bool:
    codec = REENCODE if reencode else ["-c", "copy"]
    code, _, err = await run("ffmpeg", "-y", "-v", "error", "-i", str(src), *FFMPEG_CLEAN, *codec, str(dst))
    if code != 0:
        log.warning("ffmpeg failed (reencode=%s): %s", reencode, err.decode(errors="ignore")[-300:])
    return code == 0 and dst.exists() and dst.stat().st_size > 0


async def clean_video(src: Path, dst: Path):
    """Fast stream-copy first; if anything is left, full re-encode. Returns (ok, leftover_tags)."""
    ok, left = False, []
    for reencode in (False, True):
        dst.unlink(missing_ok=True)
        ok = await ffmpeg_clean(src, dst, reencode)
        if not ok:
            continue
        left = await leftovers(dst, True)
        if not left:
            break
    return ok, left


# ---------------------------------------------------------------- cleaning: photo
def _pillow_strip(src: Path, dst: Path):
    from PIL import Image, ImageOps

    tmp = dst.with_name("pil_" + dst.name)
    with Image.open(src) as im:
        fmt = (im.format or "JPEG").upper()
        im = ImageOps.exif_transpose(im)  # bake rotation into pixels before dropping EXIF
        for k in list(im.info):
            if k != "transparency":
                del im.info[k]
        if fmt == "JPEG":
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(tmp, format="JPEG", quality=95, optimize=True)
        elif fmt == "PNG":
            im.save(tmp, format="PNG", optimize=True)
        elif fmt == "WEBP":
            im.save(tmp, format="WEBP", quality=95)
        elif fmt == "TIFF":
            im.save(tmp, format="TIFF", compression="tiff_lzw")
        else:
            raise ValueError(f"unsupported format {fmt}")
    os.replace(tmp, dst)


async def clean_photo(src: Path, dst: Path):
    """ExifTool first (keeps pixels untouched); Pillow re-save as a fallback. Returns (ok, leftover_tags)."""
    shutil.copyfile(src, dst)
    code, _, err = await run(
        "exiftool", "-all=", "-tagsfromfile", "@", "-Orientation",
        "-m", "-q", "-q", "-overwrite_original", str(dst),
    )
    ok = code == 0
    left = await leftovers(dst, False) if ok else []
    if ok and not left:
        return True, []
    log.info("exiftool result ok=%s left=%s -> trying Pillow", ok, left)
    try:
        await asyncio.to_thread(_pillow_strip, src, dst)
    except Exception as e:
        log.warning("pillow fallback failed: %s", e)
        return ok, left
    return True, await leftovers(dst, False)
