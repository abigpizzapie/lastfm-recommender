# Last.fm Recommendation Engine

A self-hosted, Linux-friendly tool that turns your Last.fm scrobble history
into Spotify-style "Discover" recommendations — new artists and tracks
you probably haven't heard, ranked and explained ("because you play X").

It's a single Python script + a generated static HTML dashboard. No
database, no server process, nothing to keep running.

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
5. Renders everything into a single `dashboard.html` file you open
   in a browser.

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

4. Run it:
   ```bash
   python3 lastfm_recommender.py
   ```

   This takes roughly 15-30 seconds (it's making ~60-80 rate-limited
   calls to Last.fm's API). It writes:
   - `recommendations.json` — raw data, if you want to script against it
   - `dashboard.html` — the visual report

5. Open `dashboard.html` in any browser (double-click it, or `xdg-open dashboard.html`).

## Running it as a persistent server (with a Refresh button)

If you're hosting this somewhere reachable over the network — a Proxmox
LXC container behind nginx, for example — run `server.py` instead of
the script directly. It serves the dashboard over HTTP and adds a
**Refresh Recommendations** button to the page that regenerates
everything in the background, so you don't need shell access to
refresh it.

```bash
python3 server.py
```

This starts a server on port `8080`. Point your reverse proxy at
`http://<host>:8080`. The first request generates a dashboard if one
doesn't exist yet; after that, the button on the page triggers a
refresh (usually 15–30 seconds) and reloads automatically when done.

**Keep it running with systemd** so it survives reboots and restarts
on failure. `lastfm-dashboard.service` is included — adjust the paths
if your install isn't at `/opt/lastfm-recommender`, then:

```bash
cp lastfm-dashboard.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now lastfm-dashboard
```

Check it's up with `systemctl status lastfm-dashboard` or `curl
localhost:8080`.

## Keeping it fresh

Three ways to refresh, pick whichever fits how you're running it:

- **Click the button** — if you're running `server.py`, just click
  "Refresh Recommendations" on the dashboard itself.
- **Re-run manually** — `python3 lastfm_recommender.py` regenerates
  `dashboard.html` directly; works whether or not `server.py` is running.
- **Cron** — automate it, e.g. weekly, regardless of which mode you use:
  ```bash
  crontab -e
  # add:
  0 9 * * 1 cd /path/to/lastfm-recommender && /usr/bin/python3 lastfm_recommender.py
  ```

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
