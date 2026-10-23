# Tour Finder

A private portal for spotting which artists will be a big draw and which
U.S. shows are likely to sell out, built from Spotify data and ticketing sites.

## What it tracks
- **Monthly listeners** for ~25,000 artists (everyone above 500K, via kworb.net),
  with a daily snapshot, so 7-day and 30-day growth build up over time.
- **Every Spotify daily and weekly chart** (Global, U.S., ~70 countries), counting
  featured artists, plus **chart newcomers** (first time on any chart).
- **U.S. shows** from Ticketmaster and SeatGeek: presale windows, Verified Fan,
  face value, resale prices and listing counts.

## Scores
- **Draw score (0–100, per artist):** 40% audience size (monthly listeners, log scale),
  20% growth rate, 25% best U.S. chart position (daily/weekly, or Global),
  15% reach (number of country charts).
- **Heat (0–100, per show):** artist draw score, plus resale price vs face value,
  SeatGeek demand score, and Verified Fan / multiple presales. "Off sale" pushes it to 90+.
- **Rising:** 2M–9M monthly listeners and growing ≥8% a month.
- **Audience fit (0–100, per show)** — `audience.py`: each venue is matched to its metro area
  (U.S. Census geocoder, cached in `venue_geo`) and compared using U.S. Census ACS 5-year data.
  Latin and Asian-language acts (from Ticketmaster/SeatGeek genre or the countries they chart in)
  are scored 50% on the area's Hispanic/Latino or Asian share vs the U.S., 30% market size,
  20% share aged 18–34. Other acts: 60% market size, 40% young adults. Nudges Heat by up to ±5.

## Vivid Seats / StubHub links
Neither site has a public API, so the **resale-links** Supabase Edge Function
(`supabase/functions/resale-links/index.ts`) runs a Brave Search for the exact show and only keeps an
event page whose address has the same date and artist. Links are cached in `resale_links`.
Setup: Supabase → Edge Functions → Deploy a new function → Via Editor → name `resale-links`,
paste the file, turn off JWT verification (the function checks `allowed_users` itself), and add the
secret `BRAVE_API_KEY` (free key at brave.com/search/api, 2,000 searches a month).

## How it runs
- `sync.py` runs three times a day on GitHub Actions (`.github/workflows/refresh.yml`)
  and writes to Supabase. To run it now: Actions → *Refresh tour data* → *Run workflow*.
- Tours are checked for the top 400 artists by draw score, plus up to 80 rising artists.
- The web app in `docs/` is served by GitHub Pages and reads Supabase directly.
  Only emails listed in the `allowed_users` table can sign in and see data.
- `supabase/setup.sql` creates or upgrades the database (safe to re-run).

## Secrets (Settings → Secrets and variables → Actions → Secrets)
| Name | Where it comes from |
|---|---|
| `SUPABASE_URL` | Supabase → Project Settings → API → Project URL |
| `SUPABASE_SERVICE_KEY` | Supabase → Project Settings → API Keys → secret key (or legacy `service_role`) |
| `TICKETMASTER_KEY` | developer.ticketmaster.com → My Apps → Consumer Key |
| `SEATGEEK_KEY` | seatgeek.com/account/develop → Client ID |
| `CENSUS_KEY` (optional) | api.census.gov/data/key_signup.html — only needed if Census requests get rate-limited |

Optional variables (same page → Variables): `ARTISTS_TO_CHECK` (default 500), `BREAKOUTS_TO_CHECK` (default 350),
`SONGS_PER_CHART` (default 200), `HISTORY_MIN` (default 0 — smallest artist
whose daily listener history is kept).

`chart_tour_checker.py` also works on its own and writes an Excel file:
`python chart_tour_checker.py --tm-key KEY --sg-key KEY`
