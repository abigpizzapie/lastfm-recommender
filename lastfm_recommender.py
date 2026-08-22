#!/usr/bin/env python3
"""
Last.fm Recommendation Engine
==============================
Pulls your scrobble history from Last.fm's public API, builds a weighted
taste profile (recent listening counts more than old listening), then
walks Last.fm's artist/track similarity graph to surface things you
probably haven't heard yet -- similar to how Spotify's "Discover" works,
but self-hosted and running entirely on your own machine.

Usage:
    1. cp config.example.json config.json
       and fill in your Last.fm API key (https://www.last.fm/api/account/create)
       and username.
    2. pip install -r requirements.txt
    3. python3 server.py
    4. open http://localhost:8080 in a browser

No Last.fm session/auth is required -- this only touches public read-only
endpoints (user.*, artist.*, track.*), so all it needs is an API key.
"""

import html
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus
import base64

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python <3.9 fallback
    ZoneInfo = None

import requests

API_ROOT = "https://ws.audioscrobbler.com/2.0/"
REQUEST_DELAY = 0.2  # seconds between calls, keeps us well under Last.fm's rate limit

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
OUTPUT_HTML = BASE_DIR / "dashboard.html"
OUTPUT_JSON = BASE_DIR / "recommendations.json"
BLOCKED_TRACKS_PATH = BASE_DIR / "blocked_tracks.json"


# --------------------------------------------------------------------------
# Blocked tracks management
# --------------------------------------------------------------------------

def load_blocked_tracks():
    """Load the set of blocked track keys from disk."""
    if not BLOCKED_TRACKS_PATH.exists():
        return set()
    try:
        data = json.loads(BLOCKED_TRACKS_PATH.read_text())
        return set(data.get("blocked", []))
    except Exception:
        return set()


def save_blocked_tracks(blocked):
    """Save the set of blocked track keys to disk."""
    data = {"blocked": sorted(list(blocked))}
    BLOCKED_TRACKS_PATH.write_text(json.dumps(data, indent=2))


def parse_track_key(key):
    """Parse a track key 'artist::track' back into components."""
    parts = key.split("::")
    if len(parts) == 2:
        return parts[0], parts[1]
    return key, ""


# --------------------------------------------------------------------------
# SVG icon helpers
# --------------------------------------------------------------------------

def get_minus_icon_data_uri(color):
    """Generate a data URI for the minus icon SVG with the specified color."""
    svg = f"""<?xml version="1.0" encoding="utf-8"?>
<svg version="1.1" xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 50 50">
<circle fill="none" stroke="{color}" stroke-linejoin="round" stroke-width="2" cx="25" cy="25" r="23.667"/>
<line fill="none" stroke="{color}" stroke-linecap="round" stroke-linejoin="round" stroke-width="3" x1="39.5" y1="25" x2="10.5" y2="25"/>
</svg>"""
    encoded = base64.b64encode(svg.encode()).decode()
    return f"data:image/svg+xml;base64,{encoded}"


# --------------------------------------------------------------------------
# Last.fm API plumbing
# --------------------------------------------------------------------------

def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(
            "config.json not found.\n"
            "Copy config.example.json to config.json and fill in your "
            "Last.fm API key + username, then run this again."
        )
    cfg = json.loads(CONFIG_PATH.read_text())
    if not cfg.get("api_key") or "your-lastfm" in cfg.get("api_key", ""):
        sys.exit("config.json is missing a real api_key. Get one at "
                  "https://www.last.fm/api/account/create")
    if not cfg.get("username"):
        sys.exit("config.json is missing your Last.fm username.")
    return cfg


def now_str(config):
    """Current time formatted for display, honoring an optional
    "timezone" key in config.json (e.g. "Europe/London"). Falls back
    to the system's local time if unset or invalid."""
    tz_name = config.get("timezone")
    if tz_name and ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def api_call(method, api_key, **params):
    query = {"method": method, "api_key": api_key, "format": "json", **params}
    resp = requests.get(API_ROOT, params=query, timeout=15)
    time.sleep(REQUEST_DELAY)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} on {method}: {resp.text[:200]}")
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Last.fm error {data['error']} on {method}: {data.get('message')}")
    return data


def get_top_artists(api_key, user, period, limit=200):
    data = api_call("user.gettopartists", api_key, user=user, period=period, limit=limit)
    artists = data.get("topartists", {}).get("artist", [])
    return [artists] if isinstance(artists, dict) else artists


