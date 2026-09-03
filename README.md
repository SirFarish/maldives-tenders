# Maldives Daily Tender Collector

Collects every public tender / announcement advertised in the Maldives each day
from the official Government Gazette (<https://www.gazette.gov.mv/iulaan>) and
combines them into a single, shareable HTML dashboard.

- **Tenders sorted to the top**, colour-coded deadlines, English type labels,
  "Tenders only" filter, live search, links to each full notice.
- Shows a **rolling 4-day window** so the page is never empty (the Maldives
  government does not post on Fri/Sat).

## Live site

Once published (see below), your partners access it at:

```
https://<your-github-username>.github.io/maldives-tenders/
```

The site **updates itself automatically every day at 4:00 PM Maldives time**
via GitHub Actions — no computer needs to be left on.

## Run it locally

```bash
pip install -r requirements.txt
python tender_collector.py                 # today
python tender_collector.py --days 7        # last 7 days
python tender_collector.py --date 2026-09-03
```

Output is written to `output/` (`index.html` = the page served online).

## Publish it as a shared website (one-time setup)

1. Create a **free GitHub account** at <https://github.com> if you don't have one.
2. Create a **new repository** named `maldives-tenders` (Public).
3. From this folder, push the code (commands provided during setup).
4. In the repo: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
5. Open the **Actions** tab → run **"Publish Maldives Tenders"** once.
   Your site goes live at the URL above. Share that link with your partners.

After that it runs on its own every day.
