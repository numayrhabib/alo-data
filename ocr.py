"""Reads text from scanned notices (image-only PDFs and photos) with Tesseract, in Bangla + English.

Needs the free tools `tesseract` (with the `ben` language) and `pdftoppm`. The GitHub workflow
installs them. If they are missing, these functions return "" and the notice is just listed.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

MAX_PAGES = 6


def available() -> bool:
    return bool(shutil.which("tesseract") and shutil.which("pdftoppm"))


def _langs() -> str:
    try:
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return "eng"
    have = set(out.split())
    return "+".join(l for l in ("ben", "eng") if l in have) or "eng"


def _tesseract(image: Path) -> str:
    # psm 6 = treat the page as one block of text; keeps table rows on one line
    r = subprocess.run(["tesseract", str(image), "stdout", "-l", _langs(), "--psm", "6"],
                       capture_output=True, text=True, timeout=180)
    return r.stdout


def ocr_pdf(data: bytes) -> str:
    if not available():
        return ""
    with tempfile.TemporaryDirectory() as d:
        pdf = Path(d) / "in.pdf"
        pdf.write_bytes(data)
        subprocess.run(["pdftoppm", "-r", "250", "-gray", "-png", "-l", str(MAX_PAGES), str(pdf), str(Path(d) / "p")],
                       capture_output=True, timeout=180)
        return "\n".join(_tesseract(p) for p in sorted(Path(d).glob("p*.png")))


def ocr_image(data: bytes, suffix: str = ".png") -> str:
    if not shutil.which("tesseract"):
        return ""
    with tempfile.TemporaryDirectory() as d:
        img = Path(d) / ("in" + suffix)
        img.write_bytes(data)
        return _tesseract(img)