def get_top_tracks(api_key, user, period, limit=100):
    data = api_call("user.gettoptracks", api_key, user=user, period=period, limit=limit)
    tracks = data.get("toptracks", {}).get("track", [])
    return [tracks] if isinstance(tracks, dict) else tracks


def get_similar_artists(api_key, artist_name, limit=20):
    try:
        data = api_call("artist.getsimilar", api_key, artist=artist_name, limit=limit, autocorrect=1)
    except RuntimeError:
        return []
    sim = data.get("similarartists", {}).get("artist", [])
    return [sim] if isinstance(sim, dict) else sim


def get_similar_tracks(api_key, artist_name, track_name, limit=12):
    try:
        data = api_call("track.getsimilar", api_key, artist=artist_name, track=track_name,
                         limit=limit, autocorrect=1)
    except RuntimeError:
        return []
    sim = data.get("similartracks", {}).get("track", [])
    return [sim] if isinstance(sim, dict) else sim


def get_top_tags(api_key, artist_name, limit=5):
    try:
        data = api_call("artist.gettoptags", api_key, artist=artist_name, autocorrect=1)
    except RuntimeError:
        return []
    tags = data.get("toptags", {}).get("tag", [])
    tags = [tags] if isinstance(tags, dict) else tags
    return [t["name"] for t in tags[:limit] if t.get("name")]


def artist_name_of(entry):
    a = entry.get("artist")
    if isinstance(a, dict):
        return a.get("name")
    return a


# --------------------------------------------------------------------------
# Taste profile + recommendation logic
# --------------------------------------------------------------------------

def build_seed_weights(overall, recent):
    """Blend long-term and recent (last 3 months) listening into one score
    per artist, 0..1, weighted toward recent taste."""

    def playcount_map(items):
        m = {}
        for it in items:
            name = it.get("name")
            if name:
                m[name] = int(it.get("playcount", 0) or 0)
        return m

    overall_pc = playcount_map(overall)
    recent_pc = playcount_map(recent)
    max_overall = max(overall_pc.values(), default=1) or 1
    max_recent = max(recent_pc.values(), default=1) or 1

    weights = {}
    for name in set(overall_pc) | set(recent_pc):
        so = overall_pc.get(name, 0) / max_overall
        sr = recent_pc.get(name, 0) / max_recent
        weights[name] = 0.4 * so + 0.6 * sr
    return weights


def recommend_artists(api_key, seed_weights, known_artists,
                       seed_limit=40, per_seed=20, result_limit=40):
    seeds = sorted(seed_weights.items(), key=lambda kv: kv[1], reverse=True)[:seed_limit]
    scores = defaultdict(float)
    best_reason = {}
    urls = {}

    for seed_name, seed_weight in seeds:
        for s in get_similar_artists(api_key, seed_name, limit=per_seed):
            cand = s.get("name")
            if not cand or cand.lower() in known_artists:
                continue
            match = float(s.get("match", 0) or 0)
            contribution = seed_weight * match
            scores[cand] += contribution
            urls.setdefault(cand, s.get("url"))
            if cand not in best_reason or contribution > best_reason[cand][1]:
                best_reason[cand] = (seed_name, contribution)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:result_limit]
    return [
        {
            "name": n,
            "score": round(s, 4),
            "because_of": best_reason[n][0],
            "lastfm_url": urls.get(n) or f"https://www.last.fm/music/{quote_plus(n)}",
            "youtube_url": f"https://www.youtube.com/results?search_query={quote_plus(n)}",
            "spotify_url": f"https://open.spotify.com/search/{quote_plus(n)}",
        }
        for n, s in ranked
    ]


