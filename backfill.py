#!/usr/bin/env python3
"""
One-time (re-runnable) history backfill from the Internet Archive.

Looks for archived copies of kworb's "Spotify top artists by monthly listeners"
pages from roughly 1 week to 4 months ago and saves them as listener history,
so 7 / 30 / 60 / 90-day growth works right away instead of after months of
daily tracking. Days that already have a real daily snapshot are left alone.

Run it from GitHub: Actions -> Backfill history -> Run workflow.
Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (same secrets as the refresh job).
"""
import datetime as dt
import time

import requests

import chart_tour_checker as c
import sync

CDX = "https://web.archive.org/cdx/search/cdx"
TARGETS = [7, 14, 21, 30, 45, 60, 75, 90, 120]   # days ago we'd like a snapshot for
TOLERANCE = 5                                    # accept a copy up to this many days off
MAX_PAGES = 15
UA = {"User-Agent": "tour-finder-backfill/1.0 (personal research)"}


def get(url, params=None, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=60)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 503):
                time.sleep(10 * (i + 1))
                continue
            return r
        except requests.RequestException:
            time.sleep(5 * (i + 1))
    return None


def snapshots(page_url, start, end):
    """Archived copies (timestamps) of one page between two dates."""
    r = get(CDX, {"url": page_url.replace("https://", ""), "output": "json", "fl": "timestamp,statuscode",
                  "filter": "statuscode:200", "from": start.strftime("%Y%m%d"), "to": end.strftime("%Y%m%d"),
                  "collapse": "timestamp:8"})
    if not r or r.status_code != 200:
        return []
    try:
        rows = r.json()
    except ValueError:
        return []
    return [row[0] for row in rows[1:]] if rows else []


def run():
    today = dt.date.today()
    start, end = today - dt.timedelta(days=max(TARGETS) + TOLERANCE), today - dt.timedelta(days=2)
    total_rows, found_days, missing = 0, set(), []
    for page in range(1, MAX_PAGES + 1):
        url = c.listeners_page_url(page)
        stamps = snapshots(url, start, end)
        print(f"page {page}: {len(stamps)} archived copies between {start} and {end}", flush=True)
        if not stamps and page > 1:
            missing.append(page)
            continue
        by_day = {dt.datetime.strptime(t[:8], "%Y%m%d").date(): t for t in stamps}
        picked = set()
        for n in TARGETS:
            want = today - dt.timedelta(days=n)
            best = min(by_day, key=lambda d: abs((d - want).days), default=None)
            if best and abs((best - want).days) <= TOLERANCE:
                picked.add(best)
        for day in sorted(picked):
            ts = by_day[day]
            r = get(f"https://web.archive.org/web/{ts}id_/{url}")
            time.sleep(1.5)
            if not r or r.status_code != 200:
                print(f"  {day}: download failed", flush=True)
                continue
            r.encoding = "utf-8"
            rows = c.parse_listeners_html(r.text)
            if not rows:
                print(f"  {day}: page had no listener table", flush=True)
                continue
            data = [{"key": x["key"], "day": day.isoformat(), "listeners": x["listeners"], "source": "archive"}
                    for x in rows if x["listeners"]]
            for i in range(0, len(data), 1000):
                sync.api("POST", "listener_history", data[i:i + 1000], params={"on_conflict": "key,day"},
                         prefer="resolution=ignore-duplicates,return=minimal")
            total_rows += len(data)
            found_days.add(day)
            print(f"  {day}: saved {len(data)} artists", flush=True)
        time.sleep(1)
    print()
    print(f"Done. Added up to {total_rows:,} archived listener counts (days that already had data were kept) across {len(found_days)} dates: "
          f"{', '.join(str(d) for d in sorted(found_days)) or 'none'}")
    if missing:
        print(f"No archived copies for pages {missing} (smaller artists). Their growth fills in from daily tracking.")
    print("The next refresh will use this history for 7/30/60/90-day growth.")


if __name__ == "__main__":
    if not sync.URL or not sync.KEY:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set.")
    run()
