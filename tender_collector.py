"""
Maldives Daily Tender / Gazette Announcement Collector
=======================================================
Scrapes the official Government Gazette (gazette.gov.mv) for every announcement
published *today*, combines them, and writes a single self-contained HTML
dashboard you can open in your browser.

Source: https://www.gazette.gov.mv/iulaan  (server-rendered, no API needed)

Usage:
    python tender_collector.py                # today's announcements
    python tender_collector.py --date 2026-09-03
    python tender_collector.py --days 2       # today + yesterday

Output: written to the ./output folder next to this script:
    tenders_YYYY-MM-DD.html   (dated copy)
    latest.html               (always the most recent run)
"""

import argparse
import datetime as dt
import html
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://www.gazette.gov.mv/iulaan"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}
MAX_PAGES = 200         # safety cap; one busy day can span ~20 pages
REQUEST_PAUSE = 0.5     # be polite to the server

# Dhivehi (Thaana) month names -> month number. Multiple spelling variants.
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

# Gazette announcement type slug -> readable English label.
TYPE_EN = {
    "beelan": "Bid / Tender",
    "masakkaiy": "Works / Project",
    "gannan-beynunvaa": "Goods Wanted",
    "kuyyah-dhinun": "For Lease",
    "neelan": "Auction",
    "vazeefaa": "Job Vacancy",
    "aanmu-mauloomaathu": "General Info",
    "dhennevun": "Notice",
}
# Types that represent an actual tender / procurement opportunity.
TENDER_TYPES = {"beelan", "masakkaiy", "gannan-beynunvaa", "kuyyah-dhinun", "neelan"}


def parse_dhivehi_date(text):
    """'03 ސެޕްޓެންބަރު 2026 00:00' -> datetime.date(2026, 9, 3) (or None)."""
    if not text:
        return None
    text = text.strip()
    m = re.search(r"(\d{1,2})\s+(\S+)\s+(\d{4})", text)
    if not m:
        return None
    day, month_word, year = int(m.group(1)), m.group(2), int(m.group(3))
    month = _MONTH_LOOKUP.get(month_word)
    if month is None:
        # tolerate a trailing/leading character difference
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


def _clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def parse_row(item):
    """Parse one .bordered.items block into a dict, or None."""
    title_a = item.select_one("a.iulaan-title")
    if not title_a:
        return None

    type_a = item.select_one("a.iulaan-type")
    office_a = item.select_one("a.iulaan-office")

    type_slug = ""
    if type_a and type_a.get("href"):
        m = re.search(r"type=([^&]+)", type_a["href"])
        type_slug = m.group(1) if m else ""

    # Published date and deadline live in the info line as
    # "ތާރީޚު: <date>" and "ސުންގަޑި: <deadline>".
    block_text = item.get_text("\n", strip=True)
    published = deadline = None
    pub_raw = dl_raw = ""
    for line in block_text.split("\n"):
        d = parse_dhivehi_date(line)
        if d is None:
            continue
        # First dated line = published, second = deadline (site order).
        if published is None:
            published, pub_raw = d, line
        elif deadline is None:
            deadline, dl_raw = d, line

    return {
        "id": _clean(title_a.get("href", "")).rsplit("/", 1)[-1],
        "title": _clean(title_a.get_text()),
        "type_slug": type_slug,
        "type_label": _clean(type_a.get_text()) if type_a else "",
        "type_en": TYPE_EN.get(type_slug, _clean(type_a.get_text()) if type_a else type_slug),
        "is_tender": type_slug in TENDER_TYPES,
        "office": _clean(office_a.get_text()) if office_a else "",
        "url": title_a.get("href", ""),
        "published": published,
        "deadline": deadline,
    }


def fetch_page(page):
    r = requests.get(BASE, params={"page": page}, headers=HEADERS, timeout=25)
    r.raise_for_status()
    r.encoding = "utf-8"
    return BeautifulSoup(r.text, "html.parser")


