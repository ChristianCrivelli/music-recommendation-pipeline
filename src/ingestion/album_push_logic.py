import os
import sys
import pandas as pd
from dotenv import load_dotenv
from supabase import create_client, Client

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ingestion.pull_albums import fetch_notion_dataframe
from ingestion.album_finder import get_metadata, _artist_name_set, _normalize_artist
from ingestion.cleaning_methods import clean_artist_list, clean_and_normalize_tags
from ingestion.bad_eggs import record_bad_egg as _record_bad_egg, clear_bad_eggs as _clear_bad_eggs
from ingestion.manual_overrides import get_override
from ingestion.spotify_client import apply_spotify_fallback, get_cover_art_fallback, get_artist_genres


load_dotenv()

url = os.environ.get("supabase_url")
key = os.environ.get("supabase_key")

supabase: Client = create_client(url, key)

# FULL_SYNC=true forces a ground-up rebuild (used by the monthly deep sync).
# Otherwise this is a delta pull: only new albums hit MusicBrainz/Spotify.
FULL_SYNC = os.getenv("FULL_SYNC", "false").strip().lower() == "true"

# Optional sharding — see the comment where these are applied to `df` below
# for why this exists (issue #13: FULL_SYNC runs weren't finishing within
# GitHub Actions' 6h job cap).
SHARD_COUNT = int(os.getenv("SHARD_COUNT", "1") or "1")
SHARD_INDEX = int(os.getenv("SHARD_INDEX", "0") or "0")


def get_existing_album_ids() -> dict:
    """Maps lower-cased title -> {id, title} for every album already in
    Supabase. Used to batch the nightly rating/timestamp refresh via a single
    upsert keyed on id (the primary key, so no new unique constraint is
    needed) instead of one UPDATE ... WHERE title = ... round-trip per row.
    The DB's own title is carried along so the refresh upsert can round-trip
    it back untouched — see the comment on refresh_records below for why
    that's necessary even though this refresh never intends to change title.

    PAGINATED: PostgREST (Supabase's REST layer) silently caps a single
    .select() at 1000 rows — no error, no truncation flag, it just stops.
    This table crossed 1000 rows at some point after this function was
    first written, and every run since then was quietly missing the tail
    end of it. Whichever albums fell outside the first 1000 looked "new" to
    the is_new check below, got needlessly re-sent through get_metadata()
    every single night (wasted MusicBrainz calls, longer runtime), and —
    now that matching is exact-only (see album_finder.py) — sometimes
    failed to re-match under the stricter rules and landed in
    failed_lookups looking like a real data problem, even though the
    existing row was already correct (2026-08-22: this is what "Section
    80", "Who Cares", "Sugar Papi", "After da Boat", and a few others
    turned out to be — not bad Notion data, just this function forgetting
    they existed). Paginating in PAGE_SIZE chunks via .range() — the same
    fix pull_albums.py already applies to the Notion side — makes sure
    every row is actually seen, however many there now are."""
    PAGE_SIZE = 1000
    all_rows = []
    start = 0
    while True:
        resp = supabase.table("albums").select("id, title").range(start, start + PAGE_SIZE - 1).execute()
        batch = resp.data or []
        all_rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    # NOTE (issue #14): this dict is keyed by lower-cased title, which
    # assumed titles were unique — true when this was written, no longer
    # guaranteed now that albums.title's UNIQUE constraint has been dropped
    # (two different real albums can share a title). If that ever actually
    # happens, whichever row comes last in `all_rows` silently wins the dict
    # slot and the other same-titled album would incorrectly be treated as
    # "already synced" by the is_new check below, or have its rating refresh
    # applied to the wrong id. Not fixed here — there are zero title
    # collisions in the data as of 2026-08-25, and properly disambiguating
    # would mean joining album_contributions per title here too, which is
    # more than this function's job (fast existence/refresh lookup) should
    # take on speculatively. Worth a look if a real collision ever shows up
    # in failed_lookups or a bad rating-refresh.
    return {
        row["title"].strip().lower(): {"id": row["id"], "title": row["title"]}
        for row in all_rows
        if row.get("title") and row.get("id")
    }


