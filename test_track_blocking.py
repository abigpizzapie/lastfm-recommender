import tempfile
import unittest
from pathlib import Path

import lastfm_recommender as lr
import server


class TrackBlockingTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.original_output_html = lr.OUTPUT_HTML
        self.original_blocked_path = lr.BLOCKED_TRACKS_PATH
        lr.OUTPUT_HTML = Path(self.tmpdir.name) / "dashboard.html"
        lr.BLOCKED_TRACKS_PATH = Path(self.tmpdir.name) / "blocked_tracks.json"

    def tearDown(self):
        lr.OUTPUT_HTML = self.original_output_html
        lr.BLOCKED_TRACKS_PATH = self.original_blocked_path
        self.tmpdir.cleanup()

    def test_track_rows_render_list_layout_with_block_button(self):
        payload = {
            "stats": {
                "username": "tester",
                "generated_at": "2026-01-01 00:00",
                "known_artist_count": 1,
                "top_tags": [],
                "top_artists": [],
            },
            "artist_recs": [],
            "track_recs": [{
                "track": "Song A",
                "artist": "Artist A",
                "score": 1.0,
                "because_of": "Seed Song",
                "track_key": "artist a::song a",
                "lastfm_url": "https://last.fm",
                "youtube_url": "https://youtube.com",
                "spotify_url": "https://spotify.com",
            }],
        }

        lr.render_dashboard(payload)
        html = lr.OUTPUT_HTML.read_text(encoding="utf-8")

        self.assertIn('class="track-list"', html)
        self.assertIn('class="track-row" data-track-key="artist a::song a"', html)
        self.assertIn(".track-row {", html)
        self.assertIn('class="btn-block"', html)

    def test_blocked_tracks_persist_to_disk(self):
        blocked = {"artist a::song a", "artist b::song b"}
        lr.save_blocked_tracks(blocked)

        self.assertEqual(lr.load_blocked_tracks(), blocked)

    def test_blocked_tracks_endpoint_returns_saved_tracks(self):
        client = server.app.test_client()
        lr.save_blocked_tracks({"artist a::song a"})

        response = client.get("/blocked-tracks")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"blocked": ["artist a::song a"]})

    def test_preview_button_enabled_when_preview_url_present(self):
        payload = {
            "stats": {
                "username": "tester",
                "generated_at": "2026-01-01 00:00",
                "known_artist_count": 1,
                "top_tags": [],
                "top_artists": [],
            },
            "artist_recs": [],
            "track_recs": [{
                "track": "Song A",
                "artist": "Artist A",
                "score": 1.0,
                "because_of": "Seed Song",
                "track_key": "artist a::song a",
                "lastfm_url": "https://last.fm",
                "youtube_url": "https://youtube.com",
                "spotify_url": "https://spotify.com",
                "preview_url": "https://example.com/preview.m4a",
            }],
        }

        lr.render_dashboard(payload)
        html = lr.OUTPUT_HTML.read_text(encoding="utf-8")

        self.assertIn('class="btn-preview" data-preview-url="https://example.com/preview.m4a"', html)
        self.assertNotIn('class="btn-preview" disabled', html)

    def test_preview_button_disabled_when_preview_url_missing(self):
        payload = {
            "stats": {
                "username": "tester",
                "generated_at": "2026-01-01 00:00",
                "known_artist_count": 1,
                "top_tags": [],
                "top_artists": [],
            },
            "artist_recs": [],
            "track_recs": [{
                "track": "Song A",
                "artist": "Artist A",
                "score": 1.0,
                "because_of": "Seed Song",
                "track_key": "artist a::song a",
                "lastfm_url": "https://last.fm",
                "youtube_url": "https://youtube.com",
                "spotify_url": "https://spotify.com",
                "preview_url": None,
            }],
        }

        lr.render_dashboard(payload)
        html = lr.OUTPUT_HTML.read_text(encoding="utf-8")

        self.assertIn('class="btn-preview" disabled', html)


if __name__ == "__main__":
    unittest.main()
