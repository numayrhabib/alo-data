"""Planned power shutdowns announced in the news.

Newspapers publish notices almost daily, e.g. "আজ ৮ ঘণ্টা বিদ্যুৎ থাকবে না যেসব এলাকায়", naming the
places and the time window. This module finds those articles through news RSS feeds, reads each
article, and turns it into {date, start, end, places} entries for the app.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from parse import BN_MONTHS, find_date, find_ranges, normalize

TITLE_HINTS = ["বিদ্যুৎ থাকবে না", "থাকবে না বিদ্যুৎ", "বিদ্যুৎ বন্ধ থাকবে", "বিদ্যুৎ সরবরাহ বন্ধ", "বিদ্যুৎহীন থাকবে", "power outage", "power supply will remain"]
DHAKA_OFFSET = timedelta(hours=6)


def is_shutdown_title(title: str) -> bool:
    t = normalize(title).lower()
    return any(h.lower() in t for h in TITLE_HINTS)


def real_link(link: str) -> str:
    """Bing wraps article links (…apiclick.aspx?…&url=<article>); unwrap them."""
    q = parse_qs(urlparse(link).query)
    return q["url"][0] if "url" in q else link


def rss_items(xml_bytes: bytes):
    root = ElementTree.fromstring(xml_bytes)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = real_link((it.findtext("link") or "").strip())
        pub = it.findtext("pubDate")
        try:
            published = parsedate_to_datetime(pub) if pub else None
        except Exception:
            published = None
        yield title, link, published


def _date_without_year(text: str, year: int) -> str | None:
    t = normalize(text)
    months = "|".join(sorted(BN_MONTHS, key=len, reverse=True))
    m = re.search(rf"(\d{{1,2}})\s*(?:ই|লা|রা|শে|তারিখ)?\s*({months})", t)
    if m:
        try:
            return date(year, BN_MONTHS[m[2]], int(m[1])).isoformat()
        except ValueError:
            return None
    return None


def article_text(html: bytes) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside", "form"]):
        tag.decompose()
    body = soup.find("article") or soup.find(attrs={"itemprop": "articleBody"}) or soup.body or soup
    paras = [p.get_text(" ", strip=True) for p in body.find_all("p")]
    paras = [p for p in paras if len(p) > 25]
    return "\n".join(paras) if paras else body.get_text("\n", strip=True)


def parse_article(text: str, published: datetime | None, title: str = "") -> dict | None:
    """Return {date, start, end, ambiguous, places} or None if no time window is found."""
    pub_day = (published + DHAKA_OFFSET).date() if published else date.today()
    full = normalize(f"{title}\n{text}")
    ranges = list(find_ranges(full))
    if not ranges:
        return None
    start, end, amb, _ = ranges[0]
    day = find_date(full) or _date_without_year(full, pub_day.year)
    if not day:
        if re.search(r"আগামীকাল|কাল\s", full):
            day = (pub_day + timedelta(days=1)).isoformat()
        else:  # "আজ" or no day mentioned: the day it was published
            day = pub_day.isoformat()
    # Places: the first paragraph (usually names the district/upazila) plus the longest
    # comma-separated list, which is almost always the list of affected localities.
    paras = [p for p in text.split("\n") if p.strip()]
    first = paras[0] if paras else ""
    lists = sorted(paras, key=lambda p: p.count(",") + p.count("،"), reverse=True)
    places = first
    if lists and lists[0] != first and lists[0].count(",") >= 2:
        places = f"{first}\n{lists[0]}"
    return {"date": day, "start": start, "end": end, "ambiguous": amb, "places": places[:900]}


def fetch_planned(feeds, fetcher, today: str, log=print, limit_articles: int = 25):
    items, seen_links = [], set()
    for feed in feeds:
        try:
            body, _ = fetcher.get(feed)
            for title, link, published in rss_items(body):
                if link and link not in seen_links and is_shutdown_title(title):
                    seen_links.add(link)
                    items.append((title, link, published))
        except Exception as e:
            log(f"planned feed failed: {feed[:60]}… {e}")
    items.sort(key=lambda x: x[2].timestamp() if x[2] else 0, reverse=True)  # newest first
    out, keys = [], set()
    for title, link, published in items[:limit_articles]:
        try:
            html, _ = fetcher.get(link)
            info = parse_article(article_text(html), published, title)
        except Exception as e:
            log(f"planned article failed: {link[:80]} {e}")
            continue
        if not info or info["date"] < today:
            continue
        key = (info["date"], info["start"], info["end"], re.sub(r"\W+", "", info["places"])[:40])
        if key in keys:
            continue
        keys.add(key)
        out.append({"title": title, "url": link, "published": published.isoformat() if published else None, **info})
    out.sort(key=lambda x: (x["date"], x["start"]))
    log(f"[ok]   planned: {len(out)} upcoming shutdown notices from {len(items)} articles")
    return out