def recommend_tracks(api_key, seed_tracks, known_track_keys, blocked_track_keys,
                      seed_limit=20, per_seed=15, result_limit=40):
    seeds = seed_tracks[:seed_limit]
    max_pc = max((int(t.get("playcount", 0) or 0) for t in seeds), default=1) or 1

    scores = defaultdict(float)
    meta = {}
    best_reason = {}

    for t in seeds:
        artist, track = artist_name_of(t), t.get("name")
        if not artist or not track:
            continue
        seed_weight = int(t.get("playcount", 0) or 0) / max_pc

        for s in get_similar_tracks(api_key, artist, track, limit=per_seed):
            cand_artist, cand_track = artist_name_of(s), s.get("name")
            if not cand_artist or not cand_track:
                continue
            key = f"{cand_artist.lower()}::{cand_track.lower()}"
            if key in known_track_keys or key in blocked_track_keys:
                continue
            match = float(s.get("match", 0) or 0)
            contribution = seed_weight * match
            scores[key] += contribution
            meta[key] = {"artist": cand_artist, "track": cand_track, "url": s.get("url")}
            if key not in best_reason or contribution > best_reason[key][1]:
                best_reason[key] = (f"{artist} \u2013 {track}", contribution)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:result_limit]
    results = []
    for k, s in ranked:
        m = meta[k]
        query = f"{m['artist']} {m['track']}"
        results.append({
            "artist": m["artist"],
            "track": m["track"],
            "score": round(s, 4),
            "because_of": best_reason[k][0],
            "track_key": k,
            "lastfm_url": m["url"] or f"https://www.last.fm/music/{quote_plus(m['artist'])}/_/{quote_plus(m['track'])}",
            "youtube_url": f"https://www.youtube.com/results?search_query={quote_plus(query)}",
            "spotify_url": f"https://open.spotify.com/search/{quote_plus(query)}",
        })
    return results


