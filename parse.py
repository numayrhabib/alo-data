"""Parsing helpers: turn messy Bangla/English schedule text into clean time slots.

Everything here is pure (no network), so it can be unit-tested with saved pages.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from datetime import date

BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

KEYWORDS = ["লোডশেডিং", "লোড শেডিং", "লোডশেড", "লোড-শেডিং", "load shed", "loadshed", "load-shed"]

# Bangla time-of-day words -> how to convert a 12h hour to 24h
PERIODS = {
    "ভোর": "early", "সকাল": "am", "দুপুর": "noon", "বিকাল": "pm", "বিকেল": "pm",
    "সন্ধ্যা": "pm", "সন্ধা": "pm", "রাত": "night",
}
SEPARATORS = r"(?:-|–|—|~|থেকে|হতে|পর্যন্ত|to|till|until)"

_PERIOD_RE = "|".join(PERIODS)
_TIME = (
    rf"(?:(?P<{{p}}p>{_PERIOD_RE})\s*)?"          # optional Bangla period word before
    rf"(?P<{{p}}h>\d{{{{1,2}}}})"                  # hour
    rf"(?:\s*[:.ঃ]\s*(?P<{{p}}m>\d{{{{2}}}}))?"    # optional minutes
    rf"\s*(?P<{{p}}k>টা|ঘটিকা)?"                   # optional "o'clock" marker
    rf"\s*(?P<{{p}}ap>a\.?m\.?|p\.?m\.?)?"         # optional AM/PM
)
RANGE_RE = re.compile(
    r"(?<![\d:.])" + _TIME.format(p="a") + rf"\s*{SEPARATORS}\s*" + _TIME.format(p="b") + r"(?!\d)(?:\s*(?:পর্যন্ত))?",
    re.IGNORECASE,
)

EN_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
BN_MONTHS = {"জানুয়ারি": 1, "জানুয়ারী": 1, "ফেব্রুয়ারি": 2, "ফেব্রুয়ারী": 2, "মার্চ": 3, "এপ্রিল": 4,
             "মে": 5, "জুন": 6, "জুলাই": 7, "আগস্ট": 8, "আগষ্ট": 8, "সেপ্টেম্বর": 9, "অক্টোবর": 10,
             "নভেম্বর": 11, "ডিসেম্বর": 12}


@dataclass
class Slot:
    utility: str
    area: str
    date: str          # YYYY-MM-DD
    start: str         # HH:MM, 24h
    end: str
    ambiguous: bool    # True when AM/PM could not be worked out
    source_url: str

    def key(self):
        return (self.utility, self.area.lower(), self.date, self.start, self.end)

    def to_dict(self):
        return asdict(self)


def normalize(text: str) -> str:
    text = (text or "").translate(BN_DIGITS)
    text = text.replace("\u200c", "").replace("\u200d", "").replace("\xa0", " ")
    text = text.replace("বিকেল", "বিকাল")
    return re.sub(r"[ \t]+", " ", text).strip()


def _to24(h: int, period: str | None, ap: str | None) -> tuple[int, bool]:
    """Return (hour24, known). known=False when neither AM/PM nor a Bangla period was given."""
    if ap:
        pm = ap.lower().startswith("p")
        if h == 12:
            return (12 if pm else 0), True
        return (h + 12 if pm else h), True
    if period:
        kind = PERIODS[period]
        if kind in ("am", "early"):
            return (0 if h == 12 else h), True
        if kind == "noon":
            return (h if h >= 11 else h + 12), True
        if kind == "pm":
            return (h if h >= 12 else h + 12), True
        if kind == "night":
            if h == 12:
                return 0, True
            return (h + 12 if h >= 6 else h), True
    if h > 12:
        return h, True  # already 24h
    return h, False


def find_ranges(text: str):
    """Yield (start 'HH:MM', end 'HH:MM', ambiguous, match_span) for every time range in text."""
    t = normalize(text)
    for m in RANGE_RE.finditer(t):
        g = m.groupdict()
        # Reject things like dates "12-09-2026": at least one side needs a time marker.
        markers = [g["am"], g["bm"], g["ap"], g["bp"], g["ak"], g["bk"], g["aap"], g["bap"]]
        if not any(markers):
            continue
        ah, bh = int(g["ah"]), int(g["bh"])
        am, bm = int(g["am"] or 0), int(g["bm"] or 0)
        if ah > 24 or bh > 24 or am > 59 or bm > 59:
            continue
        # The end often inherits the start's period ("বিকাল ৩টা থেকে ৪টা") and vice versa.
        ap_a, ap_b = g["aap"] or None, g["bap"] or None
        p_a, p_b = g["ap"] or None, g["bp"] or None
        s24, s_known = _to24(ah, p_a, ap_a or (ap_b if not p_a else None))
        e24, e_known = _to24(bh, p_b or p_a, ap_b or (ap_a if not p_b else None))
        if s_known and not e_known and e24 < s24 and e24 + 12 <= 24:
            e24 += 12
            e_known = True
        if e_known and not s_known and s24 + 12 <= e24:
            s24 += 12  # e.g. "3 - 4 PM"
            s_known = True
        # "11 PM - 12 AM"-style midnight ends are written 24:00
        if e24 == 0 and s24 > 0:
            e24 = 24
        if s24 >= e24 and not (s24 > 18 and e24 < 6):
            continue  # nonsense range
        yield (f"{s24:02d}:{am:02d}", f"{e24:02d}:{bm:02d}", not (s_known and e_known), m.span())


def strip_ranges(text: str) -> str:
    t = normalize(text)
    t = RANGE_RE.sub(" ", t)
    t = re.sub(r"^\s*[\d০-৯]+\s*[.)।]\s*", "", t)          # leading serial "1." / "১।"
    t = re.sub(r"[|:;,।\-–—]+\s*$", "", t)
    t = re.sub(r"^\s*[|:;,।\-–—]+", "", t)
    return re.sub(r"\s+", " ", t).strip()


def find_date(text: str) -> str | None:
    t = normalize(text)
    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", t)
    if m:
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
        y = y + 2000 if y < 100 else y
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            pass
    months = "|".join(sorted(BN_MONTHS, key=len, reverse=True))
    m = re.search(rf"(\d{{1,2}})\s*(?:ই|লা|রা|শে|তারিখ)?\s*({months})[,\s]*(\d{{4}})", t)
    if m:
        try:
            return date(int(m[3]), BN_MONTHS[m[2]], int(m[1])).isoformat()
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3})[a-z]*[,\s]+(\d{4})", t)
    if m and m[2].lower() in EN_MONTHS:
        try:
            return date(int(m[3]), EN_MONTHS[m[2].lower()], int(m[1])).isoformat()
        except ValueError:
            pass
    return None


def split_areas(cell: str) -> list[str]:
    parts = re.split(r"[,\n;।/]|\s{2,}", normalize(cell))
    out = []
    for p in parts:
        p = strip_ranges(p)
        if p and not re.fullmatch(r"[\d\s.]+", p) and len(p) > 1:
            out.append(p)
    return out


def slots_from_table(rows: list[list[str]], utility: str, day: str, url: str) -> list[Slot]:
    """Handles both common layouts:
    A) header row = time slots, cells below = areas off during that slot
    B) each row = area (+feeder) with one or more time ranges in its cells
    """
    rows = [[normalize(c or "") for c in r] for r in rows if r and any((c or "").strip() for c in r)]
    if not rows:
        return []
    slots: list[Slot] = []

    for hi, header in enumerate(rows[:3]):
        col_ranges = {i: list(find_ranges(c)) for i, c in enumerate(header)}
        col_ranges = {i: r[0] for i, r in col_ranges.items() if r}
        if len(col_ranges) >= 2:  # layout A
            label_cols = [i for i in range(len(header)) if i not in col_ranges]
            for row in rows[hi + 1:]:
                label = " ".join(row[i] for i in label_cols if i < len(row)
                                 and row[i] and not re.fullmatch(r"[\d\s.]+", row[i])).strip()
                for i, (s, e, amb, _) in col_ranges.items():
                    if i < len(row):
                        for area in split_areas(row[i]):
                            name = f"{label} / {area}" if label and label != area else area
                            slots.append(Slot(utility, name, day, s, e, amb, url))
            return slots

    last_area = ""
    for row in rows:  # layout B
        ranges, names = [], []
        for c in row:
            rs = list(find_ranges(c))
            if rs:
                ranges += rs
                rest = strip_ranges(c)
                if rest and not re.fullmatch(r"[\d\s.]+", rest):
                    names.append(rest)
            elif c and not re.fullmatch(r"[\d\s.]+", c):
                names.append(c)
        area = " / ".join(n for n in names if n) or last_area
        if names:
            last_area = area
        for s, e, amb, _ in ranges:
            if area:
                slots.append(Slot(utility, area, day, s, e, amb, url))
    return slots


def slots_from_text(text: str, utility: str, day: str, url: str) -> list[Slot]:
    """Fallback for pages/PDFs without real tables: one schedule entry per line."""
    slots, heading = [], ""
    for line in normalize_lines(text):
        rs = list(find_ranges(line))
        if not rs:
            if 2 < len(line) < 80 and not any(k in line.lower() for k in KEYWORDS):
                heading = strip_ranges(line)
            continue
        area = strip_ranges(line) or heading
        if area:
            for s, e, amb, _ in rs:
                slots.append(Slot(utility, area, day, s, e, amb, url))
    return slots


def normalize_lines(text: str) -> list[str]:
    return [normalize(l) for l in (text or "").splitlines() if normalize(l)]


def mentions_load_shedding(text: str) -> bool:
    t = normalize(text).lower()
    return any(k in t for k in KEYWORDS)
