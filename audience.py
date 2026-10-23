"""
Audience fit: does the area around a venue match the artist's fan base?

- Venue area: the metro area (or county) around each venue, from the U.S. Census Bureau
  (American Community Survey 5-year estimates, free, updated every year). Venues are
  matched to their metro once with the Census geocoder and cached in the venue_geo table.
- Artist audience: Ticketmaster / SeatGeek genre plus which countries' Spotify charts the
  artist is on. Latin and Asian-language acts get matched against the Hispanic/Latino or
  Asian share of the area, compared with the U.S. as a whole.

fit_score (0-100) = 50% cultural match (when the artist has one) + 30% market size + 20% young adults.
Without a cultural match: 60% market size + 40% young adults.
"""
import concurrent.futures as cf
import math
import os
import re
import time

import requests

CENSUS_KEY = os.environ.get("CENSUS_KEY", "")
ACS_YEARS = [2025, 2024, 2023, 2022]
YOUNG = [f"B01001_{i:03d}E" for i in list(range(7, 13)) + list(range(31, 37))]  # ages 18-34, men + women
VARS = ["B01003_001E", "B03003_001E", "B03003_003E", "B02001_001E", "B02001_005E", "B19013_001E"] + YOUNG
GEOCODER = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
NEW_VENUES_PER_RUN = int(os.environ.get("NEW_VENUES_PER_RUN", "600"))

LATIN_RE = re.compile(r"latin|reggaet|regional mex|mexican|banda|corrido|norte[nñ]o|bachata|salsa|cumbia|merengue|"
                      r"tejano|urbano|mariachi|ranchera|dembow|tropical", re.I)
ASIAN_RE = re.compile(r"k-?pop|j-?pop|c-?pop|mandopop|cantopop|bollywood|desi|punjabi|bhangra|filipino|\bopm\b|"
                      r"j-?rock|k-?rock|anime|asian", re.I)
SPANISH = {"mx", "co", "ar", "cl", "pe", "ec", "es", "do", "gt", "hn", "sv", "ni", "cr", "pa", "py", "uy", "bo", "ve"}
ASIA = {"kr", "jp", "tw", "hk", "ph", "id", "vn", "th", "sg", "my", "in"}
TAG_FIELD = {"latin": "area_hisp", "asian": "area_asian"}
TAG_WORDS = {"latin": "Hispanic/Latino", "asian": "Asian"}

clamp = lambda x: max(0.0, min(1.0, x))
UA = {"User-Agent": "tour-finder/1.0 (personal research)"}


def _get(url, params, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=60)
            if r.status_code == 200:
                return r
        except requests.RequestException:
            pass
        time.sleep(2 * (i + 1))
    return None


def acs(geo):
    """Census stats for every area of one kind: {geoid: stats}. geo: 'metro' | 'county' | 'us'."""
    forp = {"metro": "metropolitan statistical area/micropolitan statistical area:*",
            "county": "county:*", "us": "us:1"}[geo]
    for year in ACS_YEARS:
        params = {"get": "NAME," + ",".join(VARS), "for": forp}
        if CENSUS_KEY:
            params["key"] = CENSUS_KEY
        r = _get(f"https://api.census.gov/data/{year}/acs/acs5", params, tries=2)
        if not r:
            continue
        try:
            rows = r.json()
        except ValueError:
            continue
        head, out = rows[0], {}
        ix = {h: i for i, h in enumerate(head)}
        for row in rows[1:]:
            num = lambda v: float(row[ix[v]]) if row[ix[v]] not in (None, "", "-666666666") and float(row[ix[v]]) >= 0 else None
            pop = num("B01003_001E")
            if not pop:
                continue
            ht, h, rt, a = num("B03003_001E"), num("B03003_003E"), num("B02001_001E"), num("B02001_005E")
            young = sum(num(v) or 0 for v in YOUNG)
            gid = row[ix["state"]] + row[ix["county"]] if geo == "county" else row[-1]
            out[gid] = {
                "name": row[ix["NAME"]], "pop": int(pop), "year": year,
                "hisp": round(h / ht, 4) if ht and h is not None else None,
                "asian": round(a / rt, 4) if rt and a is not None else None,
                "young": round(young / pop, 4) if pop else None,
                "income": int(num("B19013_001E")) if num("B19013_001E") else None,
            }
        print(f"  Census {year} 5-year estimates: {len(out)} {geo} areas", flush=True)
        return out
    print(f"  Census data for {geo} areas unavailable this run", flush=True)
    return {}


def geocode(lat, lng):
    r = _get(GEOCODER, {"x": lng, "y": lat, "benchmark": "Public_AR_Current", "vintage": "Current_Current",
                        "layers": "all", "format": "json"})
    if not r:
        return None
    try:
        g = r.json()["result"]["geographies"]
    except (ValueError, KeyError):
        return None
    county = (g.get("Counties") or [{}])[0]
    metro = (g.get("Metropolitan Statistical Areas") or g.get("Micropolitan Statistical Areas") or [{}])[0]
    if not county.get("GEOID"):
        return None
    return {"county": county.get("GEOID"), "county_name": county.get("NAME"),
            "metro": metro.get("GEOID") or metro.get("CBSA"), "metro_name": metro.get("NAME")}