def build_taste_tags(api_key, seed_weights, top_n=12, tag_top_n=10):
    top_seeds = sorted(seed_weights.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    tag_scores = defaultdict(float)
    for name, weight in top_seeds:
        for tag in get_top_tags(api_key, name, limit=5):
            tag_scores[tag.lower()] += weight
    ranked = sorted(tag_scores.items(), key=lambda kv: kv[1], reverse=True)[:tag_top_n]
    return [t for t, _ in ranked]


# --------------------------------------------------------------------------
# Dashboard rendering (pure HTML/CSS, no JS required)
# --------------------------------------------------------------------------

def esc(s):
    return html.escape(str(s), quote=True)


def bar(pct, color):
    pct = max(2, min(100, pct))
    return f'<div class="bar"><div class="bar-fill" style="width:{pct:.0f}%;background:{color}"></div></div>'


def render_dashboard(payload):
    stats, artist_recs, track_recs = payload["stats"], payload["artist_recs"], payload["track_recs"]

    max_artist_score = max((a["score"] for a in artist_recs), default=1) or 1
    max_track_score = max((t["score"] for t in track_recs), default=1) or 1
    max_top_playcount = max((a["playcount"] for a in stats["top_artists"]), default=1) or 1

    tag_chips = "".join(f'<span class="chip">{esc(t)}</span>' for t in stats["top_tags"]) \
        or '<span class="chip chip-muted">not enough tag data yet</span>'

    top_artist_rows = "".join(f"""
        <div class="row">
          <span class="row-index">{i:02d}</span>
          <span class="row-name">{esc(a['name'])}</span>
          {bar(100 * a['playcount'] / max_top_playcount, 'var(--accent)')}
          <span class="row-count">{a['playcount']:,} plays</span>
        </div>""" for i, a in enumerate(stats["top_artists"], 1))

    artist_cards = "".join(f"""
        <div class="card">
          <div class="card-index">{i:02d}</div>
          <div class="card-body">
            <div class="card-name">{esc(a['name'])}</div>
            <div class="card-reason">because you play <strong>{esc(a['because_of'])}</strong></div>
            {bar(100 * a['score'] / max_artist_score, 'var(--accent)')}
            <div class="card-links">
              <a href="{esc(a['lastfm_url'])}" target="_blank" rel="noopener">Last.fm</a>
              <a href="{esc(a['youtube_url'])}" target="_blank" rel="noopener">YouTube</a>
              <a href="{esc(a['spotify_url'])}" target="_blank" rel="noopener">Spotify</a>
            </div>
          </div>
        </div>""" for i, a in enumerate(artist_recs, 1)) or '<p class="empty">No new artists surfaced this run.</p>'

    track_cards = "".join(f"""
        <div class="card track-card" data-track-key="{esc(t['track_key'])}">
          <div class="card-index">{i:02d}</div>
          <div class="card-body">
            <div class="card-name">{esc(t['track'])}</div>
            <div class="card-sub">{esc(t['artist'])}</div>
            <div class="card-reason">because of <strong>{esc(t['because_of'])}</strong></div>
            {bar(100 * t['score'] / max_track_score, 'var(--accent2)')}
            <div class="card-links">
              <a href="{esc(t['lastfm_url'])}" target="_blank" rel="noopener">Last.fm</a>
              <a href="{esc(t['youtube_url'])}" target="_blank" rel="noopener">YouTube</a>
              <a href="{esc(t['spotify_url'])}" target="_blank" rel="noopener">Spotify</a>
            </div>
            <button class="btn-block" data-track-key="{esc(t['track_key'])}" title="Don't recommend this track again"></button>
          </div>
        </div>""" for i, t in enumerate(track_recs, 1)) or '<p class="empty">No new tracks surfaced this run.</p>'

    # Generate SVG data URIs for the minus icon
    minus_icon_default = get_minus_icon_data_uri("#c85656")
    minus_icon_hover = get_minus_icon_data_uri("#22ff00")

    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Music Discovery Dashboard \u2014 {esc(stats['username'])}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #15120f;
    --panel: #1c1814;
    --paper: #efe7d8;
    --ink: #efe7d8;
    --dim: #a89e8c;
    --accent: #22ff00;
    --accent2: #3e6e64;
    --line: rgba(239,231,216,0.12);
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: 'Inter', system-ui, sans-serif;
    line-height: 1.4;
  }}
  a {{ color: var(--accent); }}
  .wrap {{ max-width: 900px; margin: 0 auto; padding: 56px 24px 96px; }}

  header.masthead {{
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    flex-wrap: wrap;
    gap: 16px;
    border-bottom: 1px solid var(--line);
    padding-bottom: 28px;
    margin-bottom: 40px;
  }}
  .header-buttons {{
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
  }}
  .btn-refresh {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    background: var(--accent);
    color: #15120f;
    border: none;
    padding: 10px 18px;
    border-radius: 4px;
    cursor: pointer;
    flex-shrink: 0;
  }}
  .btn-refresh:hover {{ background: #dba043; }}
  .btn-refresh:focus-visible {{ outline: 2px solid var(--paper); outline-offset: 2px; }}
  .btn-blocked {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    background: var(--dim);
    color: var(--panel);
    border: none;
    padding: 10px 18px;
    border-radius: 4px;
    cursor: pointer;
    flex-shrink: 0;
  }}
  .btn-blocked:hover {{ background: var(--paper); }}
  .btn-blocked:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
  .btn-blocked.has-blocked {{
    background: var(--accent2);
    color: var(--paper);
  }}
  .btn-blocked.has-blocked:hover {{ background: #4a8074; }}
  
  .eyebrow {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--accent);
    margin-bottom: 10px;
  }}
  h1 {{
    font-family: 'Fraunces', serif;
    font-weight: 600;
    font-size: clamp(34px, 6vw, 52px);
    margin: 0 0 6px;
  }}
  .sub {{ color: var(--dim); font-size: 15px; }}

  section {{ margin-bottom: 56px; }}
  h2 {{
    font-family: 'Fraunces', serif;
    font-size: 22px;
    font-weight: 600;
    margin: 0 0 4px;
  }}
  .section-note {{ color: var(--dim); font-size: 13px; margin-bottom: 20px; }}

  .chips {{ display: flex; flex-wrap: wrap; gap: 8px; }}
  .chip {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    padding: 6px 12px;
    border: 1px solid var(--line);
    border-radius: 999px;
    color: var(--paper);
    background: var(--panel);
  }}
  .chip-muted {{ color: var(--dim); }}

  .row {{
    display: grid;
    grid-template-columns: 28px 1fr 120px 90px;
    align-items: center;
    gap: 14px;
    padding: 10px 0;
    border-bottom: 1px solid var(--line);
    font-size: 14px;
  }}
  .row-index {{
    font-family: 'JetBrains Mono', monospace;
    color: var(--dim);
    font-size: 12px;
  }}
  .row-count {{
    font-family: 'JetBrains Mono', monospace;
    color: var(--dim);
    font-size: 12px;
    text-align: right;
  }}

  .bar {{
    height: 5px;
    background: var(--line);
    border-radius: 3px;
    overflow: hidden;
  }}
  .bar-fill {{ height: 100%; border-radius: 3px; }}

  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
    gap: 12px;
  }}
  .card {{
    display: flex;
    gap: 12px;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 4px;
    padding: 16px;
    position: relative;
  }}
  .card-index {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    color: var(--accent);
    flex-shrink: 0;
  }}
  .card-body {{ flex: 1; min-width: 0; }}
  .card-name {{
    font-family: 'Fraunces', serif;
    font-size: 17px;
    font-weight: 600;
    margin-bottom: 2px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }}
  .card-sub {{ color: var(--dim); font-size: 13px; margin-bottom: 4px; }}
  .card-reason {{
    font-size: 12px;
    color: var(--dim);
    margin-bottom: 10px;
  }}
  .card-reason strong {{ color: var(--paper); font-weight: 500; }}

  .card-links {{
    display: flex;
    gap: 12px;
    margin-top: 10px;
  }}
  .card-links a {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    letter-spacing: 0.03em;
    text-transform: uppercase;
    color: var(--dim);
    text-decoration: none;
    border-bottom: 1px solid transparent;
  }}
  .card-links a:hover {{
    color: var(--paper);
    border-bottom-color: var(--accent);
  }}

  .track-card {{
    padding-bottom: 52px;
  }}
  .track-card .card-links {{
    padding-right: 44px;
  }}

  .btn-block {{
    position: absolute;
    bottom: 12px;
    right: 12px;
    width: 28px;
    height: 28px;
    background: none;
    border: none;
    padding: 0;
    cursor: pointer;
    transition: transform 0.2s ease;
    background-image: url('{minus_icon_default}');
    background-size: contain;
    background-repeat: no-repeat;
    background-position: center;
  }}
  .btn-block:hover {{
    transform: scale(1.1);
    background-image: url('{minus_icon_hover}');
  }}
  .btn-block:active {{
    transform: scale(0.95);
  }}
  .btn-block:disabled {{
    opacity: 0.5;
    cursor: not-allowed;
  }}

  .track-card.blocked {{
    display: none;
  }}

  .empty {{ color: var(--dim); font-size: 14px; }}

  .modal {{
    display: none;
    position: fixed;
    z-index: 1000;
    left: 0;
    top: 0;
    width: 100%;
    height: 100%;
    background-color: rgba(0, 0, 0, 0.8);
  }}
  .modal.show {{
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  .modal-content {{
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 32px;
    max-width: 500px;
    max-height: 70vh;
    overflow-y: auto;
    color: var(--ink);
  }}
  .modal-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 20px;
    border-bottom: 1px solid var(--line);
    padding-bottom: 16px;
  }}
  .modal-header h2 {{
    margin: 0;
    font-family: 'Fraunces', serif;
    font-size: 20px;
  }}
  .modal-close {{
    background: none;
    border: none;
    color: var(--dim);
    font-size: 24px;
    cursor: pointer;
    padding: 0;
    width: 28px;
    height: 28px;
  }}
  .modal-close:hover {{
    color: var(--paper);
  }}
  .blocked-track-item {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 12px;
    border-bottom: 1px solid var(--line);
    font-size: 14px;
  }}
  .blocked-track-item:last-child {{
    border-bottom: none;
  }}
  .blocked-track-info {{
    flex: 1;
  }}
  .blocked-track-name {{
    font-weight: 500;
    color: var(--paper);
    margin-bottom: 4px;
  }}
  .blocked-track-artist {{
    font-size: 12px;
    color: var(--dim);
  }}
  .btn-unblock {{
    background: none;
    border: none;
    color: var(--accent2);
    cursor: pointer;
    padding: 4px 8px;
    font-size: 12px;
    text-transform: uppercase;
    font-family: 'JetBrains Mono', monospace;
    white-space: nowrap;
    margin-left: 12px;
  }}
  .btn-unblock:hover {{
    color: #4a8074;
  }}
  .modal-empty {{
    color: var(--dim);
    text-align: center;
    padding: 20px;
    font-size: 14px;
  }}

  footer {{
    border-top: 1px solid var(--line);
    padding-top: 20px;
    color: var(--dim);
    font-size: 12px;
    font-family: 'JetBrains Mono', monospace;
  }}

  section.hidden {{
    display: none;
  }}

  @media (max-width: 560px) {{
    .row {{ grid-template-columns: 22px 1fr 70px; }}
    .row-count {{ display: none; }}
    .header-buttons {{
      flex-direction: column;
      width: 100%;
    }}
    .btn-refresh, .btn-blocked {{
      width: 100%;
    }}
    .modal-content {{
      margin: 20px;
      max-width: calc(100% - 40px);
    }}
  }}
