"""
Last.fm fallback for artist genre/mood tags — continuing #14/#24's tag-
guarantee work, and the 2026-11-10 finding that Spotify's artist-genres
field (spotify_client.get_artist_genres) has become widely unreliable
industry-wide: it now frequently comes back empty even for major, well-
known artists (see the 2026-11-10 note in the project's
book-research-proposal doc, and community.spotify.com threads titled
"Genres from artists have been removed"). That's a Spotify-side problem,
not something this pipeline can fix, so this adds a second, independent
source instead of trying to work around it.

Last.fm's artist.getTopTags is community-voted folksonomy tag data, keyed
directly by artist name (no mbid needed, same as get_artist_genres) — the
traditional complement to MusicBrainz's own tag system, and specifically
built for exactly this kind of genre/mood tagging rather than Spotify's
coarser editorial genre list.

Needs a free API key: https://www.last.fm/api/account/create (takes
seconds, no app review). Set `lastfm_key` in .env. Degrades to returning
[] on a missing key, no match, or any API error — same as every other
optional-enrichment function in this pipeline (get_artist_genres,
get_cover_art_fallback, ...), never something a caller needs to guard
against separately.

Call sites (all three of this pipeline's "no tags yet" branches) use
this as the first-choice fallback and fall through to Spotify genres only
if Last.fm also comes up empty:
  - album_push_logic.py: MusicBrainz succeeded but came back with zero
    tags (release-group + artist-tag fallback both empty).
  - album_push_logic.py: full-manual override (skip_musicbrainz) with no
    manual_tags entered.
  - spotify_client.py: apply_spotify_fallback, for albums MusicBrainz
    never found at all.
"""

import os

import requests

_API_BASE = "https://ws.audioscrobbler.com/2.0/"
_MAX_TAGS = 5

# Last.fm's "count" on a top tag is a 0-100 relative-weight score (how
# strongly that tag applies to this artist, not a literal vote tally like
# MusicBrainz's tag-list), so this is a confidence floor to drop near-
# zero/noise tags, not a vote-count threshold.
_MIN_TAG_WEIGHT = 10

_MAX_ATTEMPTS = 3
_RETRY_STATUS_CODES = {502, 503, 504}
_RETRY_BACKOFF_SECONDS = (1.5, 3)


def _request_with_retry(url: str, params: dict):
    """Mirrors spotify_client._request_with_retry — retry on a transient
    502/503/504 or connection-level error, return immediately on anything
    else (including a clean 4xx, which Last.fm uses for "artist not
    found" and should fail fast, not retry)."""
    last_exc = None
    resp = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = requests.get(url, params=params, timeout=10)
        except requests.exceptions.RequestException as e:
            last_exc = e
            resp = None
        else:
            if resp.status_code not in _RETRY_STATUS_CODES:
                return resp

        if attempt < _MAX_ATTEMPTS - 1:
            import time
            time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
            continue

        if resp is not None:
            return resp
        raise last_exc


def get_artist_tags(artists) -> list[str]:
    """Aggregates Last.fm top tags across every given artist name, deduped
    by taking each tag's highest weight across artists, ordered by
    descending weight. `artists` may be a list or a comma-separated
    string (both forms used elsewhere in this pipeline). Returns [] on a
    missing API key, no matches for any artist, or any API error."""
    api_key = os.environ.get("lastfm_key")
    if not api_key:
        return []

    if isinstance(artists, str):
        artists = [a.strip() for a in artists.split(",") if a.strip()]

    scored: dict[str, int] = {}
    for name in artists:
        try:
            resp = _request_with_retry(
                _API_BASE,
                {
                    "method": "artist.gettoptags",
                    "artist": name,
                    "api_key": api_key,
                    "format": "json",
                    "autocorrect": 1,
                },
            )
            resp.raise_for_status()
            tags = resp.json().get("toptags", {}).get("tag", [])
        except Exception as e:
            print(f"Warning: Last.fm tag lookup failed for '{name}' — {e}")
            continue

        # Last.fm returns either a list of tag dicts, a single dict (one
        # tag, not wrapped in a list), or an empty dict/string when an
        # artist has no tags at all — normalize all three.
        if isinstance(tags, dict):
            tags = [tags]
        elif not isinstance(tags, list):
            tags = []

        for tag in tags:
            if not isinstance(tag, dict):
                continue
            tag_name = (tag.get("name") or "").strip().lower()
            try:
                weight = int(tag.get("count", 0))
            except (TypeError, ValueError):
                weight = 0
            if not tag_name or weight < _MIN_TAG_WEIGHT:
                continue
            scored[tag_name] = max(scored.get(tag_name, 0), weight)

    ranked = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)
    return [name for name, _ in ranked[:_MAX_TAGS]]
