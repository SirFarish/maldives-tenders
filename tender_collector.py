"""
Maldives Daily Tender / Announcement Collector — multi-source
=============================================================
Scrapes several Maldives sources (the Government Gazette plus SOE / agency
sites), combines and de-duplicates them, and writes a single self-contained
HTML dashboard.

Sources live in sources.py (SOURCES registry). Add adapters there.

Usage:
    python tender_collector.py                     # today, all sources
    python tender_collector.py --days 4            # rolling window
    python tender_collector.py --date 2026-09-03
    python tender_collector.py --only Gazette      # a subset of sources
    python tender_collector.py --list-sources

Output (./output):
    index.html / latest.html   (served online / most recent run)
    tenders_YYYY-MM-DD.html     (dated copy)
"""

import argparse
import datetime as dt
import html
import re
import sys
import traceback
from pathlib import Path

import requests

import sources as src


# ---------------------------------------------------------------------------
# Collect + merge + de-duplicate
# ---------------------------------------------------------------------------
def _norm_words(title):
    return set(re.sub(r"[^a-z0-9 ]", " ", (title or "").lower()).split())


def _similar(a, b):
    wa, wb = _norm_words(a), _norm_words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= 0.45


def dedupe(records):
    """Merge the same tender advertised on more than one source. Two records are
    merged only when they share a reference key AND have similar titles, so a
    coincidental ref collision between unrelated offices is left alone."""
    by_ref = {}
    singles = []
    for r in records:
        (by_ref.setdefault(r["ref"], []) if r["ref"] else singles).append(r)

    merged = list(singles)
    for ref, group in by_ref.items():
        used = [False] * len(group)
        for i, r in enumerate(group):
            if used[i]:
                continue
            cluster = [r]
            used[i] = True
            for j in range(i + 1, len(group)):
                if not used[j] and _similar(r["title"], group[j]["title"]):
                    cluster.append(group[j])
                    used[j] = True
            merged.append(_merge_cluster(cluster))
    return merged


def _merge_cluster(cluster):
    """Combine records for the same tender into one, preferring the richest."""
    if len(cluster) == 1:
        cluster[0]["also_on"] = []
        return cluster[0]
    # Primary = the one with a deadline, else the first.
    primary = next((c for c in cluster if c["deadline"]), cluster[0])
    others = [c for c in cluster if c is not primary]
    primary["deadline"] = primary["deadline"] or next(
        (c["deadline"] for c in others if c["deadline"]), None)
    primary["is_tender"] = any(c["is_tender"] for c in cluster)
    primary["also_on"] = [{"source": c["source"], "url": c["url"]} for c in others]
    return primary