</style>
</head>
<body>
<div class="wrap">

  <header class="masthead">
    <div>
      <div class="eyebrow">Discover \u2014 self-hosted music recommendations</div>
      <h1>{esc(stats['username'])}'s Discovery Sheet</h1>
      <div class="sub">Generated {esc(stats['generated_at'])} from {stats['known_artist_count']:,} known artists on Last.fm</div>
    </div>
    <div class="header-buttons">
      <button type="button" class="btn-blocked" id="btn-show-blocked" title="View blocked tracks">Blocked (0)</button>
      <form method="post" action="/refresh" style="margin: 0;">
        <button type="submit" class="btn-refresh">Refresh Recommendations</button>
      </form>
    </div>
  </header>

  <section>
    <h2>Your Sound</h2>
    <div class="section-note">Top tags across your most-played artists right now</div>
    <div class="chips">{tag_chips}</div>
  </section>

  <section>
    <h2>Heaviest Rotation</h2>
    <div class="section-note">Your top 10 artists by all-time playcount</div>
    {top_artist_rows}
  </section>

  <section class="hidden">
    <h2>Discover Artists</h2>
    <div class="section-note">New-to-you artists, ranked by similarity to what you already play. Excludes anyone already in your library.</div>
    <div class="grid">{artist_cards}</div>
  </section>

  <section>
    <h2>Discover Tracks</h2>
    <div class="section-note">Individual tracks similar to your most-played songs this quarter, excluding anything you've already scrobbled.</div>
    <div class="grid">{track_cards}</div>
  </section>

  <footer>
    Built from public Last.fm data \u00b7 re-run server.py anytime to refresh
  </footer>

