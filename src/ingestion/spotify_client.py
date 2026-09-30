"""
Spotify Client Credentials integration (issue #11 on the public repo).

Three angles from that issue were picked to build:
  1. Fallback metadata enrichment — when MusicBrainz has nothing for a
     title (small/DIY artists currently needing manual_overrides), try
     Spotify before giving up on it entirely.
  2. Cover art fallback — when Cover Art Archive has no image for a
     release group, fall back to Spotify's album art.
  3. A Spotify album ID to power the public repo's #12 in-app preview
     embed (an <iframe src="https://open.spotify.com/embed/album/{id}">
     needs no API key on the frontend/backend side at all — only the
     ingestion pipeline here needs real credentials).

Also added while dealing with issue #14's tag-guarantee prerequisite:
a genre-tag fallback (Spotify tags *artists*, not albums, with genres) for
albums that end up with zero tags from MusicBrainz — see
get_artist_genres() / apply_genre_tag_fallback() and
backfill_tags.py.

Everything in this module runs at ingestion time and writes its results
into the `albums` table (spotify_album_id, spotify_cover_url,
metadata_source). The public-facing backend never calls Spotify itself —
it only reads whatever this module already stored, which is what keeps
the webapp from needing its own Spotify credentials or a live API call on
the request path.

Env vars (see .env.example): spotify_id = Client ID, spotify_key = Client
Secret. Every function here degrades to returning None on missing
credentials, no match, or any API error — this is an optional enrichment
step, never something the pipeline should crash over.
"""

import base64
import os
import time

import requests

from ingestion.cleaning_methods import clean_and_normalize_tags

_TOKEN_URL = "https://accounts.spotify.com/api/token"
_API_BASE = "https://api.spotify.com/v1"

# Cached in-process so a single pipeline run (hundreds of albums) doesn't
# re-authenticate per album — Client Credentials tokens are valid for the
# `expires_in` Spotify returns (currently 1 hour) and carry no per-user
# state, so a module-level cache is safe here.
_token_cache = {"access_token": None, "expires_at": 0.0}

# Retry-with-backoff for transient Spotify API failures (issues #11/#12
# backfill runs showed a meaningful fraction of "no match" results were
# actually 502 Bad Gateway responses, not a real absence of a match).
_MAX_ATTEMPTS = 3
_RETRY_STATUS_CODES = {502, 503, 504}
_RETRY_BACKOFF_SECONDS = (1.5, 3)  # delay before attempt 2, and before attempt 3


def _request_with_retry(method: str, url: str, **kwargs):
    """requests.request(), but retries on a transient failure: a 502/503/504
    response, or a connection-level error (timeout, reset, DNS blip, etc).

    Non-transient responses (200, 404, 4xx, ...) are returned immediately on
    the first attempt, unchanged — callers keep calling resp.raise_for_status()
    themselves exactly as before. On a connection-level error that exhausts
    every attempt, the underlying exception is re-raised so it still lands in
    each call site's existing `except Exception as e:` logging.
    """
    last_exc = None
    resp = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.exceptions.RequestException as e:
            last_exc = e
            resp = None
        else:
            if resp.status_code not in _RETRY_STATUS_CODES:
                return resp

        if attempt < _MAX_ATTEMPTS - 1:
            time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
            continue

        if resp is not None:
            return resp  # exhausted retries on a 502/503/504 — let raise_for_status() handle it
        raise last_exc


def _get_token():
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 30:
        return _token_cache["access_token"]

    client_id = os.environ.get("spotify_id")
    client_secret = os.environ.get("spotify_key")
    if not client_id or not client_secret:
        return None

    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        resp = requests.post(
            _TOKEN_URL,
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"Warning: Spotify auth failed — {e}")
        return None

    _token_cache["access_token"] = data.get("access_token")
    _token_cache["expires_at"] = now + data.get("expires_in", 3600)
    return _token_cache["access_token"]


