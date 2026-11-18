import re

def clean_artist_list(artist_val):
    if not artist_val:
        return []

    # Tip 4: if Notion already gave us a list, use it as-is — no string mangling needed
    if isinstance(artist_val, list):
        return [str(a).strip() for a in artist_val if a]

    # Fallback: handle accidentally stringified lists like "['Artist A', 'Artist B']"
    clean_str = str(artist_val).strip("[]").replace("'", "").replace('"', '')
    return [a.strip() for a in clean_str.split(',') if a.strip()]

# Dictionary to map messy/duplicate tags to a single standardized tag
TAG_MAPPING = {
    # Hip-hop / Rap variants → canonical "hip-hop"
    "rap": "hip-hop",
    "rap/hip-hop": "hip-hop",
    "rap/hip hop": "hip-hop",
    "rap hip hop": "hip-hop",
    "hip-hop/rap": "hip-hop",
    "hip hop/rap": "hip-hop",
    "hip hop": "hip-hop",
    "hiphop": "hip-hop",

    # R&B variants
    "r b": "r&b",
    "rnb": "r&b",
    "r and b": "r&b",
    "rhythm and blues": "r&b",
    "rhythm & blues": "r&b",

    # R&B sub-genre variants
    "alt r&b": "alternative r&b",
    "alt rnb": "alternative r&b",
    "alternative rnb": "alternative r&b",

    # Rock variants
    "alt rock": "alternative rock",
    "alternative-rock": "alternative rock",
    "indie rock": "indie rock",  # keeps as-is, but normalises spacing
    "indie-rock": "indie rock",

    # Electronic / Dance variants
    "electronica": "electronic",
    "electro": "electronic",
    "dance music": "dance",

    # Pop variants
    "art pop": "art pop",  # normalise spacing
    "art-pop": "art pop",
    "synth pop": "synth-pop",
    "synthpop": "synth-pop",

    # Soul variants
    "neo soul": "neo-soul",
    "neo-soul": "neo-soul",  # already canonical, keeps it consistent

    # --- Issue #19 additions below ---

    # Hip-hop/rap + descriptor exception: a tag that adds its own descriptor
    # (hardcore, pop, british, east coast, southern, uk, ...) keeps its own
    # specific tag rather than genericizing to plain "hip-hop", per the
    # issue's own example ("hardcore hip hop = hardcore rap"). This
    # originally kept the "rap" wording as canonical; 2026-11-17, Chris
    # confirmed the full-family version of his spelling rule ("tags with
    # 'rap' or 'hip hop' should all say 'hip-hop' instead") applies here
    # too — every descriptor tag below now folds to "X hip-hop" instead of
    # "X rap". All spacing/hyphenation/typo/old-canonical variants fold
    # straight to that final form in one hop.
    "hardcore hip hop": "hardcore hip-hop",
    "hardcore hip-hop": "hardcore hip-hop",
    "hardcore rap": "hardcore hip-hop",
    "pop hip hop": "pop hip-hop",
    "pop hip-hop": "pop hip-hop",
    "pop rap": "pop hip-hop",
    "brittish rap": "uk hip-hop",
    "british rap": "uk hip-hop",
    "british hip hop": "uk hip-hop",
    "british hiphop": "uk hip-hop",
    "british hip-hop": "uk hip-hop",
    "brittish hiphop": "uk hip-hop",
    "brittish hip-hop": "uk hip-hop",
    "uk rap": "uk hip-hop",
    "uk hip hop": "uk hip-hop",
    "uk hiphop": "uk hip-hop",
    "eastcoast rap": "east coast hip-hop",
    "east coast rap": "east coast hip-hop",
    "east coast hip hop": "east coast hip-hop",
    "east coast hip-hop": "east coast hip-hop",
    "eastcoast hiphop": "east coast hip-hop",
    "eastcoast hip-hop": "east coast hip-hop",
    "east coast hiphop": "east coast hip-hop",
    "southern rap": "southern hip-hop",
    "southern hip hop": "southern hip-hop",
    "southern hiphop": "southern hip-hop",
    "southern hip-hop": "southern hip-hop",
    # Same descriptor-exception pattern, found while auditing the live tags
    # table — not individually named in the issue, but the identical rule:
    "midwest rap": "midwest hip-hop",
    "midwest hip hop": "midwest hip-hop",
    "left-field rap": "left-field hip-hop",
    "left-field hip-hop": "left-field hip-hop",
    "alt hip hop": "alternative hip-hop",
    # Reversed word order of the existing "rap hip hop" -> "hip-hop" entry
    # above; this one has no descriptor, so it IS a bare generic collapse.
    "hip hop rap": "hip-hop",

    # Bare one-off equivalences from the issue
    "afrobeats": "afrobeat",
    "christmas music": "christmas",
    "dance pop": "dance-pop",
    # laut.de/musikexpress.de/plattentests.de-sourced rows used the German
    # spelling; this app is English-first, so normalize to "english" rather
    # than the other way around.
    "englisch": "english",

    # --- 2026-11-16/17 tag review (first full pass over the live tags
    # table, see claude/ project docs) additions below ---

    # Spelling normalization: any tag with a bare two-word "hip hop" says
    # "hip-hop" instead (same kind of spacing/hyphenation fix as the
    # "synth pop" -> "synth-pop" / "neo soul" -> "neo-soul" entries above).
    "abstract hip hop": "abstract hip-hop",
    "alternative hip hop": "alternative hip-hop",
    "canadian hip hop": "canadian hip-hop",
    "christian hip hop": "christian hip-hop",
    "comedy hip hop": "comedy hip-hop",
    "conscious hip hop": "conscious hip-hop",
    "drumless hip hop": "drumless hip-hop",
    "experimental hip hop": "experimental hip-hop",
    "industrial hip hop": "industrial hip-hop",
    "lo-fi hip hop": "lo-fi hip-hop",
    "political hip hop": "political hip-hop",
    "west coast hip hop": "west coast hip-hop",
    "gangsta rap": "gangsta hip-hop",
    # Slash -> hyphen, same normalization as the other "/"-joined descriptor
    # fixes in this dict.
    "alternative singer/songwriter": "alternative singer-songwriter",
    # The rest of the "X rap" -> "X hip-hop" family, per Chris's confirmed
    # full-family rule — these had no individual spelling note, just the
    # same pattern applied consistently (the ones with a descriptor
    # exception above, like "hardcore rap"/"uk rap", are handled there).
    "cali rap": "cali hip-hop",
    "chicago rap": "chicago hip-hop",
    "chicano rap": "chicano hip-hop",
    "cloud rap": "cloud hip-hop",
    "contemporary rap": "contemporary hip-hop",
    "country rap": "country hip-hop",
    "emo rap": "emo hip-hop",
    "foreignrap": "foreign hip-hop",
    "frat rap": "frat hip-hop",
    "jersey club rap": "jersey club hip-hop",
    "latin rap": "latin hip-hop",
    "memphis rap": "memphis hip-hop",
    "mumble rap": "mumble hip-hop",
    "progressive rap": "progressive hip-hop",
    "rap musicians": "hip-hop musicians",
    "rap rock": "hip-hop rock",
    "sad rap": "sad hip-hop",
    "scam rap": "scam hip-hop",

    # True duplicates found during the tag review (same genre, different
    # spelling/name) — collapse to one canonical tag, chains resolved to
    # their final target so nothing aliases to another alias.
    "alternative rap": "alternative hip-hop",
    "canada": "canadian",
    "dirty south": "southern hip-hop",
    "jazz rap": "jazz hip-hop",
    "thug rap": "gangsta hip-hop",
    "trap rap": "trap",
    "americana": "american",
    "united states": "american",
    "usa": "american",
    # Lidarr-import artifact, not a real tag name at all.
    "funk_add_to_lidarr_batch_5": "funk",

    # 2026-11-17 — confident fixes for tags that were still unreviewed in
    # the Tag Review tool, following the same patterns Chris already set.
    # A handful of other unreviewed tags are genuinely ambiguous and were
    # deliberately left alone for Chris to decide himself (see the project
    # doc) rather than guessed at here.
    "carols": "christmas",
    "pop music": "pop",
    "pop/rock": "pop rock",
    "rap-rock": "hip-hop rock",
    "rapper": "hip-hop musicians",
    "underground rap": "underground hip-hop",
}