</div>

<!-- Blocked Tracks Modal -->
<div id="blocked-modal" class="modal">
  <div class="modal-content">
    <div class="modal-header">
      <h2>Blocked Tracks</h2>
      <button type="button" class="modal-close" id="modal-close">&times;</button>
    </div>
    <div id="blocked-list">
      <div class="modal-empty">No blocked tracks yet</div>
    </div>
  </div>
</div>

<script>
// Load blocked tracks from localStorage, then merge server-backed blocks
async function loadBlockedTracks() {{
  let blocked = {{}};
  const stored = localStorage.getItem('blockedTracks');
  if (stored) {{
    try {{
      blocked = JSON.parse(stored) || {{}};
    }} catch (err) {{
      blocked = {{}};
    }}
  }}

  try {{
    const response = await fetch('/blocked-tracks');
    if (response.ok) {{
      const data = await response.json();
      for (const key of data.blocked || []) {{
        blocked[key] = true;
      }}
      saveBlockedTracks(blocked);
    }}
  }} catch (err) {{
    // offline mode: localStorage remains the source of truth
  }}

  return blocked;
}}

function saveBlockedTracks(blocked) {{
  localStorage.setItem('blockedTracks', JSON.stringify(blocked));
}}

function updateBlockedButton(blocked) {{
  const btn = document.getElementById('btn-show-blocked');
  const count = Object.keys(blocked).length;
  btn.textContent = `Blocked (${{count}})`;
  if (count > 0) {{
    btn.classList.add('has-blocked');
  }} else {{
    btn.classList.remove('has-blocked');
  }}
}}

function renderBlockedList(blocked) {{
  const listContainer = document.getElementById('blocked-list');
  const keys = Object.keys(blocked).sort();
  
  if (keys.length === 0) {{
    listContainer.innerHTML = '<div class="modal-empty">No blocked tracks yet</div>';
    return;
  }}
  
  listContainer.innerHTML = keys.map(key => {{
    const [artist, track] = key.split('::');
    return `
      <div class="blocked-track-item">
        <div class="blocked-track-info">
          <div class="blocked-track-name">${{escapeHtml(track)}}</div>
          <div class="blocked-track-artist">${{escapeHtml(artist)}}</div>
        </div>
        <button class="btn-unblock" data-key="${{escapeHtml(key)}}">Unblock</button>
      </div>
    `;
  }}).join('');
  
  // Attach unblock handlers
  listContainer.querySelectorAll('.btn-unblock').forEach(btn => {{
    btn.addEventListener('click', (e) => {{
      const key = btn.dataset.key;
      const card = document.querySelector(`[data-track-key="${{escapeHtml(key)}}"]`);
      delete blocked[key];
      saveBlockedTracks(blocked);
      updateBlockedButton(blocked);
      renderBlockedList(blocked);
      if (card) {{
        card.classList.remove('blocked');
        const blockBtn = card.querySelector('.btn-block');
        if (blockBtn) blockBtn.disabled = false;
      }}
    }});
  }});
}}

