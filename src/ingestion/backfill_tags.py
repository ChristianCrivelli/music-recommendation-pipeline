"""
One-off backfill (issue #14's tag-guarantee prerequisite): try Last.fm
artist tags, then Spotify artist genres, for every album that currently
has zero tags. As of 2026-08-29 that was 195 albums; as of 2026-11-10,
156 — a mix of MusicBrainz successes that came back with no tags, manual
overrides that never had a tag fallback attempted at all (fixed
2026-11-10 — see album_push_logic.py), and Spotify-fallback albums whose
artist genres came back empty, which turns out to be a widely-reported
Spotify API reliability issue, not something unique to this library (see
lastfm_client.py and the 2026-11-10 note in the book-research-proposal
project doc). The ongoing pipeline (album_push_logic.py /
spotify_client.apply_spotify_fallback) now tries both fallbacks
automatically for new albums going forward; this script is just to clear
the existing gap in one pass, which is what #14's "every card shows 1-3
tags" needs before it can ship without surfacing a wall of blank tag rows.

Run locally once, after adding real spotify_id/spotify_key (and, for the
Last.fm fallback, lastfm_key — https://www.last.fm/api/account/create)
to your .env:
    python -m src.ingestion.backfill_tags

Safe to re-run — anything both sources still have no tags for is left
untagged, unchanged, for a manual tag pass later.
"""

import os
import sys

from dotenv import load_dotenv
from supabase import create_client, Client

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ingestion.spotify_client import get_artist_genres, apply_genre_tags
from ingestion.lastfm_client import get_artist_tags as get_lastfm_tags
from ingestion.cleaning_methods import clean_and_normalize_tags
from ingestion.bad_eggs import record_bad_egg as _record_bad_egg, clear_bad_eggs as _clear_bad_eggs

load_dotenv()

url = os.environ.get("supabase_url")
key = os.environ.get("supabase_key")
supabase: Client = create_client(url, key)


def _untagged_albums() -> list[dict]:
    """Every album with zero rows in album_tags. PostgREST's .not_.in_()
    against a subquery isn't available over the REST client, so this pulls
    every album_id that DOES have a tag and diffs it against all albums —
    same trick as the pagination pattern elsewhere in this pipeline, just
    two full-table pulls instead of one, which is fine at this table size."""
    PAGE_SIZE = 1000

    def _paginate(table, select):
        rows, start = [], 0
        while True:
            resp = supabase.table(table).select(select).range(start, start + PAGE_SIZE - 1).execute()
            batch = resp.data or []
            rows.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            start += PAGE_SIZE
        return rows

    all_albums = _paginate("albums", "id, title")
    tagged_ids = {row["album_id"] for row in _paginate("album_tags", "album_id")}
    return [a for a in all_albums if a["id"] not in tagged_ids]


def _artist_names(album_id: str) -> list[str]:
    resp = (
        supabase.table("album_contributions")
        .select("artists(name)")
        .eq("album_id", album_id)
        .eq("role", "artist")
        .execute()
    )
    return [
        c["artists"]["name"]
        for c in (resp.data or [])
        if c.get("artists") and c["artists"].get("name")
    ]


def main():
    untagged = _untagged_albums()
    print(f"{len(untagged)} album(s) with zero tags — trying Last.fm, then Spotify artist genres, for each.")

    tagged, still_untagged = 0, 0
    for album in untagged:
        artists = _artist_names(album["id"])
        if not artists:
            print(f"  skip (no linked artist): {album['title']}")
            still_untagged += 1
            continue

        try:
            genres = clean_and_normalize_tags(get_lastfm_tags(artists) or get_artist_genres(artists))
        except Exception as e:
            print(f"  error on {album['title']}: {e}")
            genres = []

        if genres and apply_genre_tags(supabase, album["id"], genres):
            _clear_bad_eggs(supabase, album["title"])
            print(f"  tagged: {album['title']} -> {genres}")
            tagged += 1
        else:
            _record_bad_egg(
                supabase,
                album["title"],
                ", ".join(artists),
                "No tags found from MusicBrainz, Last.fm artist tags, or Spotify artist genres — needs a manual tag override.",
            )
            print(f"  still untagged: {album['title']}")
            still_untagged += 1

    print(f"\nDone. {tagged} album(s) tagged via Last.fm/Spotify, {still_untagged} still need a manual look.")


if __name__ == "__main__":
    main()
