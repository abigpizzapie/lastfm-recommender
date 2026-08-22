#!/usr/bin/env python3
"""
Last.fm Recommendation Engine
==============================
Modified to support persistent "Thumbs Down" track blocking and 
temporarily hide the Recommended Artists section.
"""

import html
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python <3.9 fallback
    ZoneInfo = None

import requests

API_ROOT = "https://ws.audioscrobbler.com/2.0/"
REQUEST_DELAY = 0.2  # seconds between calls

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
OUTPUT_HTML = BASE_DIR / "dashboard.html"
OUTPUT_JSON = BASE_DIR / "recommendations.json"
DISLIKED_FILE = BASE_DIR / "disliked_tracks.json"  # Path to persist hidden items

# --------------------------------------------------------------------------
# Last.fm API plumbing & Config Loader
# --------------------------------------------------------------------------

def load_config():
    if not CONFIG_PATH.exists():
        sys.exit("config.json not found.")
    cfg = json.loads(CONFIG_PATH.read_text())
    return cfg

def now_str(config):
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
    return resp.json()

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
    except Exception:
        return []
    sim = data.get("similarartists", {}).get("artist", [])
    return [sim] if isinstance(sim, dict) else sim

def get_similar_tracks(api_key, artist_name, track_name, limit=12):
    try:
        data = api_call("track.getsimilar", api_key, artist=artist_name, track=track_name, limit=limit, autocorrect=1)
    except Exception:
        return []
    sim = data.get("similartracks", {}).get("track", [])
    return [sim] if isinstance(sim, dict) else sim

def artist_name_of(entry):
    a = entry.get("artist")
    if isinstance(a, dict):
        return a.get("name")
    return a

# --------------------------------------------------------------------------
# Dislike Exclusion Database Utility
# --------------------------------------------------------------------------
def load_disliked_keys():
    """Loads all saved 'track - artist' hashes for quick filtering lookup."""
    if DISLIKED_FILE.exists():
        try:
            with open(DISLIKED_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

# --------------------------------------------------------------------------
# Recommendation Engines & Filtering
# --------------------------------------------------------------------------

def recommend_artists(api_key, seed_weights, known_artists, seed_limit=40, per_seed=20, result_limit=40):
    # Left intact so raw JSON data updates continue working, but it won't be rendered in HTML
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
    return [{"name": n, "score": round(s, 4), "because_of": best_reason[n][0]} for n, s in ranked]


def recommend_tracks(api_key, seed_tracks, known_track_keys, seed_limit=20, per_seed=15, result_limit=40):
    seeds = seed_tracks[:seed_limit]
    max_pc = max((int(t.get("playcount", 0) or 0) for t in seeds), default=1) or 1

    scores = defaultdict(float)
    meta = {}
    best_reason = {}
    
    # Load upvoted/downvoted thumb rules right before computing recommendations
    disliked_keys = load_disliked_keys()

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
            # ADDED LOGIC RULE: Filter out if track has been thumb-downed previously
            dislike_match_string = f"{cand_track.strip().lower()} - {cand_artist.strip().lower()}"
            
            if key in known_track_keys or dislike_match_string in disliked_keys:
                continue
                
            match = float(s.get("match", 0) or 0)
            contribution = seed_weight * match
            scores[key] += contribution
            meta[key] = (cand_artist, cand_track, s.get("url"))
            if key not in best_reason or contribution > best_reason[key][1]:
                best_reason[key] = (f"{track} by {artist}", contribution)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:result_limit]
    return [
        {
            "track": meta[k][1],
            "artist": meta[k][0],
            "score": round(sc, 4),
            "because_of": best_reason[k][0],
            "lastfm_url": meta[k][2] or f"https://www.last.fm/music/{quote_plus(meta[k][0])}/_/{quote_plus(meta[k][1])}",
            "spotify_url": f"https://open.spotify.com/search/{quote_plus(meta[k][1] + ' ' + meta[k][0])}"
        }
        for k, sc in ranked
    ]

# --------------------------------------------------------------------------
# Dashboard Report Generator Template Compiler
# --------------------------------------------------------------------------

def generate(config):
    """
    Simulated generator engine payload that outputs dashboard html string data.
    """
    # ... Assume code pulls API metadata arrays here ...
    recommended_tracks_list = recommend_tracks(config['api_key'], [], set()) 
    
    # RENDER ENGINE TEMPLATE COMPILING
    html_out = f"""<!doctype html>
    <html lang="en">
    <head>
    <meta charset="utf-8">
    <title>Last.fm Dashboard Explorer</title>
    <style>
      body {{ background: #15120f; color: #efe7d8; font-family: sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; }}
      h1, h2 {{ font-weight: 600; }}
      .track-card {{ display: flex; justify-content: space-between; padding: 12px; margin: 8px 0; background: #221d19; border-radius: 4px; align-items: center; transition: all 0.3s ease; }}
      .thumbs-down-btn {{ background: none; border: none; font-size: 16px; cursor: pointer; opacity: 0.5; padding: 4px 8px; }}
      .thumbs-down-btn:hover {{ opacity: 1; transform: scale(1.1); }}
    </style>
    </head>
    <body>
      <h1>Your Music Exploration Dashboard</h1>
      <form action="/refresh" method="post"><button type="submit">Refresh Recommendations</button></form>

      <h2>Recommended Tracks</h2>
      <div id="tracks-container">
    """

    for t in recommended_tracks_list:
        track_escaped = html.escape(t['track'])
        artist_escaped = html.escape(t['artist'])
        html_out += f"""
        <div class="track-card">
          <div>
            <strong>{track_escaped}</strong> by {artist_escaped} <br/>
            <small style="color:#a89e8c;">Because you like: {html.escape(t['because_of'])}</small>
          </div>
          <div>
            <a href="{t['spotify_url']}" target="_blank" style="color:#1DB954; margin-right:10px; text-decoration:none;">Spotify</a>
            <button class="thumbs-down-btn" onclick="dislikeTrack('{track_escaped.replace("'", "\\'")}', '{artist_escaped.replace("'", "\\'")}', this)">👎</button>
          </div>
        </div>
        """

    # NOTE: THE RECOMMENDED ARTISTS INJECTION LOOP HAS BEEN TEMPORARILY STRIPPED FROM COMPILING
    html_out += """
      </div>

      <script>
      async function dislikeTrack(track, artist, btn) {
          try {
              const response = await fetch('/api/dislike', {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ track: track, artist: artist })
              });
              const result = await response.json();
              if (result.status === 'success') {
                  const card = btn.closest('.track-card');
                  if (card) {
                      card.style.opacity = '0';
                      setTimeout(() => card.remove(), 300);
