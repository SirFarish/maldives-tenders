"""
Source adapters for the Maldives tender collector.

Each adapter is a function `collect_<name>(target_dates) -> list[dict]` that
returns tender records published within `target_dates`. Every record is a dict
with this normalised shape:

    {
      "source":     "Gazette" | "STELCO" | ...   # where it came from
      "id":         stable unique id within the source
      "title":      str
      "org":        publishing organisation
      "type_slug":  machine type (may be "")
      "type_en":    human English type label
      "is_tender":  bool  (True = a real procurement opportunity)
      "published":  datetime.date | None
      "deadline":   datetime.date | None
      "url":        link to the notice / source page
      "ref":        normalised reference key for cross-source de-duplication
    }

To add a new SOE / agency / firm, write another `collect_*` function and add it
to the SOURCES registry at the bottom.
"""

import datetime as dt
import re
import time

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}
REQUEST_PAUSE = 0.5
MAX_PAGES = 200

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def _get(url, params=None):
    r = requests.get(url, params=params, headers=HEADERS, timeout=25)
    r.raise_for_status()
    r.encoding = "utf-8"
    return BeautifulSoup(r.text, "html.parser")


# Dhivehi (Thaana) month names -> month number (several spelling variants).
DHIVEHI_MONTHS = {
    1: ["ޖަނަވަރީ", "ޖެނުއަރީ", "ޖެނުއަރ"],
    2: ["ފެބްރުއަރީ", "ފެބުރުވަރީ", "ފެބުރުއަރީ"],
    3: ["މާރިޗު", "މާރޗް", "މާރިޗް", "މާޗް"],
    4: ["އޭޕްރީލް", "އޭޕްރިލް", "އެޕްރީލް"],
    5: ["މޭ", "މެއި"],
    6: ["ޖޫން"],
    7: ["ޖުލައި", "ޖުލާއި"],
    8: ["އޯގަސްޓު", "އޮގަސްޓު", "އޮގަސްޓް", "އޯގަސްޓް"],
    9: ["ސެޕްޓެންބަރު", "ސެޕްޓެމްބަރު", "ސެޕްޓެންބަރ"],
    10: ["އޮކްޓޫބަރު", "އޮކްޓޯބަރު", "އޮކްޓޯބަރ"],
    11: ["ނޮވެންބަރު", "ނޮވެމްބަރު", "ނޮވެންބަރ"],
    12: ["ޑިސެންބަރު", "ޑިސެމްބަރު", "ޑިސެންބަރ"],
}
_MONTH_LOOKUP = {name: num for num, names in DHIVEHI_MONTHS.items() for name in names}