# 2026-11-16 tag review — a tag that implies a broader one without
# replacing it (unlike TAG_MAPPING, which collapses two spellings of the
# SAME tag into one). "chicago drill" is still useful as its own tag, but
# an album tagged only "chicago drill" previously shared zero tag-overlap
# with an album tagged plain "hip-hop" — this closes that gap by also
# attaching every listed parent wherever the child tag appears. Applied in
# clean_and_normalize_tags below (new ingestion) and by
# scripts/backfill_tag_hierarchy.py (existing albums, run once per change
# here). Reviewed via the Tag Review tool; a tag not listed here was
# explicitly marked "no change needed" or hasn't been reviewed yet.
TAG_HIERARCHY = {
    "abstract hip-hop": ("hip-hop",),
    "adult contemporary r&b": ("contemporary r&b", "r&b"),
    "afro house": ("afrobeat", "house"),
    "afro pop": ("afrobeat", "pop"),
    "alternative dance": ("dance",),
    "alternative hip-hop": ("hip-hop",),
    "alternative metal": ("metal",),
    "alternative pop": ("pop",),
    "alternative r&b": ("r&b",),
    "alternative rock": ("rock",),
    "alternative singer-songwriter": ("singer-songwriter",),
    "ambient pop": ("pop",),
    "art pop": ("pop",),
    "baroque pop": ("pop",),
    "bedroom pop": ("pop",),
    "billionheir blues": ("blues",),
    "bitpop": ("pop",),
    "boom bap": ("hip-hop",),
    "bossa nova": ("jazz",),
    "brooklyn": ("new york",),
    "cali hip-hop": ("hip-hop",),
    "canadian hip-hop": ("hip-hop",),
    "chamber pop": ("pop",),
    "chicago drill": ("hip-hop", "drill"),
    "chicago hip-hop": ("hip-hop",),
    "chicano hip-hop": ("hip-hop",),
    "christian hip-hop": ("hip-hop",),
    "cloud hip-hop": ("hip-hop",),
    "comedy hip-hop": ("hip-hop",),
    "conscious hip-hop": ("hip-hop",),
    "contemporary christian": ("christian",),
    "contemporary folk": ("folk",),
    "contemporary r&b": ("r&b",),
    "contemporary hip-hop": ("hip-hop",),
    "country hip-hop": ("hip-hop", "country"),
    "dance-pop": ("pop",),
    "deep house": ("house",),
    "digicore": ("hyperpop",),
    "drill": ("hip-hop",),
    "drumless hip-hop": ("hip-hop",),
    "dream pop": ("pop",),
    "east coast hip-hop": ("hip-hop",),
    "edm": ("dance",),
    "electro house": ("electronic", "house"),
    "electropop": ("pop", "electronic"),
    "emo-trap": ("emo", "trap"),
    "emo hip-hop": ("hip-hop", "emo"),
    "experimental hip-hop": ("hip-hop",),
    "folk pop": ("pop",),
    "foreign hip-hop": ("hip-hop",),
    "frat hip-hop": ("hip-hop",),
    "gangsta hip-hop": ("hip-hop",),
    "glitch pop": ("hyperpop",),
    "hard rock": ("rock",),
    "hardcore hip-hop": ("hip-hop",),
    "hip house": ("hip-hop", "house"),
    "hip-hop musicians": ("hip-hop",),
    "hip-hop rock": ("hip-hop", "rock"),
    "hypnagogic pop": ("pop hip-hop",),
    "indie folk": ("folk",),
    "indie pop": ("pop",),
    "indie rock": ("rock",),
    "indietronica": ("indie", "electronic"),
    "industrial hip-hop": ("hip-hop",),
    "jangle pop": ("pop",),
    "jazz pop": ("jazz", "pop"),
    "jazz hip-hop": ("hip-hop", "jazz"),
    "jersey club hip-hop": ("hip-hop",),
    "latin pop": ("pop",),
    "latin hip-hop": ("hip-hop",),
    "left-field hip-hop": ("hip-hop",),
    "lo-fi hip-hop": ("lo-fi", "hip-hop"),
    "melodic trap": ("trap", "hip-hop"),
    "memphis hip-hop": ("hip-hop",),
    "midwest hip-hop": ("hip-hop",),
    "mumble hip-hop": ("hip-hop",),
    "neo-soul": ("soul",),
    "new york drill": ("drill", "hip-hop", "new york"),
    "pluggnb": ("plugg", "r&b"),
    "political hip-hop": ("hip-hop",),
    "pop punk": ("pop", "punk"),
    "pop hip-hop": ("pop", "hip-hop"),
    "pop rock": ("pop", "rock"),
    "pop soul": ("pop", "soul"),
    "post-emo": ("emo",),
    "progressive house": ("house",),
    "progressive hip-hop": ("hip-hop",),
    "psychedelic pop": ("pop",),
    "psychedelic rock": ("rock",),
    "sad hip-hop": ("hip-hop",),
    "scam hip-hop": ("hip-hop",),
    "soft rock": ("rock",),
    "southern hip-hop": ("hip-hop",),
    "synth-pop": ("pop",),
    "trap metal": ("hip-hop", "trap", "metal"),
    "trap soul": ("hip-hop", "trap", "soul"),
    "uk drill": ("uk hip-hop", "hip-hop", "drill"),
    "uk hip-hop": ("hip-hop",),
    "underground hip-hop": ("hip-hop",),
    "west coast hip-hop": ("hip-hop",),

    # 2026-11-17 — confident fixes for tags that were still unreviewed in
    # the Tag Review tool (same idea as above — "X implies Y" — just for
    # tags Chris hadn't gotten to yet). See the project doc for the ones
    # left alone instead.
    "bassline": ("electronic",),
    "blackgaze": ("black metal",),
    "bounce": ("hip-hop",),
    "chamber folk": ("folk",),
    "chillwave": ("electronic",),
    "classical crossover": ("classical",),
    "dark wave": ("electronic",),
    "dub": ("reggae",),
    "experimental electronic": ("electronic", "experimental"),
    "fidget house": ("house",),
    "grunge": ("rock",),
    "hardbass": ("electronic",),
    "hardstyle": ("electronic",),
    "idm": ("electronic",),
    "juke": ("house",),
    "miami bass": ("hip-hop", "electronic"),
    "new jack swing": ("r&b",),
    "nu disco": ("disco",),
    "phonk": ("hip-hop",),
    "piano rock": ("rock",),
    "psychedelic soul": ("soul",),
    "slacker rock": ("rock",),
    "space rock": ("rock",),
    "synthwave": ("electronic",),
    "teen pop": ("pop",),
    "trance": ("electronic",),
    "trap latino": ("trap",),
    "trip hop": ("electronic",),
    "uk garage": ("electronic",),
    "witch house": ("electronic",),
}