def collect(target_dates):
    """Walk pages (roughly newest-first) collecting rows whose published date is
    in target_dates.

    The gazette listing is mostly published-date descending but sprinkles a few
    out-of-order ("bumped"/edited) older entries into otherwise-current pages, so
    we must NOT stop on a single stray. We stop only once the *newest* item on a
    page is older than the oldest date we want, confirmed over 2 pages in a row.
    """
    oldest_wanted = min(target_dates)
    results = {}
    stale_pages = 0
    for page in range(1, MAX_PAGES + 1):
        soup = fetch_page(page)
        rows = soup.select("div.bordered.items")
        if not rows:
            break
        page_max = None
        for item in rows:
            row = parse_row(item)
            if not row or not row["published"]:
                continue
            p = row["published"]
            page_max = p if page_max is None else max(page_max, p)
            if p in target_dates:
                results[row["id"]] = row  # dedupe by id
        if page_max is not None and page_max < oldest_wanted:
            stale_pages += 1
            if stale_pages >= 2:  # confidently past the window
                break
        else:
            stale_pages = 0
        time.sleep(REQUEST_PAUSE)
    return sorted(results.values(), key=lambda r: (r["published"], r["type_label"]), reverse=True)


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------
def build_html(rows, target_dates, generated_at):
    today = dt.date.today()
    # Tenders first, then by published date (newest), then type.
    rows = sorted(rows, key=lambda r: (not r["is_tender"], r["type_en"]))
    types = sorted({r["type_en"] for r in rows if r["type_en"]})

    def esc(s):
        return html.escape(str(s or ""))

    cards = []
    for r in rows:
        dl = r["deadline"]
        if dl:
            days_left = (dl - today).days
            if days_left < 0:
                badge = f'<span class="badge over">closed</span>'
            elif days_left == 0:
                badge = f'<span class="badge soon">closes today</span>'
            elif days_left <= 3:
                badge = f'<span class="badge soon">{days_left}d left</span>'
            else:
                badge = f'<span class="badge ok">{days_left}d left</span>'
            dl_str = dl.strftime("%d %b %Y")
        else:
            badge, dl_str = "", "—"

        cards.append(f"""
    <tr data-type="{esc(r['type_en'])}" data-tender="{'1' if r['is_tender'] else '0'}" data-search="{esc((r['title']+' '+r['office']).lower())}">
      <td class="type"><span class="pill{' t' if r['is_tender'] else ''}">{esc(r['type_en'])}</span></td>
      <td class="title"><a href="{esc(r['url'])}" target="_blank" rel="noopener">{esc(r['title'])}</a></td>
      <td class="office">{esc(r['office'])}</td>
      <td class="date">{esc(r['published'].strftime('%d %b %Y') if r['published'] else '')}</td>
      <td class="date">{esc(dl_str)} {badge}</td>
    </tr>""")

    sd = sorted(target_dates)
    if len(sd) == 1:
        date_label = sd[0].strftime("%d %b %Y")
    else:
        date_label = f"{sd[0].strftime('%d %b')} – {sd[-1].strftime('%d %b %Y')}"
    n_tender = sum(1 for r in rows if r["is_tender"])
    filter_btns = '<button class="flt active" data-flt="">All ({})</button>'.format(len(rows))
    filter_btns += f'<button class="flt tender" data-flt="__tender__">Tenders only ({n_tender})</button>'
    for t in types:
        n = sum(1 for r in rows if r["type_en"] == t)
        filter_btns += f'<button class="flt" data-flt="{esc(t)}">{esc(t)} ({n})</button>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Maldives Tenders &amp; Gazette — {esc(date_label)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
    background:#f5f6f8; color:#1a1c20; }}
  header {{ background:#0b3d2e; color:#fff; padding:20px 24px; }}
  header h1 {{ margin:0 0 4px; font-size:20px; }}
  header .sub {{ opacity:.8; font-size:13px; }}
  .wrap {{ max-width:1200px; margin:0 auto; padding:20px 24px 60px; }}
  .toolbar {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:16px 0; }}
  .flt {{ border:1px solid #cdd2d8; background:#fff; color:#333; border-radius:20px;
    padding:6px 14px; font-size:13px; cursor:pointer; }}
  .flt.active {{ background:#0b3d2e; color:#fff; border-color:#0b3d2e; }}
  #q {{ flex:1; min-width:200px; padding:9px 14px; border:1px solid #cdd2d8;
    border-radius:8px; font-size:14px; }}
  table {{ width:100%; border-collapse:collapse; background:#fff; border-radius:10px;
    overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,.08); }}
  th {{ text-align:left; font-size:12px; text-transform:uppercase; letter-spacing:.04em;
    color:#6b7280; padding:12px 14px; border-bottom:2px solid #eef0f2; }}
  td {{ padding:12px 14px; border-bottom:1px solid #f0f2f4; vertical-align:top; }}
  tr:last-child td {{ border-bottom:none; }}
  .title a {{ color:#0b5cad; text-decoration:none; font-weight:500; }}
  .title a:hover {{ text-decoration:underline; }}
  .office {{ color:#555; max-width:220px; }}
  .date {{ white-space:nowrap; color:#444; }}
  .pill {{ background:#eceff1; color:#455a64; border-radius:6px; padding:2px 8px; font-size:12px; white-space:nowrap; }}
  .pill.t {{ background:#eaf3ee; color:#0b3d2e; font-weight:600; }}
  .flt.tender {{ border-color:#0b3d2e; color:#0b3d2e; font-weight:600; }}
  .flt.tender.active {{ background:#0b3d2e; color:#fff; }}
  .badge {{ font-size:11px; padding:1px 7px; border-radius:10px; margin-left:4px; }}
  .badge.ok {{ background:#e8f0fe; color:#1a56b0; }}
  .badge.soon {{ background:#fdecec; color:#c0392b; }}
  .badge.over {{ background:#eceff1; color:#78909c; }}
  .empty {{ padding:40px; text-align:center; color:#888; }}
  footer {{ text-align:center; color:#9aa0a6; font-size:12px; margin-top:24px; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background:#16181d; color:#e6e8eb; }}
    table {{ background:#1e2127; box-shadow:none; }}
    th {{ color:#9aa0a6; border-color:#2a2e35; }}
    td {{ border-color:#262a31; }}
    .flt {{ background:#1e2127; color:#cfd3d8; border-color:#333; }}
    #q {{ background:#1e2127; color:#e6e8eb; border-color:#333; }}
    .office {{ color:#aab; }} .date {{ color:#bcc; }}
    .pill {{ background:#173a2c; color:#8fe3bd; }}
  }}
</style></head><body>
<header>
  <h1>Maldives Public Tenders &amp; Gazette Announcements</h1>
  <div class="sub">Published: {esc(date_label)} &nbsp;•&nbsp; {len(rows)} announcements &nbsp;•&nbsp; source: gazette.gov.mv</div>
</header>
<div class="wrap">
  <div class="toolbar">{filter_btns}<input id="q" placeholder="Search title or organisation…"></div>
  <table>
    <thead><tr><th>Type</th><th>Title</th><th>Organisation</th><th>Published</th><th>Deadline</th></tr></thead>
    <tbody id="rows">{''.join(cards) if cards else ''}</tbody>
  </table>
  <div class="empty" id="empty" style="display:none">No announcements match your filter.</div>
  <footer>Generated {esc(generated_at)} • Data from the official Maldives Government Gazette</footer>
</div>
<script>
  const q=document.getElementById('q'), rows=[...document.querySelectorAll('#rows tr')];
  let curFilter='';
  function apply(){{
    const term=q.value.trim().toLowerCase(); let shown=0;
    rows.forEach(tr=>{{
      const okT=!curFilter||(curFilter==='__tender__'?tr.dataset.tender==='1':tr.dataset.type===curFilter);
      const okS=!term||tr.dataset.search.includes(term);
      const vis=okT&&okS; tr.style.display=vis?'':'none'; if(vis)shown++;
    }});
    document.getElementById('empty').style.display=shown?'none':'';
  }}
  document.querySelectorAll('.flt').forEach(b=>b.onclick=()=>{{
    document.querySelectorAll('.flt').forEach(x=>x.classList.remove('active'));
    b.classList.add('active'); curFilter=b.dataset.flt; apply();
  }});
  q.oninput=apply;
</script>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--days", type=int, default=1, help="how many days back to include")
    args = ap.parse_args()

    end = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    target_dates = {end - dt.timedelta(days=i) for i in range(args.days)}

    print(f"Collecting gazette announcements for: "
          f"{', '.join(d.isoformat() for d in sorted(target_dates))}")
    rows = collect(target_dates)
    print(f"Found {len(rows)} announcements.")

    generated_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    page = build_html(rows, target_dates, generated_at)

    out_dir = Path(__file__).resolve().parent / "output"
    out_dir.mkdir(exist_ok=True)
    for name in (f"tenders_{end.isoformat()}.html", "latest.html", "index.html"):
        (out_dir / name).write_text(page, encoding="utf-8")
        print(f"Wrote {out_dir / name}")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        print(f"Network error: {e}", file=sys.stderr)
        sys.exit(1)