def _get_avg_track_length(album_id: str, headers: dict):
    """Minutes, averaged across the album's tracks. Needs its own call —
    the /search endpoint doesn't return per-track durations."""
    try:
        resp = _request_with_retry("GET", f"{_API_BASE}/albums/{album_id}", headers=headers, timeout=10)
        resp.raise_for_status()
        tracks = resp.json().get("tracks", {}).get("items", [])
    except Exception:
        return None

    durations_min = [t["duration_ms"] / 60000 for t in tracks if t.get("duration_ms")]
    return round(sum(durations_min) / len(durations_min), 2) if durations_min else None


def find_spotify_album(title: str, artist: str = ""):
    """Search Spotify for the best-guess album match by title (+ artist,
    when known). Returns None on missing credentials, no match, or any API
    error — callers should treat this purely as an optional fallback.

    Returns:
        {
            "spotify_album_id": str,
            "cover_url": str | None,     # largest available image
            "release_year": int | None,
            "avg_length": float | None,  # minutes
        }
        or None
    """
    token = _get_token()
    if not token:
        return None

    headers = {"Authorization": f"Bearer {token}"}
    query = f"album:{title} artist:{artist}" if artist else f"album:{title}"

    try:
        resp = _request_with_retry(
            "GET",
            f"{_API_BASE}/search",
            headers=headers,
            params={"q": query, "type": "album", "limit": 1},
            timeout=10,
        )
        resp.raise_for_status()
        items = resp.json().get("albums", {}).get("items", [])
    except Exception as e:
        print(f"Warning: Spotify search failed for '{title}' — {e}")
        return None

    if not items:
        return None

    album = items[0]
    album_id = album.get("id")
    if not album_id:
        return None

    images = album.get("images") or []
    cover_url = images[0]["url"] if images else None

    release_date = album.get("release_date") or ""
    release_year = int(release_date[:4]) if release_date[:4].isdigit() else None

    return {
        "spotify_album_id": album_id,
        "cover_url": cover_url,
        "release_year": release_year,
        "avg_length": _get_avg_track_length(album_id, headers),
    }


def get_cover_art_fallback(mbid: str, title: str, artist: str = ""):
    """Probe Cover Art Archive for this release-group mbid; if it doesn't
    have art (404, or any request error), fall back to Spotify's album
    art. Returns a URL to store in albums.spotify_cover_url, or None when
    CAA already works (nothing to store) or Spotify also came up empty."""
    if not mbid:
        return None

    try:
        resp = requests.head(
            f"https://coverartarchive.org/release-group/{mbid}/front-500",
            timeout=8,
            allow_redirects=True,
        )
        if resp.status_code == 200:
            return None  # CAA already has it — no fallback needed
    except Exception:
        pass  # treat a probe failure the same as a 404: try the fallback

    match = find_spotify_album(title, artist)
    return match["cover_url"] if match else None


def get_artist_genres(artists) -> list[str]:
    """Spotify tags *artists*, not albums, with genres — MusicBrainz album
    search gives no equivalent, so this is a separate lookup used as a tag
    fallback when an album ends up with zero tags. Aggregates genres
    across every given artist name, deduped, in first-seen order.

    `artists` may be a list or a comma-separated string. Returns [] on
    missing credentials, no matches, or any API error."""
    token = _get_token()
    if not token:
        return []

    if isinstance(artists, str):
        artists = [a.strip() for a in artists.split(",") if a.strip()]

    headers = {"Authorization": f"Bearer {token}"}
    genres: list[str] = []
    for name in artists:
        try:
            resp = _request_with_retry(
                "GET",
                f"{_API_BASE}/search",
                headers=headers,
                params={"q": f"artist:{name}", "type": "artist", "limit": 1},
                timeout=10,
            )
            resp.raise_for_status()
            items = resp.json().get("artists", {}).get("items", [])
        except Exception as e:
            print(f"Warning: Spotify artist search failed for '{name}' — {e}")
            continue

        if not items:
            continue
        for genre in items[0].get("genres", []):
            if genre not in genres:
                genres.append(genre)

    return genres