# Issue #19 — a tag that should become MULTIPLE separate tags rather than
# either collapsing into one or staying joined by "/" (the generic
# slash-split logic below is wrong for this one specifically: splitting
# "alternative/indie rock" on "/" gives "alternative" + "indie rock", not
# the two full genre names Chris actually wants).
SPLIT_TAGS = {
    "alternative/indie rock": ("alternative rock", "indie rock"),
    # 2026-11-16 tag review — Chris flagged both of these as composite tags
    # that should become their two separate genre tags, same pattern as
    # above, rather than either collapsing to one or getting a hierarchy
    # parent.
    "hip-hop/r&b": ("hip-hop", "r&b"),
    "r&b/swing": ("r&b", "swing"),
}

# Tags to remove entirely — not genre/style information
TAGS_TO_REMOVE = {
    "laut.de",
    "self-titled",
    "ep",
    "billboard",
    # Issue #19 — explicit exclusion list
    "_add serie",
    "american teen",
    "drake",
    "j. cole",
    "musikexpress.de",
    "ph_1_star",
    "ph_temp_checken",
    "plattentests.de",
    "top 100",
    "tumblr",
    # 2026-11-16 tag review — Chris's note: "Shouldn't be a tag" (not a
    # genre/style, junk from the source data, unlike the real "billionheir
    # blues" tag which stays).
    "king billionheir",
    # 2026-11-17 — another artist name used as a tag, same as "drake" /
    # "j. cole" above.
    "travis scott",
}