def collect(target_dates, only=None):
    """Run every (or the chosen) source adapter, isolating failures."""
    records, errors = [], []
    for name, fn in src.SOURCES.items():
        if only and name not in only:
            continue
        try:
            got = fn(target_dates)
            records.extend(got)
            print(f"  {name}: {len(got)}")
        except Exception as e:  # one bad source must not sink the whole run
            errors.append(f"{name}: {e}")
            print(f"  {name}: FAILED — {e}", file=sys.stderr)
            traceback.print_exc()
    merged = dedupe(records)
    merged.sort(key=lambda r: (not r["is_tender"], r["published"] or dt.date.min,
                               r["source"]), reverse=False)
    # tenders first; within that, newest published first
    merged.sort(key=lambda r: (not r["is_tender"],
                               -(r["published"] or dt.date.min).toordinal()))
    return merged, errors


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------
def build_html(rows, target_dates, generated_at, errors):
    today = dt.date.today()
    types = sorted({r["type_en"] for r in rows if r["type_en"]})
    src_names = sorted({r["source"] for r in rows})

    def esc(s):
        return html.escape(str(s or ""))

    cards = []
    for r in rows:
        dl = r["deadline"]
        if dl:
            days_left = (dl - today).days
            if days_left < 0:
                badge = '<span class="badge over">closed</span>'
            elif days_left == 0:
                badge = '<span class="badge soon">closes today</span>'
            elif days_left <= 3:
                badge = f'<span class="badge soon">{days_left}d left</span>'
            else:
                badge = f'<span class="badge ok">{days_left}d left</span>'
            dl_str = dl.strftime("%d %b %Y")
        else:
            badge, dl_str = "", "—"

        also = "".join(
            f'<a class="src-extra" href="{esc(a["url"])}" target="_blank" rel="noopener" title="also on {esc(a["source"])}">+{esc(a["source"])}</a>'
            for a in r.get("also_on", []))

        cards.append(f"""
    <tr data-type="{esc(r['type_en'])}" data-source="{esc(r['source'])}" data-tender="{'1' if r['is_tender'] else '0'}" data-search="{esc((r['title']+' '+r['org']+' '+r['source']).lower())}">
      <td class="src"><span class="srcpill s-{esc(r['source'].lower())}">{esc(r['source'])}</span>{also}</td>
      <td class="type"><span class="pill{' t' if r['is_tender'] else ''}">{esc(r['type_en'])}</span></td>
      <td class="title"><a href="{esc(r['url'])}" target="_blank" rel="noopener">{esc(r['title'])}</a></td>
      <td class="office">{esc(r['org'])}</td>
      <td class="date">{esc(r['published'].strftime('%d %b %Y') if r['published'] else '')}</td>
      <td class="date">{esc(dl_str)} {badge}</td>
    </tr>""")

    sd = sorted(target_dates)
    date_label = sd[0].strftime("%d %b %Y") if len(sd) == 1 \
        else f"{sd[0].strftime('%d %b')} – {sd[-1].strftime('%d %b %Y')}"

    n_tender = sum(1 for r in rows if r["is_tender"])
    type_btns = '<button class="flt active" data-dim="type" data-flt="">All types ({})</button>'.format(len(rows))
    type_btns += f'<button class="flt tender" data-dim="type" data-flt="__tender__">Tenders only ({n_tender})</button>'
    for t in types:
        n = sum(1 for r in rows if r["type_en"] == t)
        type_btns += f'<button class="flt" data-dim="type" data-flt="{esc(t)}">{esc(t)} ({n})</button>'

    src_btns = '<button class="flt active" data-dim="source" data-flt="">All sources ({})</button>'.format(len(rows))
    for s in src_names:
        n = sum(1 for r in rows if r["source"] == s)
        src_btns += f'<button class="flt" data-dim="source" data-flt="{esc(s)}">{esc(s)} ({n})</button>'

    err_note = ""
    if errors:
        err_note = ('<div class="errs">⚠ Some sources could not be reached this run: '
                    + esc("; ".join(errors)) + "</div>")

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
  header .sub {{ opacity:.85; font-size:13px; }}
  .wrap {{ max-width:1240px; margin:0 auto; padding:16px 24px 60px; }}
  .toolbar {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:14px 0 4px; }}
  .toolbar.src {{ margin-top:4px; }}
  .lbl {{ font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:#8a9098; margin-right:2px; }}
  .flt {{ border:1px solid #cdd2d8; background:#fff; color:#333; border-radius:20px;
    padding:5px 13px; font-size:13px; cursor:pointer; }}
  .flt.active {{ background:#0b3d2e; color:#fff; border-color:#0b3d2e; }}
  .flt.tender {{ border-color:#0b3d2e; color:#0b3d2e; font-weight:600; }}
  .flt.tender.active {{ background:#0b3d2e; color:#fff; }}
  #q {{ flex:1; min-width:200px; padding:9px 14px; border:1px solid #cdd2d8; border-radius:8px; font-size:14px; }}
  table {{ width:100%; border-collapse:collapse; background:#fff; border-radius:10px;
    overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,.08); margin-top:12px; }}
  th {{ text-align:left; font-size:12px; text-transform:uppercase; letter-spacing:.04em;
    color:#6b7280; padding:11px 14px; border-bottom:2px solid #eef0f2; }}
  td {{ padding:11px 14px; border-bottom:1px solid #f0f2f4; vertical-align:top; }}
  tr:last-child td {{ border-bottom:none; }}
  .title a {{ color:#0b5cad; text-decoration:none; font-weight:500; }}
  .title a:hover {{ text-decoration:underline; }}
  .office {{ color:#555; max-width:220px; }}
  .date {{ white-space:nowrap; color:#444; }}
  .pill {{ background:#eceff1; color:#455a64; border-radius:6px; padding:2px 8px; font-size:12px; white-space:nowrap; }}
  .pill.t {{ background:#eaf3ee; color:#0b3d2e; font-weight:600; }}
  .srcpill {{ border-radius:6px; padding:2px 8px; font-size:12px; font-weight:600; background:#e7edf5; color:#274b74; white-space:nowrap; }}
  .srcpill.s-stelco {{ background:#fdf0e4; color:#a05a1a; }}
  .src-extra {{ display:inline-block; margin-left:4px; font-size:11px; color:#8a5a1a; text-decoration:none; }}
  .badge {{ font-size:11px; padding:1px 7px; border-radius:10px; margin-left:4px; }}
  .badge.ok {{ background:#e8f0fe; color:#1a56b0; }}
  .badge.soon {{ background:#fdecec; color:#c0392b; }}
  .badge.over {{ background:#eceff1; color:#78909c; }}
  .errs {{ background:#fff6e5; color:#8a5a00; border:1px solid #ffe0a3; border-radius:8px;
    padding:8px 12px; font-size:12px; margin-top:12px; }}
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
    .pill {{ background:#2a2f36; color:#c2cad3; }}
    .pill.t {{ background:#173a2c; color:#8fe3bd; }}
    .srcpill {{ background:#1c2c40; color:#9dc0ee; }}
    .srcpill.s-stelco {{ background:#3a2a17; color:#e3b98f; }}
    .errs {{ background:#2c2410; color:#e3c583; border-color:#5a4a1a; }}
  }}
</style></head><body>
<header>
  <h1>Maldives Public Tenders &amp; Gazette Announcements</h1>
  <div class="sub">Published: {esc(date_label)} &nbsp;•&nbsp; {len(rows)} announcements &nbsp;•&nbsp; sources: {esc(', '.join(src_names))}</div>
</header>
<div class="wrap">
  {err_note}
  <div class="toolbar"><span class="lbl">Type</span>{type_btns}</div>
  <div class="toolbar src"><span class="lbl">Source</span>{src_btns}
    <input id="q" placeholder="Search title, organisation or source…"></div>
  <table>
    <thead><tr><th>Source</th><th>Type</th><th>Title</th><th>Organisation</th><th>Published</th><th>Deadline</th></tr></thead>
    <tbody id="rows">{''.join(cards)}</tbody>
  </table>
  <div class="empty" id="empty" style="display:none">No announcements match your filter.</div>
  <footer>Generated {esc(generated_at)} • Government Gazette + SOE / agency sites</footer>
</div>
<script>
  const q=document.getElementById('q'), rows=[...document.querySelectorAll('#rows tr')];
  let curType='', curSource='';
  function apply(){{
    const term=q.value.trim().toLowerCase(); let shown=0;
    rows.forEach(tr=>{{
      const okT=!curType||(curType==='__tender__'?tr.dataset.tender==='1':tr.dataset.type===curType);
      const okSrc=!curSource||tr.dataset.source===curSource;
      const okS=!term||tr.dataset.search.includes(term);
      const vis=okT&&okSrc&&okS; tr.style.display=vis?'':'none'; if(vis)shown++;
    }});
    document.getElementById('empty').style.display=shown?'none':'';
  }}
  document.querySelectorAll('.flt').forEach(b=>b.onclick=()=>{{
    const dim=b.dataset.dim;
    document.querySelectorAll('.flt[data-dim="'+dim+'"]').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    if(dim==='type') curType=b.dataset.flt; else curSource=b.dataset.flt;
    apply();
  }});
  q.oninput=apply;
</script>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--days", type=int, default=1, help="how many days back to include")
    ap.add_argument("--only", help="comma-separated source names to run (default: all)")
    ap.add_argument("--list-sources", action="store_true")
    args = ap.parse_args()

    if args.list_sources:
        print("Available sources:", ", ".join(src.SOURCES))
        return

    only = {s.strip() for s in args.only.split(",")} if args.only else None
    end = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    target_dates = {end - dt.timedelta(days=i) for i in range(args.days)}

    print(f"Collecting for {', '.join(d.isoformat() for d in sorted(target_dates))}"
          f" from {', '.join(only) if only else 'all sources'}:")
    rows, errors = collect(target_dates, only)
    print(f"Total after de-duplication: {len(rows)}")

    generated_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    page = build_html(rows, target_dates, generated_at, errors)

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
