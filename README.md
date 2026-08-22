# Last.fm Recommendation Engine

A self-hosted, Linux-friendly tool that turns your Last.fm scrobble history
into Spotify-style "Discover" recommendations — new artists and tracks
you probably haven't heard, ranked and explained ("because you play X").

It's a Flask web server with a generated HTML dashboard. No database, no
external services — everything runs on your machine.

## How it works

1. Pulls your **top artists** (all-time and last 3 months) and **top
   tracks** from Last.fm's public API.
2. Builds a weighted taste profile — recent listening counts for more
   than old listening (60/40 split).
3. Walks Last.fm's own `artist.getSimilar` / `track.getSimilar`
   similarity graph starting from your most-weighted artists/tracks,
   scoring every candidate by `seed_weight × similarity_match`.
4. Filters out anything you've already scrobbled, so you only see
   genuinely new artists/tracks.
5. Filters out any tracks you've marked with a thumbs-down (👎), so
   blocked recommendations never resurface.
6. Serves everything through a Flask web server with a refresh button
   and track blocking functionality.

No Last.fm login/session is required — only a free API key, since this
only touches public read-only endpoints.

## Setup

1. Get a free Last.fm API key: https://www.last.fm/api/account/create
   (any app name/description works, you don't need a callback URL).

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Configure:
   ```bash
   cp config.example.json config.json
   ```
   Edit `config.json`:
   ```json
   {
     "api_key": "paste-your-key-here",
     "username": "your-lastfm-username",
     "timezone": "Europe/London"
   }
   ```
   `timezone` is optional — set it to your [IANA timezone
   name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)
   if the "Generated at" timestamp on the dashboard looks wrong. This
   usually means the container's system clock is set to a different
   timezone than you (containers often default to UTC) — you can fix
   it at the source instead with `timedatectl set-timezone Europe/London`
   inside the container, or just set this field and skip touching the
   container's clock.

4. Run the server:
   ```bash
   python3 server.py
   ```

   This starts a Flask web server on port `8080`. Open your browser to
   `http://localhost:8080`. The first request generates a dashboard if
   one doesn't exist yet.

5. Access your dashboard:
   - **Locally:** `http://localhost:8080`
   - **Over the network:** Point your reverse proxy (nginx, Caddy, etc.)
     at `http://<host>:8080`

## Features

### Refresh Button
Click the **Refresh Recommendations** button on the dashboard to regenerate
everything in the background (takes ~15–30 seconds). The page auto-reloads
when complete.

### Track Blocking
Click the **👎 thumbs-down button** on any recommended track to exclude it
from future recommendations. Blocked tracks are saved to `blocked_tracks.json`
and never resurface, even after page reloads, server restarts, or re-runs.
The dashboard rehydrates blocked keys from the server and keeps a local
browser copy for offline fallback.

### Keeping it Fresh

Three ways to refresh:

- **Click the button** — on the dashboard itself (easiest if you have access)
- **Re-run the server** — `python3 server.py` regenerates everything on startup
- **Cron** — automate background refreshes (make sure server is running):
   ```bash
   # Add to crontab to refresh weekly
   0 9 * * 1 curl -s http://localhost:8080/refresh -X POST > /dev/null
   ```

## Running as a persistent service

To keep the server running after logout or reboot, use systemd.

`lastfm-dashboard.service` is included — adjust the paths if your install
isn't at `/opt/lastfm-recommender`, then:

```bash
sudo cp lastfm-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lastfm-dashboard
```

Check it's up with `systemctl status lastfm-dashboard` or `curl localhost:8080`.

## Tuning it

Everything is in `lastfm_recommender.py` — a few knobs worth knowing:

- `build_seed_weights()` — the `0.4`/`0.6` split between all-time and
  recent (3-month) listening. Raise the recent weight if you want
  recommendations to chase your current mood more aggressively.
- `recommend_artists(seed_limit=40, per_seed=20, result_limit=40)` —
  how many of your top artists to use as seeds, how many similar
  artists to pull per seed, and how many final recommendations to show.
- `recommend_tracks(...)` — same idea, for track-level recommendations.
- `get_top_artists(..., period="overall", limit=200)` — the size of
  your "known artists" exclusion list. Lower it if you want
  recommendations to include artists you've only lightly played.

## Notes & limits

- Uses only Last.fm's public API — respects their rate limits with a
  small delay between calls (~0.2s), so a full run makes a few dozen
  requests over ~20 seconds.
- Artist/track "similarity" comes entirely from Last.fm's own
  crowd-sourced similarity graph, the same data Last.fm's own
  recommendations are built from.
- If your scrobble history is thin (a few hundred scrobbles), the
  "Discover" lists may come back sparse — the algorithm needs a
  reasonable number of top artists to seed from.
- Blocked tracks are stored in `blocked_tracks.json`. Delete this file
  to unblock all tracks and start fresh.

## Files

- `lastfm_recommender.py` — Core recommendation engine and dashboard rendering
- `server.py` — Flask web server with refresh and track-blocking endpoints
- `config.json` — Your credentials (created from `config.example.json`)
- `dashboard.html` — Generated dashboard (served by Flask)
- `recommendations.json` — Raw recommendation data as JSON
- `blocked_tracks.json` — List of blocked track keys (auto-created on first block)
- `lastfm-dashboard.service` — systemd service file for persistent running
````
