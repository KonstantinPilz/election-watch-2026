# 2026 midterms: prediction-market watch

Live page: https://konstantinpilz.github.io/election-watch-2026/

Kalshi and Polymarket odds for the 2026 U.S. Senate and House races, plus the seat
distribution implied by the race-level prices.

## What the page shows

1. **Chamber control**: P(win chamber) from a seat model built on race-level odds, next
   to the Kalshi and Polymarket control markets and their seat-count markets.
2. **Seat distribution**: P(exact seat count) from the model (bars) vs. the Kalshi and
   Polymarket seat-count markets (dots or bucket bars). Viewpoint toggles between
   Democrats and Republicans.
3. **Competitive races**: every Senate race and House district whose leading side traded
   below 90% at any hour in the past 7 days on either platform, with both prices, the gap,
   and a 7-day sparkline.

## How it works

- `fetch.py` (stdlib Python) discovers races on both platforms, pulls current prices,
  keeps a rolling 7-day hourly history and writes `site/data.json` + `site/state.json`.
  The first run backfills a week of history (~1,200 price series, ~90 s). Later runs
  reuse the local `site/state.json` and only append (~15–30 s).
- `scripts/deploy.sh` runs from cron on the Oracle VM every 6 minutes (log:
  `deploy.log`): it runs `fetch.py`, then force-pushes `site/` as a single orphan commit
  to the `gh-pages` branch, which GitHub Pages serves. Six minutes matches the
  ~10-builds-per-hour limit for branch-built Pages. It stops itself after 2026-11-10;
  remove the crontab line (`# election-watch-deploy`) afterwards.
- `ci/github-actions-update.yml` is an unused alternative that runs everything on GitHub
  Actions, so the site keeps updating when the VM is off. To switch, run
  `gh auth refresh -s workflow` (the current token can't push workflow files), move the
  file to `.github/workflows/`, set Pages to "GitHub Actions" and remove the cron line.
- In the browser, Polymarket prices refresh every 30 s straight from the Polymarket
  CLOB API. Kalshi blocks browser requests (403 on any `Origin` header), so Kalshi
  prices come from the server-side fetch.

### Seat model

- Senate: 34 Democratic-caucus seats (incl. King and Sanders) and 31 Republican seats are
  not up; 35 are up (33 regular + Ohio and Florida specials). The model counts the
  non-Republican side, so winning independents (Osborn, Bodnar, Achilles, Bengs) count with
  Democrats. Republicans control at 50 (VP Vance), Democrats need 51.
- One-factor Gaussian copula: race i is won by the viewpoint party if
  `sqrt(rho)*Z + sqrt(1-rho)*e_i < Phi^-1(q_i)`. Each race keeps its market probability
  `q_i`; `rho` sets how strongly races move together. The page integrates over `Z`
  (73-point grid) and counts wins exactly per grid point. Auto mode fits `rho` to the
  cumulative seat distribution of the Kalshi and Polymarket seat-count markets.
- House: same method over the 435 districts that have a market (Polymarket covers all of
  them, Kalshi about 75).

### Known quirks

- Polymarket's House seat market counts Republican seats; we convert with 435 − R.
- On 2026-11-01 the district markets summed to ~246 expected Democratic House seats,
  while the seat-count markets centered at ~240–242. Long-shot pricing roughly cancels
  (+4.6 vs +4.1 phantom seats), so this is a gap between the two kinds of market,
  not a bug in the model.
- California's 40th district is Republican vs. Republican (Calvert vs. Kim); the party
  labels for that race are hard-coded in `P_PARTY_OVERRIDES`.

## Run locally

```bash
python3 fetch.py site && python3 -m http.server 18931 --bind 127.0.0.1 --directory site
```

Written by a team of Konstantin's Claudes, 2026-11-01.
