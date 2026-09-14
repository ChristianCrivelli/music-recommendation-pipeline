import os
import time
import musicbrainzngs
from dotenv import load_dotenv
 
load_dotenv()
 
# User Agent
musicbrainzngs.set_useragent(
    app = "SimilarityMatrixBuilder", 
    version = "1.0", 
    contact = os.getenv("email")
)
 
# MusicBrainz allows 1 request/sec. We make multiple calls per album,
# so we sleep between each one to stay safe.
MB_SLEEP = 1.1

# If a release-group has fewer community tags than this, supplement with the
# artist's own tag-list (aggregated across all their releases, so it tends to
# carry far more votes than one obscure album ever gets on its own).
TAG_FALLBACK_THRESHOLD = 3

# MusicBrainz's own search ranking sometimes prioritizes an exact title match
# over the correct artist (e.g. two different artists both released an album
# called "$ad Boy" — search can return the wrong one as its top hit, and a
# fuzzy artist-similarity score can still be "close enough" for a different
# person with a similar-looking name — "TOB Duke" vs "Madalen Duke", "Tom. G"
# vs "Tom Peters"). Real mismatches slipped through fuzzy scoring undetected,
# so artist matching is no longer fuzzy at all: a candidate is only accepted
# if its MusicBrainz artist-credit is an EXACT match (case/whitespace-
# normalized) for the artist name we searched with (Notion's Artist(s), or a
# manual_overrides search_artist hint). No fuzzy fallback — if nothing
# matches exactly, the album is flagged for manual review via failed_lookups
# instead of silently attaching a possibly-wrong artist. See
# _artist_exact_match() below.
#
# Title matching used to be fuzzy (difflib.SequenceMatcher, gated by a
# MIN_TITLE_CONFIDENCE score threshold) on the theory that typos/edition
# differences are common and low-risk. In practice this let wrong-album
# matches through the same way fuzzy artist matching let wrong-artist matches
# through — a fuzzy-close title on an EP, live album, or deluxe reissue could
# out-rank the actual release. Title matching is now exact-only too, mirroring
# artist matching exactly: a candidate is only accepted if its MusicBrainz
# release-group title is an EXACT match (case/whitespace-normalized) for the
# title we searched with. See _title_exact_match() below. No fuzzy fallback —
# if MusicBrainz has no release by that exact artist with that exact title,
# the album is flagged for manual review via failed_lookups instead of
# guessing at the closest-sounding candidate.

# Transient network hiccups (connection resets, timeouts, momentary 5xx from
# MusicBrainz) surface as musicbrainzngs.WebServiceError, identically to a
# genuine "this doesn't exist" response. Previously a single blip on ANY of
# the several calls get_metadata makes was enough to mark a well-known,
# definitely-indexed album (e.g. "Eternal Atake", "WUNNA") as unfindable.
# Retrying a couple of times before giving up filters most of that noise out.
MB_MAX_ATTEMPTS = 3
MB_RETRY_BACKOFF = 2.0  # seconds, multiplied by attempt number


def _mb_call(fn, *args, **kwargs):
    """Call a musicbrainzngs function, retrying on transient WebServiceError.
    Sleeps MB_SLEEP after every attempt (success or failure) to respect the
    1 request/sec limit, then re-raises the last error if all attempts fail."""
    last_exc = None
    for attempt in range(1, MB_MAX_ATTEMPTS + 1):
        try:
            result = fn(*args, **kwargs)
            time.sleep(MB_SLEEP)
            return result
        except musicbrainzngs.WebServiceError as exc:
            last_exc = exc
            time.sleep(MB_SLEEP)
            if attempt < MB_MAX_ATTEMPTS:
                print(f"MusicBrainz call failed (attempt {attempt}/{MB_MAX_ATTEMPTS}): {exc} — retrying...")
                time.sleep(MB_RETRY_BACKOFF * attempt)
    raise last_exc


def _candidate_artist_str(rg):
    return ", ".join(
        c['artist']['name'] for c in rg.get('artist-credit', [])
        if isinstance(c, dict) and 'artist' in c
    )


