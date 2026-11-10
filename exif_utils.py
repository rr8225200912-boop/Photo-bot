"""Robust photo metadata extraction: EXIF (IFD0 + Exif + GPS), XMP fallback, file hashes."""
from __future__ import annotations

import hashlib
import io
import math
import re
from typing import Any, Optional

from PIL import ExifTags, Image
from PIL.ExifTags import GPSTAGS, TAGS

Image.MAX_IMAGE_PIXELS = 250_000_000  # decompression-bomb guard

try:  # iPhone HEIC/HEIF support
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_OK = True
except Exception:  # pragma: no cover
    HEIF_OK = False

SKIP_TAGS = {"ExifOffset", "GPSInfo", "MakerNote", "InteroperabilityOffset", "PrintImageMatching"}

ORIENTATION = {
    1: "Normal", 2: "Mirrored", 3: "Rotated 180°", 4: "Mirrored vertically",
    5: "Mirrored + 90° CCW", 6: "Rotated 90° CW", 7: "Mirrored + 90° CW", 8: "Rotated 270° CW",
}
METERING = {0: "Unknown", 1: "Average", 2: "Center-weighted", 3: "Spot", 4: "Multi-spot",
            5: "Pattern/Matrix", 6: "Partial", 255: "Other"}
EXPOSURE_PROGRAM = {0: "Not defined", 1: "Manual", 2: "Normal", 3: "Aperture priority",
                    4: "Shutter priority", 5: "Creative", 6: "Action", 7: "Portrait", 8: "Landscape"}
EXPOSURE_MODE = {0: "Auto", 1: "Manual", 2: "Auto bracket"}
WHITE_BALANCE = {0: "Auto", 1: "Manual"}
COLOR_SPACE = {1: "sRGB", 2: "Adobe RGB", 65535: "Uncalibrated"}
SCENE_TYPE = {0: "Standard", 1: "Landscape", 2: "Portrait", 3: "Night"}
EDITOR_RE = re.compile(
    r"photoshop|lightroom|gimp|snapseed|picsart|canva|facetune|vsco|pixelmator|"
    r"affinity|paint\.net|capture one|luminar|photoscape|photoleap|lensa|remini",
    re.I,
)
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


# ---------------------------------------------------------------- helpers
def num(v: Any) -> Optional[float]:
    """Safely convert IFDRational / Fraction / (n, d) / str to float."""
    try:
        if v is None:
            return None
        if isinstance(v, (tuple, list)) and len(v) == 2 and all(isinstance(x, (int, float)) for x in v):
            return None if v[1] == 0 else v[0] / v[1]
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except Exception:
        return None


def decode_text(v: Any) -> str:
    """Decode bytes / UserComment-style values into clean text."""
    if isinstance(v, bytes):
        for prefix, enc in ((b"ASCII\x00\x00\x00", "ascii"), (b"UNICODE\x00", "utf-16"),
                            (b"JIS\x00\x00\x00\x00\x00", "shift_jis")):
            if v.startswith(prefix):
                v = v[8:]
                try:
                    return v.decode(enc, "ignore").strip("\x00 ").strip()
                except Exception:
                    break
        txt = v.decode("utf-8", "ignore").strip("\x00 ").strip()
        return txt if txt and txt.isprintable() else f"<{len(v)} bytes>"
    return str(v).strip("\x00 ").strip()


def jsonable(v: Any) -> Any:
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(v, str):
        return v.strip("\x00")
    if isinstance(v, bytes):
        return decode_text(v)
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (tuple, list)):
        return [jsonable(x) for x in v]
    f = num(v)
    return f if f is not None else str(v)


def dms_to_deg(v: Any, ref: Any) -> Optional[float]:
    try:
        if isinstance(v, (tuple, list)) and len(v) >= 1 and not (
            len(v) == 2 and all(isinstance(x, (int, float)) for x in v)
        ):
            parts = [num(x) for x in v]
            if any(p is None for p in parts):
                return None
            parts += [0.0] * (3 - len(parts))
            deg = parts[0] + parts[1] / 60.0 + parts[2] / 3600.0
        else:
            deg = num(v)
        if deg is None:
            return None
        r = ref.decode(errors="ignore") if isinstance(ref, bytes) else ref
        if isinstance(r, str) and r.strip("\x00 ").upper()[:1] in ("S", "W"):
            deg = -abs(deg)
        return deg
    except Exception:
        return None


def to_dms(deg: float, pos: str, neg: str) -> str:
    ref = pos if deg >= 0 else neg
    d = abs(deg)
    dd = int(d)
    mm = int((d - dd) * 60)
    ss = (d - dd - mm / 60) * 3600
    return f"{dd}°{mm}'{ss:.2f}\"{ref}"