def apply_genre_tags(supabase, album_id: str, tags: list[str]) -> bool:
    """Write `tags` (already cleaned/normalized by the caller — see
    cleaning_methods.clean_and_normalize_tags) onto an existing album,
    replacing whatever's there. Shared by album_push_logic.py and
    backfill_tags.py. Returns True if any tags were written."""
    if not tags:
        return False
    supabase.table("album_tags").delete().eq("album_id", album_id).execute()
    for tag_name in tags:
        tag_resp = supabase.table("tags").upsert({"name": tag_name}, on_conflict="name").execute()
        if tag_resp.data:
            tag_db_id = tag_resp.data[0]["id"]
            supabase.table("album_tags").upsert(
                {"album_id": album_id, "tag_id": tag_db_id},
                on_conflict="album_id,tag_id",
            ).execute()
    return True


def apply_spotify_fallback(supabase, title: str, artists, extra_fields: dict | None = None) -> dict:
    """When MusicBrainz has nothing for `title`, try to resolve it via
    Spotify instead and upsert a real `albums` row (metadata_source =
    'spotify'), then try Spotify artist genres as tags. Shared by
    album_push_logic.py (inline, for new albums as they're ingested) and
    backfill_spotify_metadata.py (against the existing failed_lookups
    backlog) so the "resolve + write the row" logic only lives in one
    place.

    `artists` may be a list (preferred) or a comma-separated string.
    `extra_fields` lets a caller merge in things this function doesn't
    know about on its own (e.g. rating, notion_created_at/edited_at).

    Returns {"resolved": bool, "tagged": bool} — resolved is False when
    Spotify had nothing at all (caller should fall back to its normal
    bad-egg recording); tagged is only meaningful when resolved is True.
    """
    if isinstance(artists, list):
        artist_list = artists
        search_artist = ", ".join(artists)
    else:
        search_artist = artists or ""
        artist_list = [a.strip() for a in search_artist.split(",") if a.strip()]

    match = find_spotify_album(title, search_artist)
    if not match:
        return {"resolved": False, "tagged": False}

    album_data = {
        "title": title,
        "mbid": None,
        "release_year": match.get("release_year"),
        "avg_length": match.get("avg_length"),
        "spotify_album_id": match.get("spotify_album_id"),
        "spotify_cover_url": match.get("cover_url"),
        "metadata_source": "spotify",
    }
    if extra_fields:
        album_data.update(extra_fields)

    # mbid is NULL here (Spotify has no MusicBrainz id), same as the
    # manual-override path in album_push_logic.py, so on_conflict="mbid"
    # can't be used — find-or-update by title instead.
    existing_rows = (
        supabase.table("albums").select("id").eq("title", title).execute().data or []
    )
    if existing_rows:
        album_db_id = existing_rows[0]["id"]
        supabase.table("albums").update(album_data).eq("id", album_db_id).execute()
    else:
        created = supabase.table("albums").insert(album_data).execute()
        album_db_id = created.data[0]["id"] if created.data else None

    if not album_db_id:
        return {"resolved": False, "tagged": False}

    # Contributors: name-only, since Spotify's artist objects don't carry
    # a MusicBrainz mbid to link against.
    supabase.table("album_contributions").delete().eq("album_id", album_db_id).eq("role", "artist").execute()
    for name in artist_list:
        existing_artist = supabase.table("artists").select("id").eq("name", name).limit(1).execute()
        if existing_artist.data:
            person_db_id = existing_artist.data[0]["id"]
        else:
            created_artist = supabase.table("artists").insert({"name": name}).execute()
            person_db_id = created_artist.data[0]["id"] if created_artist.data else None

        if person_db_id:
            supabase.table("album_contributions").upsert(
                {"album_id": album_db_id, "person_id": person_db_id, "role": "artist"},
                on_conflict="album_id,person_id,role",
            ).execute()

    # Issue #14's tag-guarantee prerequisite: a Spotify-fallback album has
    # no MusicBrainz tags by definition, so try Spotify's artist genres
    # before leaving it tagless.
    genres = clean_and_normalize_tags(get_artist_genres(artist_list))
    tagged = apply_genre_tags(supabase, album_db_id, genres)

    return {"resolved": True, "tagged": tagged}