# A small number of well-known artists are indexed on MusicBrainz under a
# different name than the one Notion (and most people) actually use — not a
# misspelling, a real rename/rebrand (Kanye West's MusicBrainz artist-credit
# is "Ye"). Under strict exact-matching (see comment above), a search for
# "Kanye West" can never match a candidate credited to "Ye", so every new
# Kanye West album added to Notion would otherwise need its own one-off
# manual_overrides row just to work around the same rename every time.
# Resolving both sides of the comparison through this table fixes it once,
# for every current and future album by this artist, without touching
# Notion's more familiar spelling or MusicBrainz's own data. Deliberately
# small and manually curated — add an entry here only once a real
# rename/rebrand has actually produced a failed_lookups row, not
# preemptively for every stylization difference (most of those — Tyler The
# Creator, PinkPantheress, etc. — are one-off enough to leave as the
# "benign difference" the wrong-artist audit already treats them as).
ARTIST_ALIASES = {
    "kanye west": "ye",
}


def _normalize_artist(name):
    """Case/whitespace-insensitive normalization for exact-match comparison.
    Deliberately NOT fuzzy — no stripping of punctuation, accents, or
    near-miss spelling, since that's exactly the kind of "close enough"
    leniency that let wrong-artist matches through before. The one
    exception is ARTIST_ALIASES above, applied after normalization so it
    matches regardless of which side (search input or MB candidate) the
    alias name appears on."""
    normalized = " ".join((name or "").strip().lower().split())
    return ARTIST_ALIASES.get(normalized, normalized)


def _artist_name_set(artist_str):
    """Split a comma-joined artist string (both Notion's Artist(s) field and
    MusicBrainz's artist-credit are joined this way) into a normalized set of
    individual names, order-independent."""
    return {_normalize_artist(part) for part in (artist_str or "").split(",") if part.strip()}


def _artist_exact_match(candidate_artist_str, expected_artist_name):
    """True only if the candidate's MusicBrainz artist-credit and the
    expected artist (from Notion or a manual_overrides search_artist hint)
    are the same set of names once normalized — handles solo artists and
    multi-artist collabs alike, but requires an exact name match, not a
    fuzzy one."""
    expected_set = _artist_name_set(expected_artist_name)
    candidate_set = _artist_name_set(candidate_artist_str)
    if not expected_set or not candidate_set:
        return False
    return expected_set == candidate_set


def _normalize_title(title):
    """Case/whitespace-insensitive normalization for exact-match comparison.
    Mirrors _normalize_artist above — deliberately NOT fuzzy, and does not
    strip punctuation/edition markers ("(Deluxe)", "(Live)", etc.), since a
    deluxe/live/remix edition being distinct from the original album is
    exactly the kind of "close enough" leniency that let wrong matches
    through before."""
    return " ".join((title or "").strip().lower().split())


def _title_exact_match(candidate_title, expected_title):
    """True only if the candidate's MusicBrainz release-group title is an
    EXACT match (case/whitespace-normalized) for the title we searched with
    (Notion's Title, or a manual_overrides search_title hint)."""
    a = _normalize_title(candidate_title)
    b = _normalize_title(expected_title)
    return bool(a) and bool(b) and a == b


def get_artist_tags(artist_mbid):
    """Fetch an artist's own MusicBrainz tag-list, sorted by community votes.
    Used as a fallback when a specific release-group has few/no tags of its own."""
    if not artist_mbid:
        return []
    try:
        result = _mb_call(musicbrainzngs.get_artist_by_id, artist_mbid, includes=["tags"])
        raw_tags = result.get('artist', {}).get('tag-list', [])
        sorted_tags = sorted(raw_tags, key=lambda x: int(x.get('count', 0)), reverse=True)
        return [tag['name'] for tag in sorted_tags[:5]]
    except musicbrainzngs.WebServiceError as exc:
        print(f"MusicBrainz Error fetching artist tags for {artist_mbid}: {exc}")
        return []