# Substrings that mark a tag as junk regardless of what else is in the string
# (e.g. "billboard hot 100; 2014; year end chart", "billboard hot 100 tags",
# "ph_2_stars"). These catch chart/ranking/rating-encoded tags that embed a
# year, chart name, or numeric prefix, which exact-match TAGS_TO_REMOVE can't.
JUNK_SUBSTRINGS = (
    "on cover",
    "woechen",
    "wochen",      # correct German spelling ("weeks") — "woechen" alone missed this
    "weeks",
    "chart",       # catches both "chart" and "charts", singular or plural
    "billboard",   # catches any billboard-prefixed variant, not just the bare tag
    "stars",       # catches "5 stars", "ph_2_stars", etc. — rating junk, not genre info
    # Issue #19 — prize/award-related tags aren't genre information either.
    "artist of the year",
)

# Issue #19 — regex-based junk filters that a fixed substring list can't
# express: decade/year tags ("2010s", "1990s", a bare "2014"), prize/award
# tags starting with "best" (the issue's own example is "Best ... artist",
# but the live data also has "best female rapper" with no literal "artist"
# in it — matching on the "best" prefix alone catches both), and tags that
# are pure emoji/symbols with no actual word in them.
JUNK_PATTERNS = (
    re.compile(r"^(19|20)\d{2}s?$"),   # "2010s", "1990s", bare "2014"
    re.compile(r"^best\b"),            # "best female artist", "best female rapper", ...
    re.compile(r"^[^\w]+$", re.UNICODE),  # no letters/digits at all — emoji/symbol-only tags
)

