"""
One-off backfill (issues #11/#12): every album that matched fine on
MusicBrainz has never had a Spotify search run against it at all, so
spotify_album_id is NULL for all 1,093 of them (confirmed 2026-09-24) even
though they have real cover art and metadata. The #12 in-app preview embed
needs spotify_album_id specifically, so this is why previews only work for
~2% of the catalog - not a bug in the embed itself, just a backfill gap
that predates issue #11/#12 entirely (those only wired up Spotify for
albums MusicBrainz *couldn't* match - see backfill_spotify_metadata.py -
never for albums it matched fine).

Only writes spotify_album_id. Deliberately leaves spotify_cover_url,
release_year, avg_length, and metadata_source alone here - these albums
already have real MusicBrainz-sourced data for all of those, and
backfill_cover_art.py already owns the cover-art-fallback decision
(CAA-first, Spotify only on a 404). Scoping this script to one column
keeps it a pure additive fill with nothing to accidentally clobber.

Run locally once, after adding real spotify_id/spotify_key to your .env:
python -m src.ingestion.backfill_spotify_album_id

Safe to re-run - only rows with spotify_album_id still NULL are touched,
so anything already resolved (by this script, the live pipeline, or the
Spotify-fallback backfill) is skipped.
"""

import os
import sys

from dotenv import load_dotenv
from supabase import create_client, Client

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ingestion.spotify_client import find_spotify_album

load_dotenv()

url = os.environ.get("supabase_url")
key = os.environ.get("supabase_key")
supabase: Client = create_client(url, key)


def _artist_names(album_id: str) -> str:
    # Same join as backfill_cover_art.py's _artist_names().
    resp = (
        supabase.table("album_contributions")
        .select("artists(name)")
        .eq("album_id", album_id)
        .eq("role", "artist")
        .execute()
    )
    return ", ".join(
        c["artists"]["name"]
        for c in (resp.data or [])
        if c.get("artists") and c["artists"].get("name")
    )


def main():
    # Same pagination pattern as backfill_cover_art.py / get_existing_album_ids()
    # - PostgREST caps a single .select() at 1000 rows.
    PAGE_SIZE = 1000
    rows, start = [], 0
    while True:
        resp = (
            supabase.table("albums")
            .select("id, title")
            .not_.is_("mbid", "null")
            .is_("spotify_album_id", "null")
            .range(start, start + PAGE_SIZE - 1)
            .execute()
        )
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE

    print(f"Searching Spotify for {len(rows)} MusicBrainz-matched album(s) missing spotify_album_id.")

    resolved, not_found = 0, 0
    for row in rows:
        artists = _artist_names(row["id"])
        try:
            match = find_spotify_album(row["title"], artists)
        except Exception as e:
            print(f"  error on {row['title']}: {e}")
            match = None

        if match and match.get("spotify_album_id"):
            supabase.table("albums").update(
                {"spotify_album_id": match["spotify_album_id"]}
            ).eq("id", row["id"]).execute()
            print(f"  resolved: {row['title']}")
            resolved += 1
        else:
            print(f"  no Spotify match: {row['title']}")
            not_found += 1

    print(f"\nDone. {resolved}/{len(rows)} album(s) got a spotify_album_id, {not_found} had no Spotify match.")


if __name__ == "__main__":
    main()