def venue_id(lat, lng):
    try:
        return f"{float(lat):.3f},{float(lng):.3f}"
    except (TypeError, ValueError):
        return None


def audience_tag(genres, countries):
    """('latin' | 'asian' | None, why)."""
    text = " ".join(g for g in genres if g)
    m = LATIN_RE.search(text)
    if m:
        return "latin", f"Genre: {next(g for g in genres if g and LATIN_RE.search(g))}"
    m = ASIAN_RE.search(text)
    if m:
        return "asian", f"Genre: {next(g for g in genres if g and ASIAN_RE.search(g))}"
    cs = set(countries) - {"us", "global"}
    if cs:
        sp, asn = len(cs & SPANISH), len(cs & ASIA)
        if sp >= 2 and sp >= 0.6 * len(cs):
            return "latin", f"On Spotify charts in {sp} Spanish-speaking countries"
        if asn >= 2 and asn >= 0.6 * len(cs):
            return "asian", f"On Spotify charts in {asn} Asian countries"
    return None, ""


def fit_score(area, us, tag):
    if not area or not area.get("pop"):
        return None, {}
    size = clamp((math.log10(area["pop"]) - 5) / 2) * 100            # 100K people = 0, 10M+ = 100
    young = clamp(((area.get("young") or 0) / (us.get("young") or 0.22) - 0.85) / 0.4) * 100
    parts = {"size": round(size), "young": round(young)}
    if tag:
        share = area.get({"latin": "hisp", "asian": "asian"}[tag]) or 0
        base = us.get({"latin": "hisp", "asian": "asian"}[tag]) or (0.19 if tag == "latin" else 0.06)
        culture = clamp((share / base - 0.5) / 2.0) * 100               # half the U.S. share = 0, 2.5x = 100
        parts["culture"] = round(culture)
        score = 0.5 * culture + 0.3 * size + 0.2 * young
    else:
        score = 0.6 * size + 0.4 * young
    return round(score, 1), parts


def enrich(show_rows, raw_by_id, artist_info, api, fetch_all):
    """Adds area_* and fit_* columns to show_rows in place. artist_info: {artist_key: (tag, why)}."""
    pts = {}
    for r in show_rows:
        raw = raw_by_id.get(r["id"]) or {}
        vid = venue_id(raw.get("_lat"), raw.get("_lng"))
        r["venue_geo"] = vid
        if vid:
            pts[vid] = (raw["_lat"], raw["_lng"])
    if not pts:
        return
    print(f"Audience fit: {len(pts)} venues...", flush=True)
    cache = {x["id"]: x for x in fetch_all("venue_geo", {"select": "*"})}
    todo = [v for v in pts if v not in cache][:NEW_VENUES_PER_RUN]
    if todo:
        print(f"  matching {len(todo)} new venues to their metro area (Census geocoder)...", flush=True)
        new = []
        with cf.ThreadPoolExecutor(6) as ex:
            for vid, g in zip(todo, ex.map(lambda v: geocode(*pts[v]), todo)):
                if g:
                    row = {"id": vid, **g}
                    cache[vid] = row
                    new.append(row)
        for i in range(0, len(new), 500):
            api("POST", "venue_geo", new[i:i + 500], params={"on_conflict": "id"},
                prefer="resolution=merge-duplicates,return=minimal")
    metros, counties, us = acs("metro"), acs("county"), acs("us")
    us = next(iter(us.values()), {})
    for r in show_rows:
        g = cache.get(r.get("venue_geo"))
        area = None
        if g:
            area = metros.get(g.get("metro")) or counties.get(g.get("county"))
        tag, why = artist_info.get(r["artist_key"], (None, ""))
        r["audience_tag"] = tag
        for k in ("area_name", "area_pop", "area_hisp", "area_asian", "area_young", "area_income", "fit_score", "fit_parts"):
            r[k] = None
        if not area:
            continue
        r["area_name"] = area["name"].replace(" Metro Area", " metro").replace(" Micro Area", " area")
        r["area_pop"], r["area_hisp"], r["area_asian"] = area["pop"], area["hisp"], area["asian"]
        r["area_young"], r["area_income"] = area["young"], area["income"]
        score, parts = fit_score(area, us, tag)
        parts.update({"tag": tag, "why": why, "us_hisp": us.get("hisp"), "us_asian": us.get("asian"),
                      "us_young": us.get("young"), "us_income": us.get("income"), "year": area.get("year")})
        r["fit_score"], r["fit_parts"] = score, parts
    for r in show_rows:
        r.pop("venue_geo", None)