function applyBlockedTracks(blocked) {{
  document.querySelectorAll('.track-card[data-track-key]').forEach(card => {{
    const trackKey = card.dataset.trackKey;
    const blockBtn = card.querySelector('.btn-block');
    if (blocked[trackKey]) {{
      card.classList.add('blocked');
      if (blockBtn) blockBtn.disabled = true;
    }} else {{
      card.classList.remove('blocked');
      if (blockBtn) blockBtn.disabled = false;
    }}
  }});
}}

function escapeHtml(text) {{
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}}

// Initialize
let blocked = {{}};

// Modal controls
const modal = document.getElementById('blocked-modal');
const showBtn = document.getElementById('btn-show-blocked');
const closeBtn = document.getElementById('modal-close');

showBtn.addEventListener('click', () => {{
  renderBlockedList(blocked);
  modal.classList.add('show');
}});

closeBtn.addEventListener('click', () => {{
  modal.classList.remove('show');
}});

modal.addEventListener('click', (e) => {{
  if (e.target === modal) {{
    modal.classList.remove('show');
  }}
}});

// Block track handlers
document.querySelectorAll('.btn-block').forEach(btn => {{
  btn.addEventListener('click', async (e) => {{
    e.preventDefault();
    const trackKey = btn.dataset.trackKey;
    const card = btn.closest('.card');
    
    // Store in localStorage
    blocked[trackKey] = true;
    saveBlockedTracks(blocked);
    updateBlockedButton(blocked);
    
    // Update UI
    card.classList.add('blocked');
    btn.disabled = true;
    
    // Try to sync with server if available
    try {{
      await fetch('/block-track', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{track_key: trackKey}})
      }});
    }} catch (err) {{
      console.log('Server sync failed (offline mode):', err);
    }}
  }});
}});

async function initBlockedState() {{
  blocked = await loadBlockedTracks();
  updateBlockedButton(blocked);
  applyBlockedTracks(blocked);
}}

initBlockedState();
</script>
</body>
</html>
"""
    OUTPUT_HTML.write_text(doc, encoding="utf-8")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def generate(config):
    """Fetch data, compute recommendations, write JSON + dashboard.html.
    Returns the payload dict. Callable from the CLI or from server.py."""
    api_key, user = config["api_key"], config["username"]

    print(f"Fetching Last.fm data for {user}...")
    overall_artists = get_top_artists(api_key, user, "overall", limit=200)
    recent_artists = get_top_artists(api_key, user, "3month", limit=100)
    recent_tracks = get_top_tracks(api_key, user, "3month", limit=50)
    overall_tracks = get_top_tracks(api_key, user, "12month", limit=200)

    if not overall_artists:
        sys.exit(f"No scrobble data found for '{user}'. Check the username in config.json.")

    known_artists = {a["name"].lower() for a in overall_artists if a.get("name")}
    known_track_keys = set()
    for t in overall_tracks:
        artist = artist_name_of(t)
        if artist and t.get("name"):
            known_track_keys.add(f"{artist.lower()}::{t['name'].lower()}")

    blocked_track_keys = load_blocked_tracks()

    seed_weights = build_seed_weights(overall_artists, recent_artists)

    print("Walking artist similarity graph (this takes ~15-20s)...")
    artist_recs = recommend_artists(api_key, seed_weights, known_artists)

    print("Walking track similarity graph...")
    track_recs = recommend_tracks(api_key, recent_tracks, known_track_keys, blocked_track_keys)

    print("Building taste profile...")
    taste_tags = build_taste_tags(api_key, seed_weights)

    stats = {
        "username": user,
        "known_artist_count": len(known_artists),
        "top_artists": [
            {"name": a["name"], "playcount": int(a.get("playcount", 0) or 0)}
            for a in overall_artists[:10]
        ],
        "top_tags": taste_tags,
        "generated_at": now_str(config),
    }

    payload = {"stats": stats, "artist_recs": artist_recs, "track_recs": track_recs}
    OUTPUT_JSON.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {OUTPUT_JSON}")

    render_dashboard(payload)
    print(f"Wrote {OUTPUT_HTML}")
    return payload


def main():
    config = load_config()
    generate(config)
    print("\nDone. Open http://localhost:8080 in your browser (requires server.py to be running).")


if __name__ == "__main__":
    main()