def get_metadata(album_name, artist_name):
    """Returns (metadata_dict, reason). On success, metadata_dict is populated
    and reason is None. On failure, metadata_dict is None and reason is a
    human-readable string distinguishing *why* — no candidates found, no
    confident match, or a MusicBrainz API/network error — instead of the old
    behavior where all three collapsed into an indistinguishable None."""
    try:
        # 1. Search for the Release Group
        # Searching by Release Group is better for catching the "master" record.
        # Fetch more candidates than before (10, was 5) — now that acceptance
        # requires an EXACT title AND EXACT artist match rather than fuzzy
        # scoring, the right candidate needs to actually be in the fetched
        # set, so it's worth casting a slightly wider net against
        # MusicBrainz's own ranking.
        search = _mb_call(musicbrainzngs.search_release_groups, artist=artist_name, releasegroup=album_name, limit=10)

        candidates = search.get('release-group-list', [])
        if not candidates:
            return None, "No MusicBrainz release-group candidates found for this title/artist"

        scored = []
        for rg in candidates:
            title_exact = _title_exact_match(rg.get('title'), album_name)
            candidate_artist_str = _candidate_artist_str(rg)
            artist_exact = _artist_exact_match(candidate_artist_str, artist_name)
            scored.append((title_exact, artist_exact, candidate_artist_str, rg))

        # Both title and artist matching are exact-only now (see comment on
        # this file's title-matching note above) — a fuzzy-close title or
        # artist name is treated the same as a totally wrong one: not a
        # match. No candidate with BOTH an exact title match and an exact
        # artist match means we don't know which release this actually is,
        # so flag it for manual review (via failed_lookups) rather than
        # guess at the closest-sounding candidate.
        exact_candidates = [c for c in scored if c[0] and c[1]]
        if not exact_candidates:
            candidate_summary = "; ".join(
                f"'{c[3].get('title')}' by {c[2]}" for c in scored[:5]
            ) or "none"
            reason = (
                f"No MusicBrainz candidate had both an exact title match for '{album_name}' "
                f"and an exact artist match for '{artist_name}' (case/whitespace-normalized) — "
                f"top candidates found: {candidate_summary}. Flagged for manual review: confirm "
                f"the correct title/artist and add a manual_overrides search_title/search_artist "
                f"hint, or fix the source data if MusicBrainz spells/lists it differently."
            )
            print(f"{reason} [{album_name} by {artist_name}]")
            return None, reason

        # More than one exact match is possible (e.g. the same album
        # reissued under separate release-groups — a clean vs. explicit
        # edition, a regional release, a remaster, etc.). This used to just
        # take MusicBrainz's first hit among the exact matches, trusting its
        # relevance ranking — but that ranking is NOT guaranteed to return
        # candidates in the same order on every call (it can shift as
        # MusicBrainz's own index changes). Since album_push_logic.py
        # upserts on_conflict="mbid", a different "first hit" on a later run
        # meant the very same real album silently got inserted as a brand
        # new row instead of updating the existing one — issue #22 found 762
        # albums (~39% of the catalog) duplicated this way, accumulated
        # across repeated monthly FULL_SYNC deep syncs.
        #
        # Break ties deterministically instead: earliest first-release-date
        # (a reasonable "prefer the original edition" heuristic — missing
        # dates sort last), then lowest release-group id as a final,
        # unconditionally stable tiebreaker. The exact tiebreak rule matters
        # less than that it's deterministic — the same title/artist must
        # always resolve to the same release-group.
        exact_candidates.sort(
            key=lambda c: (
                not c[3].get('first-release-date'),
                c[3].get('first-release-date') or '',
                c[3].get('id') or '',
            )
        )
        _, _, best_candidate_artist_str, best_rg = exact_candidates[0]

        rg_id = best_rg['id']
        
        # 2. Fetch Full Release Group Data
        # Includes tags (genres/vibes), the list of specific releases (CDs, Vinyls, etc.),
        # and artist-credits (used below as the tag/artist-fallback source — this is a
        # free addition to the same request, not an extra MusicBrainz call).
        # NOTE: "artists" and "artist-credits" are two different valid includes for
        # release-group — "artists" is an unrelated subquery include and does NOT
        # populate the artist-credit field read below. Must be "artist-credits".
        rg_data = _mb_call(musicbrainzngs.get_release_group_by_id, rg_id, includes=["tags", "releases", "artist-credits"])['release-group']
        
        # --- EXTRACT HIGHER-LEVEL TAXONOMY ---
        primary_type = rg_data.get('primary-type', 'Unknown')
        secondary_types = rg_data.get('secondary-type-list', [])
        
        # Sort tags by community votes (count) and take the top 5
        raw_tags = rg_data.get('tag-list', [])
        sorted_tags = sorted(raw_tags, key=lambda x: int(x.get('count', 0)), reverse=True)
        top_tags = [tag['name'] for tag in sorted_tags[:5]]
        tag_source = "release-group"

        # --- FALLBACK: sparse release-group tags → supplement with artist tags ---
        # Many lesser-known or newer releases simply never accumulate community
        # tags on MusicBrainz. The artist entity usually has richer tag data
        # (votes aggregated across every release), so fill gaps from there.
        if len(top_tags) < TAG_FALLBACK_THRESHOLD:
            rg_artist_mbids = [
                c['artist']['id'] for c in rg_data.get('artist-credit', [])
                if isinstance(c, dict) and 'artist' in c
            ]
            seen = {t.lower() for t in top_tags}
            gained_any = False
            for artist_mbid in rg_artist_mbids:
                for tag in get_artist_tags(artist_mbid):
                    if tag.lower() not in seen:
                        top_tags.append(tag)
                        seen.add(tag.lower())
                        gained_any = True
            top_tags = top_tags[:5]
            if gained_any:
                tag_source = "release-group+artist-fallback"
        
        # Get the original release year
        first_date = rg_data.get('first-release-date', 'Unknown')
        year = first_date.split('-')[0] if first_date != 'Unknown' else None
        
        # --- EXTRACT SONIC FOOTPRINT & CREDITS ---
        avg_track_length = None
        labels = []
        artist_mbids = []
        artist_dicts = []  # Tip 2: initialise here so it always exists
        release_id = None  # Tip 2: initialise here so it always exists
        producers = []  # initialise here so it always exists, same reasoning
 
        # We need a specific release to get tracks and labels. We'll just grab the first one.
        if 'release-list' in rg_data and len(rg_data['release-list']) > 0:
            release_id = rg_data['release-list'][0]['id']
            
            # Fetch the specific release including tracks, labels, artist
            # credits, AND artist relationships (producer credits) in one
            # call. This used to be two separate get_release_by_id calls for
            # the exact same release_id — this one, and a second one made
            # later by get_producers(release_id) with includes=["artist-rels"]
            # only. Same endpoint, same release, just different includes —
            # merged here to save a full MB_SLEEP round-trip per album (see
            # the perf discussion in issue #13). Producers are extracted
            # below, right after this fetch, from the same response.
            release_data = _mb_call(musicbrainzngs.get_release_by_id, release_id, includes=["recordings", "labels", "artist-credits", "artist-rels"])['release']
            
            # Extract Labels
            if 'label-info-list' in release_data:
                labels = [l['label']['name'] for l in release_data['label-info-list'] if 'label' in l]
                
            # Extract Artist MBIDs (Great for finding overlapping producers/personnel later)
            if 'artist-credit' in release_data:
                artist_mbids = [c['artist']['id'] for c in release_data['artist-credit'] if isinstance(c, dict) and 'artist' in c]
                
            # Calculate Average Track Length
            total_ms = 0
            track_count = 0
            for medium in release_data.get('medium-list', []):
                for track in medium.get('track-list', []):
                    if 'recording' in track and 'length' in track['recording']:
                        total_ms += int(track['recording']['length'])
                        track_count += 1
                        
            if track_count > 0:
                avg_ms = total_ms / track_count
                avg_track_length = round(avg_ms / 60000, 2) # Convert to minutes

            # Tip 2: build artist_dicts inside the block where release_data is guaranteed to exist
            if 'artist-credit' in release_data:
                for c in release_data['artist-credit']:
                    if isinstance(c, dict) and 'artist' in c:
                        artist_dicts.append({
                            'name': c['artist']['name'],
                            'mbid': c['artist']['id']
                        })

            # Extract producer credits from the same release_data fetched
            # above (now includes "artist-rels") instead of a second
            # get_release_by_id call — see the comment on that fetch.
            if 'artist-relation-list' in release_data:
                for rel in release_data['artist-relation-list']:
                    if rel['type'] == 'producer':
                        artist_info = rel['artist']
                        producers.append({
                            'name': artist_info['name'],
                            'mbid': artist_info['id']
                        })

        # Fallback: the specific release sometimes has no artist-credit data
        # at all (not just a missing mbid — the whole block absent), which
        # is a separate cause of "Unknown artist" from the missing-mbid case
        # handled downstream in album_push_logic.py. The release-group level
        # almost always has artist-credit, and we already fetch it above
        # (via includes=["artist-credits"]) for the tag fallback, so this
        # costs no extra request.
        if not artist_dicts:
            rg_artist_dicts = [
                {'name': c['artist']['name'], 'mbid': c['artist']['id']}
                for c in rg_data.get('artist-credit', [])
                if isinstance(c, dict) and 'artist' in c
            ]
            if rg_artist_dicts:
                artist_dicts = rg_artist_dicts
                if not artist_mbids:
                    artist_mbids = [a['mbid'] for a in rg_artist_dicts if a.get('mbid')]
                
        # 3. Return the compiled feature vector
        print(f"Found data for {album_name} by {artist_name}")
 
        return {
            "Album": album_name,
            "Primary Type": primary_type,
            "Secondary Types": secondary_types,
            "Release Year": year,
            "Top Tags": top_tags,
            "Tag Source": tag_source,
            "Avg Track Length (Mins)": avg_track_length,
            "Labels": labels,
            "Artist MBIDs": artist_mbids,
            # The release-GROUP id is stable across runs — it identifies
            # "the album" itself, unrelated to which specific edition/
            # pressing MusicBrainz happens to return first. This is what
            # should be persisted as the durable identifier (see
            # album_push_logic.py). Release ID (below) is still needed for
            # this run's track/label lookups, but is NOT safe to treat as a
            # stable per-album key — MusicBrainz
            # doesn't guarantee release-list ordering is the same between
            # calls, so re-processing the same album (e.g. the monthly
            # FULL_SYNC deep sync) could previously land on a different
            # release each time, producing a different "mbid" and causing
            # the same album to be inserted as a brand new row every run.
            "Release Group ID": rg_id,
            "Release ID": release_id,
            "Artists": artist_dicts,
            "Producers": producers,
        }, None
        
    except musicbrainzngs.WebServiceError as exc:
        # Reached MB_MAX_ATTEMPTS retries in _mb_call without success — this
        # is very likely a transient network/API issue, not a genuine "not
        # found". Surface the real exception text instead of a generic
        # "lookup failed" so it's distinguishable in failed_lookups and worth
        # a re-run rather than manual research.
        reason = f"MusicBrainz API error after {MB_MAX_ATTEMPTS} attempts: {exc}"
        print(f"{reason} [{album_name} by {artist_name}]")
        return None, reason

def get_producers(release_id):
    """Standalone producer lookup for a known release_id.

    NOT called by the main pipeline anymore (album_push_logic.py) — that
    path gets producers for free from get_metadata()'s own release_data
    fetch, which now includes "artist-rels" too, instead of making this
    exact same MusicBrainz call a second time. Kept here for standalone/
    one-off use (e.g. scripts operating on a release_id without having
    called get_metadata first)."""
    result = _mb_call(musicbrainzngs.get_release_by_id, release_id, includes=["artist-rels"])

    producers = []
    
    # Look through the relationships for this release
    if 'artist-relation-list' in result['release']:
        for rel in result['release']['artist-relation-list']:
            # We specifically look for the 'producer' type
            if rel['type'] == 'producer':
                artist_info = rel['artist']
                producers.append({
                    'name': artist_info['name'],
                    'mbid': artist_info['id']
                })
                
    return producers