def parse_dhivehi_date(text):
    """'03 ސެޕްޓެންބަރު 2026 00:00' -> date(2026, 9, 3) (or None)."""
    if not text:
        return None
    m = re.search(r"(\d{1,2})\s+(\S+)\s+(\d{4})", text.strip())
    if not m:
        return None
    day, month_word, year = int(m.group(1)), m.group(2), int(m.group(3))
    month = _MONTH_LOOKUP.get(month_word)
    if month is None:
        for name, num in _MONTH_LOOKUP.items():
            if name in month_word or month_word in name:
                month = num
                break
    if month is None:
        return None
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def parse_english_date(text):
    """Parse dates like 'Aug 30th 2026', '30 Aug 2026', '2026-08-30'."""
    if not text:
        return None
    t = _clean(text)
    t = re.sub(r"(\d{1,2})(st|nd|rd|th)", r"\1", t, flags=re.I)  # 30th -> 30
    for fmt in ("%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y",
                "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return dt.datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def extract_ref(title):
    """Pull a normalised 'YYYY/seq' reference so the same tender advertised on
    several sites can be de-duplicated. Returns e.g. '2026/128' or ''."""
    if not title:
        return ""
    # Prefer a 4-digit year (2018-2035) followed by a sequence number.
    m = re.search(r"(20[1-3]\d)\s*/\s*(\d{1,5})", title)
    if m:
        return f"{m.group(1)}/{int(m.group(2))}"
    return ""


# Gazette announcement type slug -> readable English label.
TYPE_EN = {
    "beelan": "Bid / Tender",
    "masakkaiy": "Works / Project",
    "gannan-beynunvaa": "Goods Wanted",
    "kuyyah-dhinun": "For Lease",
    "kuyyah-hifun": "Wanted to Rent",
    "neelan": "Auction",
    "vazeefaa": "Job Vacancy",
    "thamreenu": "Training",
    "mubaaraaiy": "Competition",
    "aanmu-mauloomaathu": "General Info",
    "dhennevun": "Notice",
}
TENDER_TYPES = {"beelan", "masakkaiy", "gannan-beynunvaa",
                "kuyyah-dhinun", "kuyyah-hifun", "neelan"}


# ---------------------------------------------------------------------------
# Source: Government Gazette (gazette.gov.mv/iulaan) — ALL announcement types
# ---------------------------------------------------------------------------
GAZETTE_BASE = "https://www.gazette.gov.mv/iulaan"


def _parse_gazette_row(item):
    title_a = item.select_one("a.iulaan-title")
    if not title_a:
        return None
    type_a = item.select_one("a.iulaan-type")
    office_a = item.select_one("a.iulaan-office")

    type_slug = ""
    if type_a and type_a.get("href"):
        m = re.search(r"type=([^&]+)", type_a["href"])
        type_slug = m.group(1) if m else ""

    published = deadline = None
    for line in item.get_text("\n", strip=True).split("\n"):
        d = parse_dhivehi_date(line)
        if d is None:
            continue
        if published is None:
            published = d
        elif deadline is None:
            deadline = d

    title = _clean(title_a.get_text())
    return {
        "source": "Gazette",
        "id": _clean(title_a.get("href", "")).rsplit("/", 1)[-1],
        "title": title,
        "org": _clean(office_a.get_text()) if office_a else "",
        "type_slug": type_slug,
        "type_en": TYPE_EN.get(type_slug, _clean(type_a.get_text()) if type_a else type_slug),
        "is_tender": type_slug in TENDER_TYPES,
        "published": published,
        "deadline": deadline,
        "url": title_a.get("href", ""),
        "ref": extract_ref(title),
    }


def collect_gazette(target_dates):
    """Walk the gazette (roughly newest-first) collecting rows published within
    target_dates. Robust to out-of-order 'bumped' entries: stops only once the
    newest item on a page is older than the window, confirmed over 2 pages."""
    oldest = min(target_dates)
    out = {}
    stale = 0
    for page in range(1, MAX_PAGES + 1):
        soup = _get(GAZETTE_BASE, {"page": page})
        rows = soup.select("div.bordered.items")
        if not rows:
            break
        page_max = None
        for item in rows:
            r = _parse_gazette_row(item)
            if not r or not r["published"]:
                continue
            page_max = r["published"] if page_max is None else max(page_max, r["published"])
            if r["published"] in target_dates:
                out[r["id"]] = r
        if page_max is not None and page_max < oldest:
            stale += 1
            if stale >= 2:
                break
        else:
            stale = 0
        time.sleep(REQUEST_PAUSE)
    return list(out.values())


# ---------------------------------------------------------------------------
# Source: STELCO (stelco.com.mv/tenders) — WordPress Download Manager table
# ---------------------------------------------------------------------------
STELCO_BASE = "https://stelco.com.mv/tenders"


def collect_stelco(target_dates):
    oldest = min(target_dates)
    out = []
    seen = set()
    stale = 0
    for page in range(1, 80):
        soup = _get(STELCO_BASE, {"cp_1": page} if page > 1 else None)
        table = soup.find("table")
        if not table:
            break
        rows = table.find_all("tr")[1:]  # skip header
        if not rows:
            break
        page_max = None
        for tr in rows:
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            raw_title = tds[0].get_text(" ", strip=True)
            # strip the trailing "N file(s) M downloads" that WPDM appends
            title = _clean(re.sub(r"\d+\s*file\(s\).*$", "", raw_title, flags=re.I))
            pub = parse_english_date(tds[2].get_text(strip=True))
            if pub is None:
                continue
            page_max = pub if page_max is None else max(page_max, pub)
            if pub in target_dates:
                link = tds[0].find("a")
                url = link.get("href") if link and link.get("href", "#") != "#" else STELCO_BASE
                key = (title, pub)
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "source": "STELCO",
                    "id": f"stelco:{extract_ref(title) or title[:40]}:{pub}",
                    "title": title,
                    "org": "STELCO",
                    "type_slug": "beelan",
                    "type_en": "Bid / Tender",
                    "is_tender": True,
                    "published": pub,
                    "deadline": None,
                    "url": url,
                    "ref": extract_ref(title),
                })
        if page_max is not None and page_max < oldest:
            stale += 1
            if stale >= 2:
                break
        else:
            stale = 0
        time.sleep(REQUEST_PAUSE)
    return out


# ---------------------------------------------------------------------------
# Source: MWSC (mwsc.com.mv/tenders)
# MWSC lists only *currently open* tenders (no publish date — just a
# registration deadline and a bid-submission date), so this adapter ignores the
# date window and returns every open tender, using the bid date as the deadline.
# ---------------------------------------------------------------------------
MWSC_BASE = "https://www.mwsc.com.mv/tenders"


def collect_mwsc(target_dates):
    today = dt.date.today()
    soup = _get(MWSC_BASE)
    table = soup.find("table")
    if not table:
        return []
    out = []
    for tr in table.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        title = _clean(tds[0].get_text(" ", strip=True))
        if not title:
            continue
        bid_raw = tds[2].get_text(" ", strip=True).split("|")[0]
        deadline = parse_english_date(bid_raw)
        if deadline and deadline < today:
            continue  # already closed
        doc = None
        for td in tds:
            a = td.find("a")
            if a and a.get("href"):
                doc = a["href"]
                break
        out.append({
            "source": "MWSC",
            "id": f"mwsc:{title[:60]}",
            "title": title,
            "org": "MWSC",
            "type_slug": "beelan",
            "type_en": "Bid / Tender",
            "is_tender": True,
            "published": None,           # site gives no publish date
            "deadline": deadline,
            "url": doc or MWSC_BASE,
            "ref": extract_ref(title),
        })
    return out


# ---------------------------------------------------------------------------
# Registry — add new adapters here as they are built
# ---------------------------------------------------------------------------
SOURCES = {
    "Gazette": collect_gazette,
    "STELCO": collect_stelco,
    "MWSC": collect_mwsc,
}
