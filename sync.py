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
  ARTISTS_TO_CHECK   default 600  (tour lookups per run, plus up to 250 rising artists)
  SONGS_PER_CHART    default 200
  HISTORY_MIN        default 1000000 (min listeners to keep daily history)
"""
import datetime as dt
import math
import os
import sys

import pandas as pd
import requests

import audience
import chart_tour_checker as c

URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
TM_KEY = os.environ.get("TICKETMASTER_KEY", "").strip()
SG_KEY = os.environ.get("SEATGEEK_KEY", "").strip()
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()      # optional phone alerts via the free ntfy app
SITE_URL = os.environ.get("SITE_URL", "").strip()
ALERT_MIN_HEAT = float(os.environ.get("ALERT_MIN_HEAT") or 55)
MAX_ARTISTS = int(os.environ.get("ARTISTS_TO_CHECK") or 500)
BREAKOUTS_TO_CHECK = int(os.environ.get("BREAKOUTS_TO_CHECK") or 350)
TOP = int(os.environ.get("SONGS_PER_CHART") or 200)
HISTORY_MIN = int(os.environ.get("HISTORY_MIN") or 0)   # keep daily history for every tracked artist
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


def breakout_score(a, seen_days):
    """How strongly a (preferably small) artist is breaking out right now. 0-100."""
    L = a.get("listeners") or 0
    if L <= 0:
        return 0.0, {}
    # growth as "times bigger per month": best measured window, else today's pace
    m = None
    if a.get("mult_30d"):
        m = a["mult_30d"]
    elif a.get("mult_7d"):
        m = min(4.0, a["mult_7d"] ** (30 / 7))
    elif a.get("daily_pct") is not None:
        m = min(3.0, (1 + max(a["daily_pct"], -0.5)) ** 30)
    growth = clip(math.log2(m) / math.log2(3)) if m and m > 1 else 0.0        # 3x a month = 100
    if a.get("mult_60d"):
        growth = max(growth, clip(math.log2(a["mult_60d"]) / math.log2(5)))      # 5x in 2 months = 100
    small = clip((math.log10(25e6) - math.log10(L)) / (math.log10(25e6) - math.log10(5e5)))  # 500K=100, 25M+=0
    fresh = 0.0
    if a.get("in_charts") and a.get("chart_age_days") is not None:
        fresh = 1.0 if a["chart_age_days"] <= 14 else 0.6 if a["chart_age_days"] <= 30 else 0.0
        # a new single from an established star isn't "fresh": full credit up to 5M, none past 15M
        fresh *= clip((math.log10(15e6) - math.log10(L)) / (math.log10(15e6) - math.log10(5e6)))
    if seen_days is not None and seen_days <= 45:
        fresh = max(fresh, 1 - seen_days / 60)                                   # just crossed 500K
    if a.get("at_peak"):
        fresh = max(fresh, 0.5)
    today = clip((a.get("daily_pct") or 0) / 0.03)                              # +3% in a day = 100
    score = 100 * (0.45 * growth + 0.20 * small + 0.20 * fresh + 0.15 * today)
    return round(score, 1), {"growth": round(growth * 100), "lowkey": round(small * 100),
                             "fresh": round(fresh * 100), "today": round(today * 100)}


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
    # no sale coming up and Ticketmaster has stopped selling: often a sell-out
    if show.get("Sale Status") == "Not on sale (may be sold out)":
        heat, parts["off_sale"] = max(heat, 80), True
    return round(heat, 1), parts


# ------------------------------------------------------------------- alerts --
def make_alerts(old, rows, today):
    """Selling-fast signals, from comparing this refresh with the last one."""
    out = []
    if not old:  # first run: nothing to compare against
        return out
    by_city = {}
    for o in old.values():
        if o.get("artist_key") and o.get("city") and o.get("date"):
            by_city.setdefault((o["artist_key"], c.norm(o["city"])), []).append(o)

    def base(r, kind, aid, title, detail):
        return {"id": aid, "kind": kind, "show_id": r["id"], "artist": r["artist"], "artist_key": r["artist_key"],
                "title": title, "detail": detail, "heat": r["heat_score"], "show_date": r["date"],
                "city": r["city"], "state": r["state"], "venue": r["venue"],
                "url": r["ticket_url"] or r["seatgeek_url"]}

    for r in rows:
        o = old.get(r["id"])
        where = f'{r["city"] or ""}{", " + r["state"] if r["state"] else ""}'
        if o is None:
            # another date in the same city, close to one we already knew about
            sib = [x for x in by_city.get((r["artist_key"], c.norm(r["city"] or "")), [])
                   if x["id"] != r["id"] and r["date"]]
            near = [x for x in sib if abs((dt.date.fromisoformat(x["date"]) - dt.date.fromisoformat(r["date"])).days) <= 7]
            if near:
                out.append(base(r, "show_added", f"added:{r['id']}", f"Another {where} date added",
                                f"{r['artist']} added {dt.date.fromisoformat(r['date']):%a %b} {dt.date.fromisoformat(r['date']).day} at {r['venue']} ({len(sib) + 1} {r['city']} dates now). "
                                "Extra dates usually mean the first show sold out or demand is very high."))
            continue
        if r["sale_status"] == "Not on sale (may be sold out)" and o.get("status_code") != "offsale":
            out.append(base(r, "sold_out", f"soldout:{r['id']}", "Likely sold out",
                            f"Ticketmaster stopped selling {r['artist']} at {r['venue']} after the on-sale."))
        lo0, lo1 = o.get("resale_low"), r.get("resale_low")
        if lo0 and lo1 and float(lo1) >= float(lo0) * 1.25 and float(lo1) - float(lo0) >= 20:
            out.append(base(r, "resale_jump", f"resale:{r['id']}:{today}", "Resale price jumped",
                            f"Cheapest resale went from ${float(lo0):.0f} to ${float(lo1):.0f} "
                            f"(+{(float(lo1) / float(lo0) - 1) * 100:.0f}%) since the last refresh."))
        n0, n1 = o.get("resale_listings"), r.get("resale_listings")
        if n0 and n1 is not None and n0 >= 20 and n1 <= n0 * 0.7:
            out.append(base(r, "listings_drop", f"listings:{r['id']}:{today}", "Resale tickets disappearing",
                            f"Resale listings dropped from {n0} to {n1} (−{(1 - n1 / n0) * 100:.0f}%) since the last refresh."))
    return out


def save_alerts(alerts):
    """Insert alerts, skipping ones already raised. Returns the ones that are new."""
    if not alerts:
        return []
    ids = [a["id"] for a in alerts]
    seen = set()
    for i in range(0, len(ids), 150):
        chunk = ids[i:i + 150]
        q = "(" + ",".join('"' + x.replace('"', '') + '"' for x in chunk) + ")"
        seen |= {r["id"] for r in api("GET", "alerts", params={"select": "id", "id": f"in.{q}"}).json()}
    new = [a for a in alerts if a["id"] not in seen]
    for i in range(0, len(new), 500):
        api("POST", "alerts", new[i:i + 500], params={"on_conflict": "id"},
            prefer="resolution=ignore-duplicates,return=minimal")
    return new


def push_digest(new_alerts, rows, now):
    """One phone notification per refresh via ntfy.sh (free app), if NTFY_TOPIC is set."""
    if not NTFY_TOPIC:
        return
    hot = sorted((a for a in new_alerts if (a.get("heat") or 0) >= ALERT_MIN_HEAT),
                 key=lambda a: -(a.get("heat") or 0))[:8]
    soon = []
    for r in rows:
        if not r["active"] or (r["heat_score"] or 0) < 70:
            continue
        for p in r.get("presale_windows") or []:
            try:
                t = dt.datetime.fromisoformat(p["start"])
            except (KeyError, ValueError):
                continue
            if now <= t <= now + dt.timedelta(hours=24):
                soon.append((t, r, p["name"]))
    soon.sort(key=lambda x: x[0])
    if not hot and not soon:
        return
    et = c._ET or dt.timezone.utc
    lines = []
    if hot:
        lines.append("SELLING FAST")
        lines += [f"• {a['artist']} – {a['title']} ({a['city']}, heat {a['heat']:.0f})" for a in hot]
    if soon:
        lines.append("PRESALES NEXT 24H")
        lines += [f"• {t.astimezone(et):%a %I:%M%p} {r['artist']} – {r['city']} ({n})" for t, r, n in soon[:10]]
    title = f"Tour Finder: {len(hot)} selling fast, {len(soon)} presales in 24h"
    headers = {"Title": title, "Tags": "ticket", "Priority": "high" if hot else "default"}
    if SITE_URL:
        headers["Click"] = SITE_URL
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data="\n".join(lines).encode("utf-8"),
                      headers=headers, timeout=20)
        print("Phone alert sent.")
    except requests.RequestException as e:
        print(f"Phone alert failed: {e}")


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
        a = agg.setdefault(k, {"name": name, "codes": set(), "entries": [], "new": 0, "age": None,
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
        # how long this artist has been on the charts: longest-running entry, in days
        d = e["Days on Chart"] if e["Days on Chart"] is not None else (
            e["Weeks on Chart"] * 7 if e["Weeks on Chart"] is not None else None)
        if d is not None and (a["age"] is None or d > a["age"]):
            a["age"] = d
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
    l7, l30, l60, l90 = ago(7), ago(30), ago(60), ago(90)
    existing_first = {r["key"]: r["first_charted"] for r in fetch_all(
        "artists", {"select": "key,first_charted", "first_charted": "not.is.null"})}
    first_run = not existing_first
    # when each artist first appeared in the 500K+ listeners list (and how big they were then)
    seen_before = {r["key"]: r for r in fetch_all(
        "artists", {"select": "key,first_seen_list,first_seen_listeners", "first_seen_list": "not.is.null"})}
    baseline = not seen_before
    today_d = dt.date.fromisoformat(today)
    mult = lambda now, then: round(now / then, 3) if now and then else None

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
            "daily_pct": round((L or {}).get("daily") / (lis - (L or {}).get("daily")), 5)
                         if lis and (L or {}).get("daily") is not None and lis - (L or {}).get("daily") > 0 else None,
            "listeners_peak": (L or {}).get("peak"), "listeners_rank": (L or {}).get("rank"),
            "growth_7d": (lis - l7[k]) if lis and k in l7 else None,
            "growth_30d": (lis - l30[k]) if lis and k in l30 else None,
            "pct_7d": round((lis - l7[k]) / l7[k], 4) if lis and l7.get(k) else None,
            "pct_30d": round((lis - l30[k]) / l30[k], 4) if lis and l30.get(k) else None,
            "growth_60d": (lis - l60[k]) if lis and k in l60 else None,
            "pct_60d": round((lis - l60[k]) / l60[k], 4) if lis and l60.get(k) else None,
            "mult_7d": mult(lis, l7.get(k)), "mult_30d": mult(lis, l30.get(k)),
            "mult_60d": mult(lis, l60.get(k)), "mult_90d": mult(lis, l90.get(k)),
            "l7_ago": l7.get(k), "l30_ago": l30.get(k), "l60_ago": l60.get(k), "l90_ago": l90.get(k),
            "at_peak": bool(lis and (L or {}).get("peak") and lis >= 0.995 * L["peak"]),
            "in_charts": bool(ch), "n_charts": len(ch["codes"]) if ch else 0,
            "us_daily_best": ch and ch["us_daily_best"], "us_weekly_best": ch and ch["us_weekly_best"],
            "global_daily_best": ch and ch["global_daily_best"], "global_weekly_best": ch and ch["global_weekly_best"],
            "new_entries": ch["new"] if ch else 0,
            "chart_age_days": ch["age"] if ch else None,
            "chart_countries": sorted(ch["codes"] - {"us", "global"}) if ch else [],
            "chart_songs": top_entries(ch["entries"]) if ch else [],
            "top_song": (top_entries(ch["entries"])[0]["title"] if ch else None),
            "chart_seen_at": started if ch else None,
            "updated_at": started,
        }
        if ch:
            a["first_charted"] = existing_first.get(k) or (BEFORE_TRACKING if first_run else started)
        else:
            a["first_charted"] = existing_first.get(k)
        if lis:
            prev = seen_before.get(k)
            if prev:
                a["first_seen_list"], a["first_seen_listeners"] = prev["first_seen_list"], prev["first_seen_listeners"]
            else:  # first refresh with this feature = baseline ("already on the list")
                a["first_seen_list"] = "2000-01-01" if baseline else today
                a["first_seen_listeners"] = lis
        else:
            a["first_seen_list"], a["first_seen_listeners"] = None, None
        seen_days = None
        if a["first_seen_list"] and a["first_seen_list"] > "2001":
            seen_days = (today_d - dt.date.fromisoformat(a["first_seen_list"][:10])).days
        a["breakout_score"], a["breakout_parts"] = breakout_score(a, seen_days)
        a.pop("mult_7d")
        a["draw_score"], a["score_parts"], g = draw_score(a)
        a["growth_signal"] = round(g, 4)
        a["is_rising"] = bool(lis and 2_000_000 <= lis <= 9_000_000 and g >= 0.08)
        rows.append(a)
    print(f"Saving {len(rows)} artists...")
    upsert("artists", rows)
    api("PATCH", "artists", {"in_charts": False, "n_charts": 0, "chart_songs": [], "new_entries": 0, "chart_age_days": None,
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
        pri = [r for r in rows if r["in_charts"] or (r["listeners"] or 0) >= 500_000]
        # biggest draws, plus the strongest low-key breakouts (and anything Rising)
        top = sorted(pri, key=lambda r: r["draw_score"], reverse=True)[:MAX_ARTISTS]
        top_keys = {r["key"] for r in top}
        breakouts = sorted((r for r in pri if r["key"] not in top_keys),
                           key=lambda r: r["breakout_score"], reverse=True)[:BREAKOUTS_TO_CHECK]
        pick = top + breakouts
        by_name = {}
        for r in pick:
            by_name.setdefault(r["name"], r)
        print(f"Checking U.S. tours for {len(by_name)} artists...")
        df = pd.DataFrame({"Artist": list(by_name)})
        shows, summary = c.check_tours(df, TM_KEY or None, len(df), SG_KEY or None)
        checked = len(summary)

        by_id = {}
        heat_by_artist = {}
        for s in shows:
            sid = s.get("Show ID")
            if not sid:
                continue
            art = by_name[s["Artist"]]
            if sid in by_id:  # same event found for another artist on the bill (festival, NYE show, support act)
                first = by_id[sid]
                if art["key"] != first["artist_key"] and art["key"] not in first["also_keys"]:
                    first["also_keys"].append(art["key"])
                    first["also_names"].append(s["Artist"])
                continue
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
                "lat": clean(s.get("_lat")), "lng": clean(s.get("_lng")),
                "genre": ", ".join(dict.fromkeys(g for g in (s.get("_genres") or []) if g)) or None,
                "also_keys": [], "also_names": [],
            })
            by_id[sid] = show_rows[-1]

        # ---- audience fit: venue-area demographics vs the artist's fan base ----
        artist_info = {}
        for name, art in by_name.items():
            genres = [g for s in shows if s["Artist"] == name for g in (s.get("_genres") or [])]
            artist_info[art["key"]] = audience.audience_tag(genres, art.get("chart_countries") or [])
        try:
            audience.enrich(show_rows, {s.get("Show ID"): s for s in shows}, artist_info, api, fetch_all)
        except Exception as ex:  # never let the extra stats break the refresh
            print(f"  audience fit skipped this run: {ex}", flush=True)
            for r in show_rows:
                for k in ("area_name", "area_pop", "area_hisp", "area_asian", "area_young", "area_income",
                          "fit_score", "fit_parts", "audience_tag"):
                    r.setdefault(k, None)
        for r in show_rows:  # a strong local fan base nudges Heat up, a weak one down (at most +/-5)
            if r.get("fit_score") is not None:
                r["heat_score"] = round(max(0, min(100, r["heat_score"] + (r["fit_score"] - 50) / 10)), 1)
                r["heat_parts"]["fit"] = r["fit_score"]

        # ---- selling-fast signals: compare with what we saw last refresh ----
        old = {r["id"]: r for r in fetch_all("shows", {
            "select": "id,artist_key,city,venue,date,status_code,sale_status,resale_low,resale_listings"})}
        alerts = make_alerts(old, show_rows, started[:10])
        hot_ids = {a["show_id"] for a in alerts if a["kind"] in ("show_added", "resale_jump", "listings_drop")}
        heat_by_artist = {}
        for r in show_rows:  # selling fast -> a little more heat
            if r["id"] in hot_ids:
                r["heat_score"] = min(100, round(r["heat_score"] + 8, 1))
                r["heat_parts"]["selling_fast"] = True
            if r["active"]:
                heat_by_artist[r["artist_key"]] = max(heat_by_artist.get(r["artist_key"], 0), r["heat_score"])
        for a in alerts:
            a["heat"] = next((r["heat_score"] for r in show_rows if r["id"] == a["show_id"]), a.get("heat"))

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
                "genre": ", ".join(dict.fromkeys(g for s in shows if s["Artist"] == name for g in (s.get("_genres") or []) if g)) or None,
                "audience_tag": artist_info.get(art["key"], (None, ""))[0],
                "audience_why": artist_info.get(art["key"], (None, ""))[1] or None,
            })
        print(f"Saving tour info for {len(tour_rows)} artists, {len(show_rows)} shows...")
        upsert("artists", tour_rows)
        upsert("shows", show_rows)
        if show_rows:
            api("PATCH", "shows", {"active": False},
                params={"last_seen": f"lt.{started}", "active": "eq.true"}, prefer="return=minimal")
        new_alerts = save_alerts(alerts)
        print(f"{len(new_alerts)} new selling-fast alerts")
        push_digest(new_alerts, show_rows, now)
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
