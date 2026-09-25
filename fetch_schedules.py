"""Alo schedule fetcher.

Checks each power utility's website, pulls load shedding schedules (HTML tables,
PDF notices, plain text), and writes one clean file the app reads: data/schedule.json

Run:  python fetch_schedules.py
      python fetch_schedules.py --offline tests/fixtures   (parse saved pages, no internet)

No manual input: when a source is down or changes its layout, the last good data
for that source is kept and the problem is recorded under "sources" in the output.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

import ocr
from planned import fetch_planned
from tls_fix import _BUNDLES, bundle_with_intermediates
from parse import (Slot, find_date, mentions_load_shedding, slots_from_table,
                   slots_from_text)

try:
    import pdfplumber
except ImportError:  # PDFs are skipped (and reported) if pdfplumber isn't installed
    pdfplumber = None

HERE = Path(__file__).parent
OUT = HERE / "data" / "schedule.json"
DHAKA = timezone(timedelta(hours=6))
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AloScheduleBot/1.0; +public load shedding schedule reader)",
           "Accept-Language": "bn,en;q=0.8"}
MAX_NOTICES_PER_SOURCE = 4
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def today_dhaka():
    return datetime.now(DHAKA).date()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- fetching
class Fetcher:
    def __init__(self, offline_dir: Path | None = None):
        self.offline_dir = offline_dir
        self.dead_hosts = set()
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def get(self, url: str, insecure: bool = False) -> tuple[bytes, str]:
        """Return (body, content_type). Offline mode reads tests/fixtures/<sha1(url)>.*"""
        if self.offline_dir:
            h = hashlib.sha1(url.encode()).hexdigest()[:12]
            for p in self.offline_dir.glob(h + ".*"):
                ctype = "application/pdf" if p.suffix == ".pdf" else "text/html"
                return p.read_bytes(), ctype
            raise FileNotFoundError(f"no fixture for {url} ({h})")
        last = None
        host = urlparse(url).hostname or ""
        if host in self.dead_hosts:
            raise requests.exceptions.ConnectTimeout(f"{host} did not answer earlier in this run")
        for attempt in range(2):
            verify = False if insecure else _BUNDLES.get(host, True)
            try:
                r = self.session.get(url, timeout=(12, 40), verify=verify)
                r.raise_for_status()
                return r.content, r.headers.get("content-type", "")
            except requests.exceptions.SSLError as e:
                last = e
                if host in _BUNDLES:
                    break
                try:
                    bundle_with_intermediates(host)
                    continue  # retry at once with the completed chain
                except Exception as e2:
                    last = requests.exceptions.SSLError(f"{e} / chain repair failed: {e2}")
                    break
            except requests.exceptions.ConnectionError as e:
                last = e
                if "NameResolutionError" in str(e) or "Name or service not known" in str(e):
                    break  # domain doesn't exist: retrying won't help
                if isinstance(e, requests.exceptions.ConnectTimeout) and attempt == 1:
                    self.dead_hosts.add(host)  # don't wait on this host again this run
                time.sleep(2 * (attempt + 1))
            except requests.RequestException as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise last


# ---------------------------------------------------------------- extraction
def extract_html(body: bytes, utility: str, url: str, day_default: str):
    soup = BeautifulSoup(body, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    day = find_date(text) or day_default
    slots: list[Slot] = []
    for table in soup.find_all("table"):
        rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])] for tr in table.find_all("tr")]
        cap = table.find_previous(string=True)
        tday = find_date(" ".join(filter(None, [str(cap or "")] + [" ".join(r) for r in rows[:2]]))) or day
        slots += slots_from_table(rows, utility, tday, url)
    if not slots:
        main = soup.find("main") or soup.find(id="content") or soup.body or soup
        slots = slots_from_text(main.get_text("\n"), utility, day, url)
    links = []
    for a in soup.find_all("a", href=True):
        title = a.get_text(" ", strip=True)
        row = a.find_parent(["tr", "li"])
        if row is not None and (len(title) < 12 or not mentions_load_shedding(title)):
            title = (title + " " + row.get_text(" ", strip=True))[:300]  # new portals: link says "দেখুন", title is in the row
        links.append((title, urljoin(url, a["href"])))
    for img in soup.find_all("img", src=True):
        alt = (img.get("alt") or "") + " " + (img.get("title") or "")
        links.append((alt.strip(), urljoin(url, img["src"])))
    return slots, links, text


def extract_pdf(body: bytes, utility: str, url: str, day_default: str):
    if pdfplumber is None:
        raise RuntimeError("pdfplumber not installed")
    slots: list[Slot] = []
    all_text = []
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        for page in pdf.pages:
            all_text.append(page.extract_text() or "")
        day = find_date("\n".join(all_text)) or day_default
        for page in pdf.pages:
            for tbl in page.extract_tables() or []:
                slots += slots_from_table(tbl, utility, day, url)
    if not "".join(all_text).strip():  # scanned PDF: read it with OCR
        text = ocr.ocr_pdf(body)
        if not text.strip():
            raise RuntimeError("PDF has no text layer (scanned image) and OCR found nothing")
        log(f"       OCR read {len(text)} characters from {url}")
        return slots_from_text(text, utility, find_date(text) or day_default, url)
    if not slots:
        slots = slots_from_text("\n".join(all_text), utility, day, url)
    return slots


def is_pdf(url, ctype):
    return url.lower().split("?")[0].endswith(".pdf") or "pdf" in (ctype or "")


def is_image(url):
    return url.lower().split("?")[0].endswith(IMAGE_EXT)


def process_source(src: dict, f: Fetcher, day_default: str):
    """Returns (slots, notices). Raises on hard failure of the main page."""
    utility, insecure = src["utility"], src.get("insecure_ssl", False)
    body, ctype = f.get(src["url"], insecure)
    notices, slots = [], []
    if is_pdf(src["url"], ctype):
        return extract_pdf(body, utility, src["url"], day_default), notices

    page_slots, links, text = extract_html(body, utility, src["url"], day_default)
    if src["kind"] == "page":
        slots += page_slots
        # follow PDFs/images linked from the schedule page
        cand = [(t, u) for t, u in links if is_pdf(u, "") or (is_image(u) and mentions_load_shedding(t + u))]
        if len(cand) > 6:
            cand = [(t, u) for t, u in cand if mentions_load_shedding(t + u)]
    else:  # notice board: only links whose title mentions load shedding
        cand = [(t, u) for t, u in links if mentions_load_shedding(t)]

    seen = set()
    for title, link in cand:
        if link in seen or len(seen) >= MAX_NOTICES_PER_SOURCE:
            continue
        seen.add(link)
        try:
            if is_image(link):
                got = []
                try:
                    b, _ = f.get(link, insecure)
                    text = ocr.ocr_image(b, "." + link.lower().split("?")[0].rsplit(".", 1)[-1])
                    got = slots_from_text(text, utility, find_date(text) or day_default, link)
                except Exception as e:
                    log(f"       image OCR failed for {link}: {e}")
                slots += got
                notices.append({"utility": utility, "title": title or "Load shedding notice", "url": link, "type": "image", "slots": len(got)})
                continue
            b, ct = f.get(link, insecure)
            if is_pdf(link, ct):
                got = extract_pdf(b, utility, link, day_default)
                notices.append({"utility": utility, "title": title, "url": link, "type": "pdf", "slots": len(got)})
                slots += got
            else:
                got, sub_links, _ = extract_html(b, utility, link, day_default)
                slots += got
                pdfs = [u for _, u in sub_links if is_pdf(u, "") or (is_image(u) and "objectstorage" in u)][:3]
                for p in pdfs:  # notice detail page usually just wraps a PDF
                    try:
                        pb, pct = f.get(p, insecure)
                        if is_image(p):
                            t = ocr.ocr_image(pb, "." + p.lower().split("?")[0].rsplit(".", 1)[-1])
                            more = slots_from_text(t, utility, find_date(t) or day_default, p)
                        else:
                            more = extract_pdf(pb, utility, p, day_default)
                        slots += more
                        got += more
                    except Exception as e:  # keep going
                        notices.append({"utility": utility, "title": title, "url": p, "type": "pdf", "error": str(e)[:160]})
                notices.append({"utility": utility, "title": title, "url": link, "type": "page", "slots": len(got)})
        except Exception as e:
            notices.append({"utility": utility, "title": title, "url": link, "type": "unknown", "error": str(e)[:160]})
    return slots, notices


def fetch_news(feeds, f: Fetcher, limit=8):
    items = []
    for feed in feeds:
        try:
            body, _ = f.get(feed)
            root = ElementTree.fromstring(body)
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                if mentions_load_shedding(title):
                    items.append({"title": title, "link": it.findtext("link"), "published": it.findtext("pubDate")})
        except Exception as e:
            log(f"news feed failed: {e}")
    seen, out = set(), []
    for i in items:
        if i["title"] not in seen:
            seen.add(i["title"])
            out.append(i)
    return out[:limit]


# ---------------------------------------------------------------- merge + write
def merge(new_by_source: dict, ok: dict, old: dict, today: str):
    """Use fresh data where a source worked; keep last good data where it failed."""
    old_by_source = {}
    for s in old.get("slots", []):
        old_by_source.setdefault(s.get("source_id"), []).append(s)
    out, keys = [], set()
    for sid in set(new_by_source) | set(old_by_source):
        rows = [dict(s.to_dict(), source_id=sid) for s in new_by_source.get(sid, [])] if ok.get(sid) \
            else old_by_source.get(sid, [])
        for r in rows:
            if r["date"] < today:
                continue
            k = (r["utility"], r["area"].lower(), r["date"], r["start"], r["end"])
            if k not in keys:
                keys.add(k)
                out.append(r)
    out.sort(key=lambda r: (r["date"], r["utility"], r["area"], r["start"]))
    return out


def content_hash(doc):
    d = {k: v for k, v in doc.items() if k not in ("generated_at", "sources")}
    return hashlib.sha256(json.dumps(d, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", type=Path, help="parse saved fixtures instead of the internet")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--sources", type=Path, default=HERE / "sources.json")
    args = ap.parse_args(argv)

    cfg = json.loads(args.sources.read_text(encoding="utf-8"))
    f = Fetcher(args.offline)
    today = today_dhaka().isoformat()
    old = json.loads(args.out.read_text(encoding="utf-8")) if args.out.exists() else {}

    new_by_source, ok, status, notices = {}, {}, [], []
    for src in cfg["sources"]:
        t0 = time.time()
        try:
            slots, nts = process_source(src, f, today)
            new_by_source[src["id"]], ok[src["id"]] = slots, True
            notices += nts
            status.append({"id": src["id"], "utility": src["utility"], "ok": True, "slots_found": len(slots),
                           "notices_found": len(nts), "seconds": round(time.time() - t0, 1)})
            log(f"[ok]   {src['id']}: {len(slots)} slots, {len(nts)} notices")
        except Exception as e:
            ok[src["id"]] = False
            status.append({"id": src["id"], "utility": src["utility"], "ok": False, "error": str(e)[:200]})
            log(f"[fail] {src['id']}: {e}")

    doc = {
        "generated_at": datetime.now(DHAKA).isoformat(timespec="seconds"),
        "timezone": "Asia/Dhaka",
        "slots": merge(new_by_source, ok, old, today),
        "notices": notices,
        "news": fetch_news(cfg.get("news_feeds", []), f) if not args.offline else old.get("news", []),
        "planned": fetch_planned(cfg.get("planned_feeds", []), f, today, log) if not args.offline
        else [p for p in old.get("planned", []) if p.get("date", "") >= today],
        "sources": status,
    }
    changed = content_hash(doc) != content_hash(old) if old else True
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"{len(doc['slots'])} slots total · changed={changed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