def _is_junk(clean_tag: str) -> bool:
    if any(junk in clean_tag for junk in JUNK_SUBSTRINGS):
        return True
    if clean_tag in TAGS_TO_REMOVE:
        return True
    if any(pattern.search(clean_tag) for pattern in JUNK_PATTERNS):
        return True
    return False

def clean_and_normalize_tags(tags_list):
    if not tags_list:
        return []

    # Issue #19 — some rows have multiple genres baked into one semicolon-
    # joined string (e.g. "east coast rap;hardcore rap;rap;southern rap"),
    # almost certainly from an import that concatenated what should have
    # been separate tags. Flatten those into individual tags before any
    # other processing, so each fragment gets cleaned/mapped/junk-filtered
    # on its own rather than passing through as one unrecognizable blob.
    flattened = []
    for tag in tags_list:
        pieces = str(tag).split(";")
        flattened.extend(p for p in pieces if p.strip())

    cleaned_tags = []
    for tag in flattened:
        clean_tag = str(tag).lower().strip()
        # Strip stray wrapping brackets/parens some source rows carry (e.g.
        # "[hip-hop/rap]"), so the real tag underneath gets processed
        # normally instead of silently passing through as unrecognized junk.
        clean_tag = clean_tag.strip("[]() ")
        if not clean_tag:
            continue

        # Skip junk tags (substring, exact-match, or regex-pattern junk —
        # charts/weeks/billboard, the explicit exclusion list, decades/years,
        # prize tags, emoji-only tags)
        if _is_junk(clean_tag):
            continue

        # Issue #19 — a tag that should explicitly become two separate tags
        if clean_tag in SPLIT_TAGS:
            for split_tag in SPLIT_TAGS[clean_tag]:
                if split_tag not in cleaned_tags:
                    cleaned_tags.append(split_tag)
            continue

        # --- Handle composite / slash-separated tags ---
        # A whole-string TAG_MAPPING entry (e.g. "alternative singer/songwriter"
        # -> "alternative singer-songwriter") takes priority over the generic
        # per-side slash split below — otherwise it would never be reached,
        # since the "/" check always matches first and splits/rejoins the two
        # halves instead of looking up the full original string.
        if clean_tag in TAG_MAPPING:
            clean_tag = TAG_MAPPING[clean_tag]
        elif "/" in clean_tag:
            sub_tags = [sub.strip() for sub in clean_tag.split("/")]
            normalized_sub_tags = [TAG_MAPPING.get(sub, sub) for sub in sub_tags]
            seen = []
            for sub in normalized_sub_tags:
                if sub not in seen:
                    seen.append(sub)
            clean_tag = "/".join(seen)

        if clean_tag not in cleaned_tags:
            cleaned_tags.append(clean_tag)

    # 2026-11-16 tag review — expand every tag that implies a broader
    # category (see TAG_HIERARCHY above) by also attaching its parent
    # tag(s), so e.g. an album tagged only "chicago drill" also surfaces
    # under "hip-hop" and "drill" instead of sharing zero tag overlap with
    # albums tagged plain "hip-hop". Transitive — a parent can itself have
    # a parent — via a small fixed-point loop; the real chains here are at
    # most 2-3 deep, so this comfortably converges.
    for _ in range(4):
        added_any = False
        for tag in list(cleaned_tags):
            for parent in TAG_HIERARCHY.get(tag, ()):
                if parent not in cleaned_tags:
                    cleaned_tags.append(parent)
                    added_any = True
        if not added_any:
            break

    return cleaned_tags