def compass(deg: float) -> str:
    return COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def fmt_exposure(t: Optional[float]) -> Optional[str]:
    if not t or t <= 0:
        return None
    return f"{t:g} s" if t >= 1 else f"1/{round(1 / t)} s"


def decode_flash(v: Any) -> Optional[str]:
    try:
        v = int(v)
    except Exception:
        return None
    s = "ফ্ল্যাশ জ্বলেছে" if v & 1 else "ফ্ল্যাশ জ্বলেনি"
    mode = (v >> 3) & 3
    if mode == 1:
        s += " (compulsory)"
    elif mode == 2:
        s += " (suppressed)"
    elif mode == 3:
        s += " (auto)"
    if v & 0x20:
        s += ", no flash function"
    if v & 0x40:
        s += ", red-eye reduction"
    return s


def first(d: dict, *names: str) -> Any:
    for n in names:
        v = d.get(n)
        if v not in (None, "", b""):
            return v
    return None


def lookup(table: dict, v: Any) -> Optional[str]:
    try:
        return table.get(int(v), f"code {int(v)}")
    except Exception:
        return None


# ---------------------------------------------------------------- readers
def _get_ifd(exif, ifd) -> dict:
    try:
        return dict(exif.get_ifd(ifd))
    except Exception:
        return {}


def read_exif(img: Image.Image):
    """Return (ifd0, exif_ifd, gps_ifd) dicts keyed by readable tag names."""
    try:
        exif = img.getexif()
    except Exception:
        return {}, {}, {}
    ifd0 = {}
    for k, v in exif.items():
        name = TAGS.get(k, f"Tag_{k}")
        if name not in SKIP_TAGS:
            ifd0[name] = v
    exif_ifd = {TAGS.get(k, f"Tag_{k}"): v for k, v in _get_ifd(exif, ExifTags.IFD.Exif).items()
                if TAGS.get(k) not in SKIP_TAGS}
    gps_ifd = {GPSTAGS.get(k, f"GPS_{k}"): v for k, v in _get_ifd(exif, ExifTags.IFD.GPSInfo).items()}
    return ifd0, exif_ifd, gps_ifd


def read_xmp(img: Image.Image, raw: bytes) -> str:
    xmp = img.info.get("xmp") or img.info.get("XML:com.adobe.xmp")
    if isinstance(xmp, bytes):
        xmp = xmp.decode("utf-8", "ignore")
    if not xmp:
        head = raw[:4_000_000]
        s = head.find(b"<x:xmpmeta")
        if s != -1:
            e = head.find(b"</x:xmpmeta>", s)
            if e != -1:
                xmp = head[s:e + 12].decode("utf-8", "ignore")
    return xmp or ""


def xmp_attr(xmp: str, name: str) -> Optional[str]:
    if not xmp:
        return None
    m = re.search(rf'(?:[\w-]+:)?{name}\s*=\s*"([^"]*)"', xmp, re.I)
    if m:
        return m.group(1)
    m = re.search(rf"<(?:[\w-]+:)?{name}>([^<]*)</", xmp, re.I)
    return m.group(1) if m else None