def find_existing_album_id(title: str, artist_names) -> str | None:
    """Look for an existing `albums` row with this exact title
    (case/whitespace-insensitive) and at least one overlapping artist-role
    contributor, regardless of what mbid that row currently has.

    Fix for issue #22: the main enrichment path used to upsert album_data
    purely on on_conflict="mbid", trusting that the release-group id
    get_metadata() picked would be stable across runs. It usually is, but
    when MusicBrainz has more than one release-group that exactly matches a
    title+artist (see the tie-break comment in album_finder.py), a flip to
    a different-but-still-valid mbid meant the very same real album got
    inserted as a brand new row instead of updating the existing one — 762
    albums (~39% of the catalog) ended up duplicated this way. Checking by
    title+artist first — the identity a person actually means by "this
    album" — catches that even when the mbid changes, and also guards
    against MusicBrainz itself later merging/splitting release-groups,
    which a deterministic tie-break alone can't prevent.

    `artist_names` accepts either a comma-joined string (e.g. this script's
    `search_artist`) or a list of names — both are normalized the same way
    album_finder.py's exact-match logic already does, so "existing album"
    here means the same thing get_metadata() means by "correct match".

    Returns the existing album's id, or None if no title+artist match is
    found (a genuinely new album, or an existing title that belongs to a
    different artist — see issue #14, two different real albums can share
    an exact title)."""
    candidates = (
        supabase.table("albums")
        .select("id")
        .ilike("title", title.strip())
        .execute()
        .data
        or []
    )
    if not candidates:
        return None

    artist_str = ", ".join(artist_names) if not isinstance(artist_names, str) else artist_names
    wanted_names = _artist_name_set(artist_str)
    if not wanted_names:
        return None

    for candidate in candidates:
        linked = (
            supabase.table("album_contributions")
            .select("artists(name)")
            .eq("album_id", candidate["id"])
            .eq("role", "artist")
            .execute()
        )
        linked_names = {
            _normalize_artist(c["artists"]["name"])
            for c in (linked.data or [])
            if c.get("artists") and c["artists"].get("name")
        }
        if linked_names & wanted_names:
            return candidate["id"]

    return None


def _chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# Thin wrappers binding the shared helpers to this script's supabase client,
# so the call sites below don't change.
def record_bad_egg(title: str, artists: str, reason: str) -> None:
    _record_bad_egg(supabase, title, artists, reason)


def clear_bad_eggs(title: str) -> None:
    _clear_bad_eggs(supabase, title)


# === Get the Data From Notion ===
df = fetch_notion_dataframe()
df = df[['Title', 'Artist(s)', 'Rating/10', 'NotionCreatedAt', 'NotionEditedAt']]
df = df.rename(columns={'Artist(s)': 'Artists', 'Rating/10': 'Rating'})

# Notion rows with a blank Title can't be searched, matched, or stored
# meaningfully (title is the primary key used throughout this pipeline) —
# drop them up front. Without this, a NaN title survives astype(str) (pandas
# deliberately does NOT stringify NaN — it's preserved as a float, not
# coerced to the string "nan") and crashes downstream string operations,
# taking the entire batch down with it rather than just that one row.
missing_title = df['Title'].isna()
if missing_title.any():
    print(f"Warning: {int(missing_title.sum())} row(s) in Notion have a blank Title — skipping. Check your Notion database for empty rows.")
    df = df[~missing_title].copy()

# Strip stray whitespace from titles at ingestion time. Untrimmed titles used
# to slip into Supabase, and since /api/recommend strips the incoming search
# query but compares it against the stored title, a title like "Temporary "
# would show up fine in autocomplete but never match on search.
df['Title'] = df['Title'].astype(str).str.strip()

# Clean the entire Artists column before looping
df['Artists'] = df['Artists'].apply(clean_artist_list)

print(f"Loaded {len(df)} albums from Notion.")

# === Delta pull: skip MusicBrainz/Spotify calls for albums we already have ===
if FULL_SYNC:
    print("FULL_SYNC enabled — rebuilding metadata for every album.")
