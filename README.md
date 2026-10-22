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

## How it runs
- `sync.py` runs twice a day on GitHub Actions (`.github/workflows/refresh.yml`)
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

Optional variables (same page → Variables): `ARTISTS_TO_CHECK` (default 400),
`SONGS_PER_CHART` (default 200), `HISTORY_MIN` (default 1000000 — smallest artist
whose daily listener history is kept).

`chart_tour_checker.py` also works on its own and writes an Excel file:
`python chart_tour_checker.py --tm-key KEY --sg-key KEY`
