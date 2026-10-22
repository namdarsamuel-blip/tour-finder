#!/usr/bin/env python3
"""
Tour Finder refresh job (runs on GitHub Actions).

1. Monthly listeners for ~25k artists (kworb, from Spotify) -> daily snapshot
   in listener_history, so 7-day / 30-day growth can be computed.
2. Every Spotify daily + weekly chart (Global, U.S., ~70 countries), counting
   featured artists too. Tracks when an artist first appears on any chart.
3. Draw Score (0-100) per artist: audience size, growth, U.S. chart heat, reach.
4. Ticketmaster + SeatGeek U.S. shows for the highest-scoring artists, with a
   Heat score per show (draw + resale premium + Verified Fan + SeatGeek demand).

Environment variables (GitHub repository secrets / variables):
  SUPABASE_URL, SUPABASE_SERVICE_KEY           required
  TICKETMASTER_KEY, SEATGEEK_KEY               optional (tour data)
  ARTISTS_TO_CHECK   default 400  (tour lookups per run)
  SONGS_PER_CHART    default 200
  HISTORY_MIN        default 1000000 (min listeners to keep daily history)
"""
import datetime as dt
import math
import os
import sys

import pandas as pd
import requests

import chart_tour_checker as c

URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
TM_KEY = os.environ.get("TICKETMASTER_KEY", "").strip()
SG_KEY = os.environ.get("SEATGEEK_KEY", "").strip()
MAX_ARTISTS = int(os.environ.get("ARTISTS_TO_CHECK") or 400)
TOP = int(os.environ.get("SONGS_PER_CHART") or 200)
HISTORY_MIN = int(os.environ.get("HISTORY_MIN") or 1_000_000)
BEFORE_TRACKING = "2000-01-01T00:00:00+00:00"  # "already charting when tracking began"
SKIP = {c.norm(x) for x in c.SKIP_ARTISTS}


# ----------------------------------------------------------------- Supabase --
def headers(prefer=None):
    h = {"apikey": KEY, "Content-Type": "application/json"}
    if not KEY.startswith("sb_"):
        h["Authorization"] = f"Bearer {KEY}"
    if prefer:
        h["Prefer"] = prefer
    return h


def api(method, path, rows=None, params=None, prefer=None, ok404=False):
    r = requests.request(method, f"{URL}/rest/v1/{path}", params=params, json=rows,
                         headers=headers(prefer), timeout=180)
    if r.status_code >= 300 and not (ok404 and r.status_code == 404):
        sys.exit(f"Supabase {method} {path} failed ({r.status_code}): {r.text[:500]}")
    return r


def fetch_all(path, params, method="GET", body=None):
    out, size, off = [], 1000, 0
    while True:
        p = dict(params, limit=size, offset=off)
        r = api(method, path, body, params=p)
        rows = r.json() if r.text else []
        out.extend(rows)
        if len(rows) < size:
            return out
        off += size


def upsert(table, rows, batch=500, on_conflict=None):
    params = {"on_conflict": on_conflict} if on_conflict else None
    for i in range(0, len(rows), batch):
        api("POST", table, rows[i:i + batch], params=params,
            prefer="resolution=merge-duplicates,return=minimal")


def clean(v):
    if v is None:
        return None
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, str) and v == "":
        return None
    return v


# ------------------------------------------------------------------ scoring --
clip = lambda x: max(0.0, min(1.0, x))


def pos_score(p):
    return clip((201 - p) / 200) if p else 0.0


def growth_signal(listeners, pct30, pct7, daily):
    """Best available growth estimate as a monthly fraction (0.10 = +10%/month)."""
    if pct30 is not None:
        return pct30
    if pct7 is not None:
        return pct7 * 30 / 7
    if daily is not None and listeners:
        return daily * 30 / listeners
    return 0.0


def draw_score(a):
    L = a.get("listeners") or 0
    size = clip((math.log10(L) - 5.7) / (7.9 - 5.7)) if L > 0 else 0.0
    g = growth_signal(L, a.get("pct_30d"), a.get("pct_7d"), a.get("listeners_daily"))
    momentum = clip((g + 0.02) / 0.32)
    us = max(pos_score(a.get("us_daily_best")), pos_score(a.get("us_weekly_best")))
    gl = max(pos_score(a.get("global_daily_best")), pos_score(a.get("global_weekly_best")))
    heat = max(us, 0.6 * gl)
    reach = clip((a.get("n_charts") or 0) / 25)
    score = 100 * (0.40 * size + 0.20 * momentum + 0.25 * heat + 0.15 * reach)
    parts = {"size": round(size * 100), "growth": round(momentum * 100),
             "chart": round(heat * 100), "reach": round(reach * 100)}
    return round(score, 1), parts, g