else:
    existing_album_ids = get_existing_album_ids()
    is_new = ~df['Title'].str.strip().str.lower().isin(existing_album_ids.keys())

    # Existing albums skip enrichment entirely, but keep their rating and
    # Notion timestamps fresh — rating and last_edited_time are the two
    # things that change in Notion after the fact. Sending notion_created_at
    # here too (harmless — it never actually changes) is what backfills it
    # onto any album row that existed before this feature, automatically,
    # on the very next run — no separate migration/backfill script needed.
    #
    # This used to be one supabase.table("albums").update(...).eq("title", ...)
    # call per already-synced row — 1,000+ sequential network round-trips a
    # night with ~1,174 albums, and the main reason the pipeline took
    # 35-65 minutes even with zero/few new albums. Batched into a single
    # upsert keyed on id (the primary key, so it works without a new unique
    # constraint on title) instead, chunked at 500 rows/request to stay
    # under any request payload limit.
    #
    # NOTE: even though every row here is really just an update to an
    # existing id, Postgres/PostgREST still validate NOT NULL constraints
    # (like albums.title) on the row as constructed for the INSERT branch of
    # "INSERT ... ON CONFLICT DO UPDATE" *before* the conflict is resolved —
    # so leaving a NOT NULL column out of the payload blows up the whole
    # batch, even though the update itself never touches that column. This
    # crashed every run on 2026-08-19 (see issue #11) because "title" wasn't
    # included here. Round-tripping the album's own current title back
    # through the payload satisfies that check without actually changing
    # anything — title is intentionally left untouched by this refresh.
    refresh_records = [
        {
            "id": existing_album_ids[row.Title.strip().lower()]["id"],
            "title": existing_album_ids[row.Title.strip().lower()]["title"],
            "rating": row.Rating,
            "notion_created_at": row.NotionCreatedAt,
            "notion_edited_at": row.NotionEditedAt,
        }
        for row in df[~is_new].itertuples()
    ]

    refresh_failures = 0
    for chunk in _chunked(refresh_records, 500):
        try:
            supabase.table("albums").upsert(chunk, on_conflict="id").execute()
        except Exception as e:
            # A bad batch here used to take the entire run down before it
            # ever reached a single album's enrichment (the 2026-08-19
            # outage: the whole night's ingestion — every new album — was
            # skipped because this upsert crashed in the first 35 seconds).
            # Log it and keep going instead: a failed rating/timestamp
            # refresh for some already-synced albums is far cheaper than
            # losing an entire night's worth of new-album ingestion.
            refresh_failures += 1
            print(f"Warning: rating/timestamp refresh batch failed ({len(chunk)} album(s)): {e}")

    skipped = len(refresh_records)
    df = df[is_new]
    refresh_note = f", {refresh_failures} refresh batch(es) failed — see warnings above" if refresh_failures else ""
    print(f"Delta pull: {len(df)} new album(s) to enrich, {skipped} already synced (rating refreshed in {max(1, -(-skipped // 500)) if skipped else 0} batch(es){refresh_note}).")

# === Optional sharding: split this run's enrichment across parallel jobs ===
# A FULL_SYNC re-enriches every album against MusicBrainz — 3+ calls per
# album at ~1.1s each — which has consistently taken 4-6+ hours against
# GitHub Actions' hard 6h per-job cap, and the 2026-08-21 run got killed by
# it before finishing (issue #13). Rather than build real cross-run
# resumability, deep_sync.yml can run several shards of this same job in
# parallel (a build matrix): each shard gets a disjoint slice of the album
# list via SHARD_INDEX/SHARD_COUNT, so wall-clock time drops roughly in
# proportion to the shard count. Different GitHub-hosted runners get
# different IPs, so MusicBrainz's 1 req/sec-per-IP limit is respected
# independently by each shard without any cross-shard coordination needed.
# Sorting by title before slicing keeps a given shard's assignment stable
# from run to run even if Notion's row order changes. No-op when
# SHARD_COUNT=1 (the default — every other caller of this script).
if SHARD_COUNT > 1:
    if not (0 <= SHARD_INDEX < SHARD_COUNT):
        raise ValueError(f"SHARD_INDEX ({SHARD_INDEX}) must be in [0, {SHARD_COUNT})")
    df = df.sort_values("Title", kind="stable").reset_index(drop=True)
    df = df.iloc[SHARD_INDEX::SHARD_COUNT]
    print(f"Shard {SHARD_INDEX}/{SHARD_COUNT}: {len(df)} album(s) assigned to this job.")

# Tip 3: track failures so we can save them at the end
failures = []