def parse_xmp_coord(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        pass
    m = re.match(r"^(\d+(?:\.\d+)?),(\d+(?:\.\d+)?)([NSEW])$", s)  # DDD,MM.mmmN
    if m:
        d = float(m[1]) + float(m[2]) / 60
        return -d if m[3] in "SW" else d
    m = re.match(r"^(\d+),(\d+),(\d+(?:\.\d+)?)([NSEW])$", s)  # DDD,MM,SSN
    if m:
        d = float(m[1]) + float(m[2]) / 60 + float(m[3]) / 3600
        return -d if m[4] in "SW" else d
    return None


def parse_gps(gps: dict, xmp: str) -> Optional[dict]:
    lat = lon = None
    source = None
    if "GPSLatitude" in gps and "GPSLongitude" in gps:
        lat = dms_to_deg(gps["GPSLatitude"], gps.get("GPSLatitudeRef"))
        lon = dms_to_deg(gps["GPSLongitude"], gps.get("GPSLongitudeRef"))
        if lat is not None and lon is not None:
            source = "EXIF"
    if (lat is None or lon is None) and xmp:  # drones / some editors keep GPS only in XMP
        lat = parse_xmp_coord(xmp_attr(xmp, "GpsLatitude") or xmp_attr(xmp, "GPSLatitude"))
        lon = parse_xmp_coord(xmp_attr(xmp, "GpsLongitude") or xmp_attr(xmp, "GPSLongitude"))
        if lat is not None and lon is not None:
            source = "XMP"
    if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
        return None

    out: dict = {"lat": lat, "lon": lon, "source": source, "null_island": (lat == 0 and lon == 0)}

    alt = num(gps.get("GPSAltitude"))
    if alt is None:
        alt = num(xmp_attr(xmp, "AbsoluteAltitude"))
    else:
        ref = gps.get("GPSAltitudeRef")
        if isinstance(ref, bytes):
            ref = ref[0] if ref else 0
        if ref == 1:
            alt = -abs(alt)
    if alt is not None:
        out["alt"] = alt

    d = num(gps.get("GPSImgDirection"))
    if d is not None:
        ref = gps.get("GPSImgDirectionRef")
        ref = ref.decode(errors="ignore") if isinstance(ref, bytes) else ref
        out["direction"] = d
        out["direction_ref"] = "True north" if str(ref).strip("\x00 ").upper().startswith("T") else "Magnetic north"

    sp = num(gps.get("GPSSpeed"))
    if sp is not None:
        ref = gps.get("GPSSpeedRef")
        ref = (ref.decode(errors="ignore") if isinstance(ref, bytes) else ref) or "K"
        factor = {"K": 1.0, "M": 1.609344, "N": 1.852}.get(str(ref).strip("\x00 ").upper()[:1], 1.0)
        out["speed_kmh"] = sp * factor

    ds, ts = gps.get("GPSDateStamp"), gps.get("GPSTimeStamp")
    if ds and ts:
        try:
            h, m, s = (num(x) for x in ts)
            ds = ds.decode() if isinstance(ds, bytes) else str(ds)
            out["utc"] = f"{ds.strip(chr(0)).replace(':', '-')} {int(h):02d}:{int(m):02d}:{int(s):02d} UTC"
        except Exception:
            pass

    for key, label in (("GPSMapDatum", "datum"), ("GPSProcessingMethod", "method"), ("GPSSatellites", "satellites")):
        if gps.get(key):
            out[label] = decode_text(gps[key])
    for key, label in (("GPSDOP", "dop"), ("GPSHPositioningError", "h_error")):
        v = num(gps.get(key))
        if v is not None:
            out[label] = v

    out["dms"] = f"{to_dms(lat, 'N', 'S')} {to_dms(lon, 'E', 'W')}"
    return out


# ---------------------------------------------------------------- main API
def analyze(raw: bytes, filename: str = "image") -> dict:
    """Analyze image bytes. Returns {'sections', 'gps', 'warnings', 'raw'}; raises on unreadable images."""
    img = Image.open(io.BytesIO(raw))
    ifd0, exif_ifd, gps_ifd = read_exif(img)
    xmp = read_xmp(img, raw)
    merged = {**ifd0, **exif_ifd}
    gps = parse_gps(gps_ifd, xmp)
    warnings: list[str] = []

    w, h = img.size
    orient = lookup(ORIENTATION, merged.get("Orientation")) if "Orientation" in merged else None

    # ---- file
    file_rows = [
        ("নাম", filename),
        ("ফরম্যাট", img.format or "Unknown"),
        ("রেজোলিউশন", f"{w} × {h}  ({w * h / 1e6:.1f} MP)"),
        ("সাইজ", f"{len(raw) / 1024:.1f} KB"),
        ("MD5", hashlib.md5(raw).hexdigest()),
        ("SHA-256", hashlib.sha256(raw).hexdigest()),
    ]
    if orient:
        file_rows.append(("Orientation", orient))
    if first(merged, "ColorSpace") is not None:
        file_rows.append(("Color space", lookup(COLOR_SPACE, merged["ColorSpace"])))

    # ---- camera
    make = decode_text(merged["Make"]) if merged.get("Make") else ""
    model = decode_text(merged["Model"]) if merged.get("Model") else ""
    cam_name = model if (make and model.lower().startswith(make.lower())) else f"{make} {model}".strip()
    software = decode_text(merged["Software"]) if merged.get("Software") else ""
    cam_rows = [
        ("ক্যামেরা/ফোন", cam_name),
        ("লেন্স", decode_text(first(merged, "LensModel") or "")),
        ("লেন্স তৈরিকারী", decode_text(first(merged, "LensMake") or "")),
        ("সফটওয়্যার", software),
        ("Body serial", decode_text(first(merged, "BodySerialNumber") or "")),
        ("মালিক", decode_text(first(merged, "CameraOwnerName", "Artist") or "")),
        ("কপিরাইট", decode_text(first(merged, "Copyright") or "")),
        ("বর্ণনা", decode_text(first(merged, "ImageDescription") or "")),
        ("কমেন্ট", decode_text(first(merged, "UserComment") or "")),
    ]
    if software and EDITOR_RE.search(software):
        warnings.append(f"সফটওয়্যার ট্যাগে এডিটিং টুলের নাম আছে ({software}), ছবিটি এডিট করা হয়ে থাকতে পারে।")

    # ---- time
    def with_sub(base: Optional[str], sub: Any) -> Optional[str]:
        if not base:
            return None
        base = decode_text(base)
        return f"{base}.{decode_text(sub)}" if sub not in (None, "") else base

    taken = with_sub(first(merged, "DateTimeOriginal"), merged.get("SubsecTimeOriginal"))
    digitized = with_sub(first(merged, "DateTimeDigitized"), merged.get("SubsecTimeDigitized"))
    modified = with_sub(first(merged, "DateTime"), merged.get("SubsecTime"))
    time_rows = [
        ("তোলা হয়েছে", taken),
        ("Digitized", digitized if digitized != taken else None),
        ("সর্বশেষ মডিফাই", modified if modified != taken else None),
        ("টাইমজোন অফসেট", decode_text(first(merged, "OffsetTimeOriginal", "OffsetTime") or "")),
        ("GPS সময় (UTC)", gps.get("utc") if gps else None),
    ]

    # ---- exposure settings
    iso = first(merged, "ISOSpeedRatings", "PhotographicSensitivity")
    if isinstance(iso, (tuple, list)):
        iso = iso[0] if iso else None
    fnum, focal, focal35 = num(merged.get("FNumber")), num(merged.get("FocalLength")), num(merged.get("FocalLengthIn35mmFilm"))
    bias, zoom = num(merged.get("ExposureBiasValue")), num(merged.get("DigitalZoomRatio"))
    set_rows = [
        ("শাটার স্পিড", fmt_exposure(num(merged.get("ExposureTime")))),
        ("অ্যাপারচার", f"f/{fnum:g}" if fnum else None),
        ("ISO", iso),
        ("ফোকাল লেংথ", f"{focal:g} mm" if focal else None),
        ("35mm সমতুল্য", f"{focal35:g} mm" if focal35 else None),
        ("Exposure bias", f"{bias:+g} EV" if bias is not None else None),
        ("Exposure program", lookup(EXPOSURE_PROGRAM, merged["ExposureProgram"]) if "ExposureProgram" in merged else None),
        ("Exposure mode", lookup(EXPOSURE_MODE, merged["ExposureMode"]) if "ExposureMode" in merged else None),
        ("Metering", lookup(METERING, merged["MeteringMode"]) if "MeteringMode" in merged else None),
        ("ফ্ল্যাশ", decode_flash(merged["Flash"]) if "Flash" in merged else None),
        ("White balance", lookup(WHITE_BALANCE, merged["WhiteBalance"]) if "WhiteBalance" in merged else None),
        ("Digital zoom", f"{zoom:g}x" if zoom and zoom != 1 else None),
        ("Scene type", lookup(SCENE_TYPE, merged["SceneCaptureType"]) if "SceneCaptureType" in merged else None),
    ]

    def clean(rows):
        return [(k, str(v)) for k, v in rows if v not in (None, "", "None")]

    sections = [("📁 ফাইল", clean(file_rows)), ("📷 ডিভাইস", clean(cam_rows)),
                ("🕒 সময়", clean(time_rows)), ("⚙️ ক্যামেরা সেটিংস", clean(set_rows))]
    sections = [(t, r) for t, r in sections if r]

    if not (ifd0 or exif_ifd or gps_ifd):
        warnings.append("ছবিতে EXIF ডেটা নেই (সম্ভবত কম্প্রেসড, স্ক্রিনশট বা সোশ্যাল মিডিয়া/মেসেঞ্জার থেকে আসা)।")
    elif not gps:
        if gps_ifd:
            warnings.append(f"GPS ব্লক আছে কিন্তু কোঅর্ডিনেট পড়া যায়নি। পাওয়া ট্যাগ: {', '.join(map(str, gps_ifd))}")
        else:
            warnings.append(
                "ছবিতে GPS ডেটা নেই। কারণ হতে পারে: (১) তোলার সময় Location tags বন্ধ ছিল, "
                "(২) Android গ্যালারি/ফটো পিকার দিয়ে পাঠালে লোকেশন মুছে দেয়, ফাইল ম্যানেজার "
                "থেকে DCIM/Camera ফোল্ডারের মূল ফাইল পাঠাও, (৩) Google Photos থেকে শেয়ার করলে লোকেশন বাদ যায়।"
            )
    if gps and gps["null_island"]:
        warnings.append("GPS মান 0,0 (খালি/ভুয়া ডেটা), তাই এটাকে সঠিক লোকেশন ধরা যাবে না।")

    raw_dump = {
        "file": {"name": filename, "format": img.format, "width": w, "height": h, "bytes": len(raw)},
        "ifd0": jsonable(ifd0), "exif": jsonable(exif_ifd), "gps_raw": jsonable(gps_ifd),
        "gps_parsed": gps, "xmp": xmp[:30000] if xmp else None,
    }
    return {"sections": sections, "gps": gps, "warnings": warnings, "raw": raw_dump}