def heat_score(show, draw):
    parts, total, wsum = {"draw": round(draw)}, 0.55 * draw / 100, 0.55
    face, low = show.get("Price Min"), show.get("_sg_low")
    if face and low and show.get("Source") == "Ticketmaster + SeatGeek":
        ratio = low / face
        r = clip((ratio - 1) / 1.5)
        parts["resale"] = round(r * 100)
        parts["resale_x"] = round(ratio, 2)
        total, wsum = total + 0.2 * r, wsum + 0.2
    if show.get("_sg_score") is not None:
        s = clip(float(show["_sg_score"]))
        parts["seatgeek"] = round(s * 100)
        total, wsum = total + 0.15 * s, wsum + 0.15
    vf = 1.0 if show.get("_verified_fan") else (0.5 if (show.get("_presale_count") or 0) >= 2 else 0.0)
    parts["presale"] = round(vf * 100)
    total, wsum = total + 0.1 * vf, wsum + 0.1
    heat = 100 * total / wsum
    if show.get("_status_code") == "offsale":
        heat, parts["sold_out"] = max(heat, 90), True
    return round(heat, 1), parts


# --------------------------------------------------------------------- main --
def run():
    if not URL or not KEY:
        sys.exit("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set.")
    now = dt.datetime.now(dt.timezone.utc)
    started = now.isoformat()
    today = now.date().isoformat()

    print("Monthly listeners...")
    listeners = c.fetch_listeners()
    print(f"  {len(listeners)} artists with listener counts")

    print("Spotify charts (daily + weekly)...")
    charts = c.list_charts(include_weekly=True)
    entries, index = c.fetch_all_charts(charts, TOP)
    if not entries and not listeners:
        sys.exit("No data downloaded from kworb.")

    # ---- aggregate chart presence per artist (main + featured) ----
    agg = {}

    def credit(name, aid, e, featured):
        if not name or c.norm(name) in SKIP:
            return
        k = aid or c.norm(name)
        a = agg.setdefault(k, {"name": name, "codes": set(), "entries": [], "new": 0,
                               "us_daily_best": None, "us_weekly_best": None,
                               "global_daily_best": None, "global_weekly_best": None})
        a["codes"].add(e["Chart Code"])
        field = {("us", "daily"): "us_daily_best", ("us", "weekly"): "us_weekly_best",
                 ("global", "daily"): "global_daily_best", ("global", "weekly"): "global_weekly_best"
                 }.get((e["Chart Code"], e["Period"]))
        if field and (a[field] is None or e["Pos"] < a[field]):
            a[field] = e["Pos"]
        if e["Change"] in ("NEW",) or (e["Days on Chart"] or 99) <= 7 or (e["Weeks on Chart"] or 99) <= 1:
            a["new"] += 1
        a["entries"].append({"chart": e["Chart"], "code": e["Chart Code"], "period": e["Period"],
                             "pos": e["Pos"], "title": e["Title"], "change": e["Change"],
                             "featured": featured})

    for e in entries:
        credit(e["Artist"], e["Artist ID"], e, False)
        for n, aid in e["_featured"]:
            credit(n, aid, e, True)

    def top_entries(es):
        pri = {"us": 0, "global": 1}
        es = sorted(es, key=lambda x: (pri.get(x["code"], 2), x["pos"], x["period"] != "weekly"))
        out, seen = [], set()
        for x in es:
            sig = (x["code"], x["period"], x["title"])
            if sig not in seen:
                seen.add(sig)
                out.append(x)
            if len(out) >= 12:
                break
        return out

    # ---- history: listener counts 7 / 30 days ago ----
    def ago(n):
        rows = fetch_all("rpc/listeners_ago", {}, method="POST", body={"n": n})
        return {r["key"]: r["listeners"] for r in rows}
    l7, l30 = ago(7), ago(30)
    existing_first = {r["key"]: r["first_charted"] for r in fetch_all(
        "artists", {"select": "key,first_charted", "first_charted": "not.is.null"})}
    first_run = not existing_first

    # ---- build artist rows ----
    lmap = {x["key"]: x for x in listeners}
    keys = set(lmap) | set(agg)
    rows = []
    for k in keys:
        L, ch = lmap.get(k), agg.get(k)
        name = (L or {}).get("name") or ch["name"]
        if c.norm(name) in SKIP:
            continue
        lis = (L or {}).get("listeners")
        a = {
            "key": k, "name": name,
            "spotify_url": f"https://open.spotify.com/artist/{k}" if len(k) == 22 else None,
            "listeners": lis, "listeners_daily": (L or {}).get("daily"),
            "listeners_peak": (L or {}).get("peak"), "listeners_rank": (L or {}).get("rank"),
            "growth_7d": (lis - l7[k]) if lis and k in l7 else None,
            "growth_30d": (lis - l30[k]) if lis and k in l30 else None,
            "pct_7d": round((lis - l7[k]) / l7[k], 4) if lis and l7.get(k) else None,
            "pct_30d": round((lis - l30[k]) / l30[k], 4) if lis and l30.get(k) else None,
            "in_charts": bool(ch), "n_charts": len(ch["codes"]) if ch else 0,
            "us_daily_best": ch and ch["us_daily_best"], "us_weekly_best": ch and ch["us_weekly_best"],
            "global_daily_best": ch and ch["global_daily_best"], "global_weekly_best": ch and ch["global_weekly_best"],
            "new_entries": ch["new"] if ch else 0,
            "chart_songs": top_entries(ch["entries"]) if ch else [],
            "top_song": (top_entries(ch["entries"])[0]["title"] if ch else None),
            "chart_seen_at": started if ch else None,
            "updated_at": started,
        }
        if ch:
            a["first_charted"] = existing_first.get(k) or (BEFORE_TRACKING if first_run else started)
        else:
            a["first_charted"] = existing_first.get(k)
        a["draw_score"], a["score_parts"], g = draw_score(a)
        a["growth_signal"] = round(g, 4)
        a["is_rising"] = bool(lis and 2_000_000 <= lis <= 9_000_000 and g >= 0.08)
        rows.append(a)
    print(f"Saving {len(rows)} artists...")
    upsert("artists", rows)
    api("PATCH", "artists", {"in_charts": False, "n_charts": 0, "chart_songs": [], "new_entries": 0,
                             "us_daily_best": None, "us_weekly_best": None,
                             "global_daily_best": None, "global_weekly_best": None},
        params={"in_charts": "eq.true", "chart_seen_at": f"lt.{started}"}, prefer="return=minimal")

    # ---- listener history snapshot ----
    hist = [{"key": x["key"], "day": today, "listeners": x["listeners"]}
            for x in rows if x["listeners"] and (x["listeners"] >= HISTORY_MIN or x["in_charts"])]
    print(f"Saving {len(hist)} listener snapshots...")
    upsert("listener_history", hist, batch=1000, on_conflict="key,day")
    api("POST", "rpc/prune_history", {}, prefer="return=minimal")

    # ---- chart entries (latest only) ----
    chart_rows = [{
        "id": f"{e['Chart Code']}_{e['Period']}_{e['Pos']}",
        "chart": e["Chart"], "chart_code": e["Chart Code"], "period": e["Period"],
        "chart_date": clean(e["Chart Date"]), "pos": e["Pos"], "change": clean(e["Change"]),
        "artist": e["Artist"], "artist_key": e["Artist ID"] or c.norm(e["Artist"]),
        "featured": clean(e["Featured"]), "title": e["Title"],
        "days": clean(e["Days on Chart"]), "weeks": clean(e["Weeks on Chart"]), "peak": clean(e["Peak"]),
        "streams": clean(e["Streams"]), "streams_change": clean(e["Streams Change"]),
        "total_streams": clean(e["Total Streams"]), "track_url": clean(e["Spotify Track Link"]),
        "updated_at": started,
    } for e in entries]
    print(f"Saving {len(chart_rows)} chart entries...")
    upsert("chart_entries", chart_rows)
    api("DELETE", "chart_entries", params={"updated_at": f"lt.{started}"})

    # ---- tours for the highest-scoring artists ----
    show_rows, checked = [], 0
    if TM_KEY or SG_KEY:
        pri = sorted((r for r in rows if r["in_charts"] or (r["listeners"] or 0) >= 1_000_000),
                     key=lambda r: (r["is_rising"], r["draw_score"]), reverse=True)
        # biggest draws first, but always include the rising 2M-9M artists
        top = sorted(pri, key=lambda r: r["draw_score"], reverse=True)[:MAX_ARTISTS]
        rising = [r for r in pri if r["is_rising"] and r not in top][:max(50, MAX_ARTISTS // 5)]
        pick = top + rising
        by_name = {}
        for r in pick:
            by_name.setdefault(r["name"], r)
        print(f"Checking U.S. tours for {len(by_name)} artists...")
        df = pd.DataFrame({"Artist": list(by_name)})
        shows, summary = c.check_tours(df, TM_KEY or None, len(df), SG_KEY or None)
        checked = len(summary)

        seen = set()
        heat_by_artist = {}
        for s in shows:
            sid = s.get("Show ID")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            art = by_name[s["Artist"]]
            heat, hparts = heat_score(s, art["draw_score"])
            if s["Sale Status"] != "Cancelled":
                heat_by_artist[art["key"]] = max(heat_by_artist.get(art["key"], 0), heat)
            show_rows.append({
                "id": sid, "artist": s["Artist"], "artist_key": art["key"],
                "artist_rank": None, "draw_score": art["draw_score"], "listeners": art["listeners"],
                "heat_score": heat, "heat_parts": hparts,
                "sale_status": s["Sale Status"], "date": clean(s["Date"]), "time": clean(s["Time"]),
                "event": clean(s["Event"]), "venue": clean(s["Venue"]), "city": clean(s["City"]),
                "state": clean(s["State"]), "public_onsale": clean(s["Public On-Sale"]),
                "presales_now": clean(s["Presales Open Now"]), "presales_upcoming": clean(s["Upcoming Presales"]),
                "price_min": clean(s["Price Min"]), "price_max": clean(s["Price Max"]),
                "resale_low": clean(s.get("_sg_low")), "resale_median": clean(s.get("_sg_median")),
                "resale_listings": clean(s.get("_sg_listings")),
                "verified_fan": bool(s.get("_verified_fan")),
                "ticket_url": clean(s["Ticket Link"]), "seatgeek_url": clean(s.get("SeatGeek Link")),
                "source": clean(s.get("Source")), "active": s["Sale Status"] != "Cancelled",
                "presale_windows": s.get("_presales") or [], "onsale_at": s.get("_onsale_at"),
                "status_code": clean(s.get("_status_code")), "last_seen": started,
            })

        tour_rows = []
        for name, t in summary.items():
            art = by_name[name]
            tour_rows.append({
                "key": art["key"], "tour_checked_at": started,
                "on_tour": t.get("On Tour (US)") == "YES", "us_shows": t.get("US Shows"),
                "presale_now": t.get("Presale Now"), "on_sale_now": t.get("On Sale Now"),
                "coming": t.get("Presale/On-sale Coming"), "next_show": clean(t.get("Next Show")),
                "next_show_date": clean(t.get("Next Show Date")), "next_show_url": clean(t.get("Next Show Link")),
                "states": clean(t.get("States")), "tm_url": clean(t.get("Ticketmaster Artist Page")),
                "axs_url": t.get("AXS Search"), "bandsintown_url": t.get("Bandsintown Search"),
                "songkick_url": t.get("Songkick Search"), "google_url": t.get("Google Tour Search"),
                "max_heat": heat_by_artist.get(art["key"]),
            })
        print(f"Saving tour info for {len(tour_rows)} artists, {len(show_rows)} shows...")
        upsert("artists", tour_rows)
        upsert("shows", show_rows)
        if show_rows:
            api("PATCH", "shows", {"active": False},
                params={"last_seen": f"lt.{started}", "active": "eq.true"}, prefer="return=minimal")
    else:
        print("No Ticketmaster/SeatGeek key - skipping tours.")

    api("POST", "runs", [{
        "ran_at": started, "charts": len(index), "artists_checked": checked,
        "shows": len(show_rows), "artists_tracked": len(rows),
        "presales_now": sum(r["sale_status"] == "PRESALE NOW" for r in show_rows),
        "status": "ok" if (TM_KEY or SG_KEY) else "no tour keys",
    }], prefer="return=minimal")
    print("Done.")


if __name__ == "__main__":
    run()