# === Loop through the table from Notion to enrich it with Musicbrainz Data ===
n = 1
for row in df.itertuples():
    print(f"--- Processing N.{n}: {row.Title} ---")
    n += 1
    search_artist = ""  # available in the except block below even if we fail before it's set

    try:
        # 1. Get metadata
        search_artist = ", ".join(row.Artists)

        # 0. Check for a manual override before touching MusicBrainz at all —
        # see config/manual_override_examples.sql for the two modes.
        # Computed above search_artist's normal spot because get_override()
        # now needs it: title alone can no longer be trusted as a unique
        # lookup key (issue #14 — two different real albums can share an
        # exact title), so it's passed through to disambiguate the rare case
        # where more than one override row shares this title.
        override = get_override(supabase, row.Title, search_artist)

        if override and override.get("skip_musicbrainz"):
            # Full-manual mode: never call MusicBrainz for this title.
            meta, mb_reason = None, None
        else:
            # Search-hint mode (or no override at all): use the corrected
            # title/artist if one was provided, otherwise the Notion values.
            search_title = (override.get("search_title") if override else None) or row.Title
            search_artist_query = (override.get("search_artist") if override else None) or search_artist
            meta, mb_reason = get_metadata(search_title, search_artist_query)

        # 2. Insert/Get Artist ID
        if meta and meta.get('Release ID'):
            release_id = meta['Release ID']

            # Issue #11: probe Cover Art Archive for this release group and
            # only hit Spotify (one extra search call) when CAA doesn't
            # have art — most albums already work fine via CAA, so this
            # keeps the common case cheap.
            cover_fallback = get_cover_art_fallback(
                meta.get('Release Group ID'), row.Title, search_artist
            )

            # Entity A (Album)
            album_data = {
                "title": row.Title,
                # Release-GROUP id, not release id — see the comment on
                # "Release Group ID" in album_finder.py for why this
                # distinction matters (it's what keeps this upsert stable
                # across repeated runs of the same album).
                "mbid": meta.get('Release Group ID'),
                "rating": row.Rating,
                "primary_type": meta.get('Primary Type'),
                "release_year": meta.get('Release Year'),
                "avg_length": meta.get('Avg Track Length (Mins)'),
                "spotify_cover_url": cover_fallback,
                "metadata_source": "musicbrainz",
                "notion_created_at": row.NotionCreatedAt,
                "notion_edited_at": row.NotionEditedAt,
            }

            # Entities B & C (Contributors)
            # get_metadata() now pulls producer credits from the same
            # release fetch it already makes for tracks/labels (its
            # release_data includes "artist-rels" too) instead of this
            # code making a second, separate get_release_by_id call for the
            # exact same release_id — see the comment on that fetch in
            # album_finder.py and issue #13.
            producers = meta.get('Producers', [])
            contributors = []

            for artist in meta.get('Artists', []):
                contributors.append({
                    'name': artist['name'],
                    'mbid': artist['mbid'],
                    'role': 'artist'
                })

            for prod in producers:
                contributors.append({
                    'name': prod['name'],
                    'mbid': prod['mbid'],
                    'role': 'producer'
                })

            # Push into Supabase
            # Upsert Album — keyed on mbid, which now stores the stable
            # release-GROUP id (see the comment on "Release Group ID" in
            # album_finder.py) rather than a specific release id. A
            # release-group id is MusicBrainz's canonical identifier for
            # one particular album by one particular artist, so this both
            # (a) survives repeated runs without spawning duplicate rows,
            # and (b) correctly keeps two different artists' same-titled
            # albums as separate rows, since their release-group ids
            # differ. This used to fail on (b) — and on any upsert where
            # MusicBrainz's search landed on a different-but-still-valid
            # mbid for an existing title — because albums.title also had
            # its own UNIQUE constraint, so a new mbid with an already-used
            # title tripped that instead of just inserting cleanly (issue
            # #14, the FULL_SYNC crash from 2026-08-21). That constraint has
            # been dropped — see config/queries/
            # title_uniqueness_migration.sql — so mbid is now the only
            # uniqueness this upsert has to satisfy.
            #
            # But "one row per mbid" and "one row per real album" turned out
            # not to be the same thing (issue #22): get_metadata() can
            # legitimately resolve the same title+artist to a different
            # release-group mbid on a later run (see the tie-break comment
            # in album_finder.py), and on_conflict="mbid" alone can't catch
            # that — it just inserts a second row for the same album. Check
            # by title+artist FIRST, which is what "one album" actually
            # means here; only fall through to the mbid-keyed upsert (which
            # still correctly handles two different artists sharing a title
            # — issue #14 — since neither of them will title+artist-match
            # the other) when no existing row claims this title+artist.
            existing_album_id = find_existing_album_id(row.Title, search_artist)

            if existing_album_id:
                supabase.table("albums").update(album_data).eq("id", existing_album_id).execute()
                album_db_id = existing_album_id
            else:
                album_resp = supabase.table("albums").upsert(
                    album_data,
                    on_conflict="mbid"
                ).execute()

                # Ensure we got data back before extracting the UUID
                if not album_resp.data:
                    print(f"Warning: No data returned from Supabase for {row.Title}. Check RLS policies.")
                    reason = "Supabase returned no data"
                    failures.append({"title": row.Title, "artists": search_artist, "reason": reason})
                    record_bad_egg(title=row.Title, artists=search_artist, reason=reason)
                    continue

                album_db_id = album_resp.data[0]['id']

            # This title now has a real album row, so any bad-egg flags left
            # over from a previous run (e.g. an old "MusicBrainz lookup
            # failed") are stale. Clear them first — anything still wrong
            # this run (e.g. a missing-mbid contributor below) gets re-flagged.
            clear_bad_eggs(row.Title)

            # Loop through all contributors
            for person in contributors:
                if person.get('mbid'):
                    person_resp = supabase.table("artists").upsert(
                        {"name": person['name'], "mbid": person['mbid']},
                        on_conflict="mbid"
                    ).execute()
                    person_db_id = person_resp.data[0]['id'] if person_resp.data else None
                else:
                    # No mbid from MusicBrainz for this contributor. Previously this
                    # branch just skipped the person entirely, which is why some
                    # albums end up showing "Unknown artist" downstream: no row in
                    # artists, no link in album_contributions, nothing for the API
                    # to display. Instead, link by name (find-or-create) so the
                    # album still has a real artist attached, and flag it as a bad
                    # egg since a name-only match could collide with a different
                    # artist of the same name and deserves a manual look.
                    existing = (
                        supabase.table("artists")
                        .select("id")
                        .eq("name", person['name'])
                        .limit(1)
                        .execute()
                    )
                    if existing.data:
                        person_db_id = existing.data[0]['id']
                    else:
                        created = supabase.table("artists").insert(
                            {"name": person['name']}
                        ).execute()
                        person_db_id = created.data[0]['id'] if created.data else None

                    record_bad_egg(
                        title=row.Title,
                        artists=search_artist,
                        reason=f"No MusicBrainz mbid for contributor '{person['name']}' ({person['role']}) — linked by name only, verify for duplicate artist rows.",
                    )

                if person_db_id:
                    # Step 3: Create the link in the Junction Table
                    link_data = {
                        "album_id": album_db_id,
                        "person_id": person_db_id,
                        "role": person['role']
                    }

                    supabase.table("album_contributions").upsert(
                        link_data,
                        on_conflict="album_id,person_id,role"
                    ).execute()

            # Loop through all tags
            raw_tags = meta.get('Top Tags', [])

            top_tags = clean_and_normalize_tags(raw_tags)

            # Issue #14's tag-guarantee prerequisite: MusicBrainz sometimes
            # has a release-group match but no tags on it at all. Before
            # falling back to a bad egg, try Spotify's artist genres —
            # cheap (one extra call only when MusicBrainz came up empty)
            # and closes most of what used to be a permanent gap.
            if not top_tags:
                top_tags = clean_and_normalize_tags(get_artist_genres(row.Artists))

            if top_tags:
                # Substitute, don't just accumulate: if this album already has
                # tags (e.g. a manually-added placeholder from a period when
                # MusicBrainz had nothing, or stale tags from a prior wrong-
                # artist match), a fresh non-empty result from MusicBrainz is
                # taken as authoritative and replaces them outright. Without
                # this, placeholder/stale tags linger forever — upsert only
                # ever adds new album_tags links, it never removes old ones,
                # so a real tag arriving later just sits next to a leftover
                # placeholder instead of superseding it.
                supabase.table("album_tags").delete().eq("album_id", album_db_id).execute()

                for tag_name in top_tags:
                    # Upsert the tag into a 'tags' table
                    tag_resp = supabase.table("tags").upsert(
                        {"name": tag_name},
                        on_conflict="name" # Assumes 'name' is unique in your tags table
                    ).execute()

                    if tag_resp.data:
                        tag_db_id = tag_resp.data[0]['id']

                        # Create the link in the Tag Junction Table
                        tag_link_data = {
                            "album_id": album_db_id,
                            "tag_id": tag_db_id
                        }

                        supabase.table("album_tags").upsert(
                            tag_link_data,
                            on_conflict="album_id,tag_id"
                        ).execute()
            else:
                # No tags found this run — leave whatever's already linked
                # (placeholder or otherwise) alone rather than wiping it out
                # over a transient MusicBrainz gap. Just flag it so it stays
                # visible as still needing attention.
                record_bad_egg(
                    title=row.Title,
                    artists=search_artist,
                    reason="No tags found from MusicBrainz release-group/artist, or Spotify artist genres — needs a manual tag override.",
                )

            print(f"Success! Added {len(contributors)} contributors and {len(top_tags)} tags.")

        elif override and override.get("skip_musicbrainz"):
            # Full-manual path: no MusicBrainz data exists or will ever be
            # sought for this title. Store whatever Chris entered in
            # manual_overrides directly, with mbid left NULL.
            print(f"Using manual override for {row.Title} (skipping MusicBrainz).")

            album_data = {
                "title": row.Title,
                "mbid": None,
                "rating": row.Rating,
                "primary_type": override.get("manual_primary_type"),
                "release_year": override.get("manual_release_year"),
                "avg_length": override.get("manual_avg_length"),
                "metadata_source": "manual",
                "notion_created_at": row.NotionCreatedAt,
                "notion_edited_at": row.NotionEditedAt,
            }

            # manual_artist_names is needed before the lookup below now (see
            # that comment), not just for the contributors loop further down.
            manual_artist_names = override.get("manual_artist_names") or row.Artists

            # A NULL mbid never "conflicts" with anything under Postgres
            # uniqueness rules, so on_conflict="mbid" would insert a fresh
            # duplicate album row on every single re-run. Find-or-update by
            # title instead. Title alone used to be this pipeline's de facto
            # unique key for any album without an mbid, but the
            # albums.title unique constraint is gone now (issue #14 — two
            # different real albums can share an exact title), so more than
            # one row can come back here. Disambiguate by the manually-
            # overridden artist(s) when that happens; fall back to the first
            # row (old behavior) if nothing matches rather than dropping the
            # album.
            existing_rows = (supabase.table("albums").select("id").eq("title", row.Title).execute().data or [])
            album_db_id = None
            if len(existing_rows) == 1:
                album_db_id = existing_rows[0]['id']
            elif len(existing_rows) > 1:
                wanted_names = {n.strip().lower() for n in manual_artist_names}
                for candidate in existing_rows:
                    linked = (
                        supabase.table("album_contributions")
                        .select("artists(name)")
                        .eq("album_id", candidate['id'])
                        .eq("role", "artist")
                        .execute()
                    )
                    linked_names = {
                        c["artists"]["name"].strip().lower()
                        for c in (linked.data or [])
                        if c.get("artists") and c["artists"].get("name")
                    }
                    if linked_names & wanted_names:
                        album_db_id = candidate['id']
                        break
                if album_db_id is None:
                    print(
                        f"Warning: {len(existing_rows)} existing albums share title "
                        f"'{row.Title}' and none matched artist(s) {sorted(wanted_names)} — "
                        f"using the first row."
                    )
                    album_db_id = existing_rows[0]['id']

            if album_db_id:
                supabase.table("albums").update(album_data).eq("id", album_db_id).execute()
            else:
                created_album = supabase.table("albums").insert(album_data).execute()
                if not created_album.data:
                    print(f"Warning: No data returned from Supabase for {row.Title}. Check RLS policies.")
                    reason = "Supabase returned no data (manual override insert)"
                    failures.append({"title": row.Title, "artists": search_artist, "reason": reason})
                    record_bad_egg(title=row.Title, artists=search_artist, reason=reason)
                    continue
                album_db_id = created_album.data[0]['id']

            clear_bad_eggs(row.Title)

            # Contributors: name-only, since no MBIDs exist for these by
            # definition. Wipe old artist links first so re-running replaces
            # rather than accumulates duplicates if manual_artist_names changes.
            supabase.table("album_contributions").delete().eq("album_id", album_db_id).eq("role", "artist").execute()
            for name in manual_artist_names:
                existing_artist = supabase.table("artists").select("id").eq("name", name).limit(1).execute()
                if existing_artist.data:
                    person_db_id = existing_artist.data[0]['id']
                else:
                    created_artist = supabase.table("artists").insert({"name": name}).execute()
                    person_db_id = created_artist.data[0]['id'] if created_artist.data else None

                if person_db_id:
                    supabase.table("album_contributions").upsert(
                        {"album_id": album_db_id, "person_id": person_db_id, "role": "artist"},
                        on_conflict="album_id,person_id,role"
                    ).execute()

            # Tags: wipe and re-add from manual_tags for the same reason.
            supabase.table("album_tags").delete().eq("album_id", album_db_id).execute()
            top_tags = clean_and_normalize_tags(override.get("manual_tags") or [])
            for tag_name in top_tags:
                tag_resp = supabase.table("tags").upsert({"name": tag_name}, on_conflict="name").execute()
                if tag_resp.data:
                    tag_db_id = tag_resp.data[0]['id']
                    supabase.table("album_tags").upsert(
                        {"album_id": album_db_id, "tag_id": tag_db_id},
                        on_conflict="album_id,tag_id"
                    ).execute()

            print(f"Success (manual)! Added {len(manual_artist_names)} contributor(s) and {len(top_tags)} tag(s).")

        else:
            print(f"Could not find MusicBrainz data for {row.Title}")

            # Issue #11: before giving up entirely, see if Spotify has this
            # one — mostly catches small/DIY releases MusicBrainz doesn't
            # index. Issue #14: apply_spotify_fallback also tries Spotify
            # artist genres as tags, since a Spotify-fallback album has no
            # MusicBrainz tags by definition.
            spotify_result = apply_spotify_fallback(
                supabase,
                row.Title,
                row.Artists,
                extra_fields={
                    "rating": row.Rating,
                    "notion_created_at": row.NotionCreatedAt,
                    "notion_edited_at": row.NotionEditedAt,
                },
            )
            if spotify_result["resolved"]:
                clear_bad_eggs(row.Title)
                if not spotify_result["tagged"]:
                    record_bad_egg(
                        title=row.Title,
                        artists=search_artist,
                        reason="Resolved via Spotify fallback (no MusicBrainz match) — no tags available (Spotify had no artist genres either), needs a manual tag override if desired.",
                    )
                print(f"Success (Spotify fallback)! {row.Title} resolved via Spotify"
                      f"{' with tags' if spotify_result['tagged'] else ' — still untagged'}.")
            else:
                # Tip 3: record the failure with a reason so it's easy to investigate.
                # mb_reason now distinguishes "no candidates" / "low-confidence match"
                # / "API error after retries" instead of one generic string, so it's
                # obvious from failed_lookups.csv alone whether an album needs a
                # title fix, a manual MBID override, or is just worth re-running.
                reason = mb_reason or "MusicBrainz lookup failed"
                failures.append({"title": row.Title, "artists": search_artist, "reason": reason})
                record_bad_egg(title=row.Title, artists=search_artist, reason=reason)

    except Exception as e:
        # Safety net: nothing that happens while processing a single album —
        # a MusicBrainz quirk, a malformed Notion row, a Supabase hiccup,
        # anything unanticipated — should be able to crash the whole run and
        # silently skip every remaining album in the batch (this is exactly
        # what happened with a blank-title row: it took down all 199 new
        # albums in that run, not just itself). Log it, flag it, move on.
        print(f"Unexpected error processing {row.Title}: {e}")
        reason = f"Unexpected error: {e}"
        failures.append({"title": row.Title, "artists": search_artist, "reason": reason})
        record_bad_egg(title=str(row.Title), artists=str(search_artist), reason=reason)
        continue

# Tip 3: the CSV is kept as a local run artifact for convenience, but it lives
# on the GitHub Actions runner's disk and is destroyed when the job ends —
# it's not actually a durable record. The Supabase failed_lookups table
# (populated via record_bad_egg above, as failures happen) is now the source
# of truth Chris can check any time: `SELECT * FROM open_bad_eggs;`
if failures:
    failures_df = pd.DataFrame(failures)
    failures_df.to_csv("failed_lookups.csv", index=False)
    print(f"\nDone! {len(failures)} album(s) failed — saved to Supabase failed_lookups table (and failed_lookups.csv locally).")
else:
    print("\nDone! All albums processed successfully.")