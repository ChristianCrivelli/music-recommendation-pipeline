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
    # issue's own example ("hardcore hip hop = hardcore rap"). Canonical
    # spelling keeps the "rap" wording; spacing/hyphenation/typo variants
    # all fold into it.
    "hardcore hip hop": "hardcore rap",
    "hardcore hip-hop": "hardcore rap",
    "pop hip hop": "pop rap",
    "pop hip-hop": "pop rap",
    "brittish rap": "british rap",
    "british hip hop": "british rap",
    "british hiphop": "british rap",
    "british hip-hop": "british rap",
    "brittish hiphop": "british rap",
    "brittish hip-hop": "british rap",
    "eastcoast rap": "east coast rap",
    "east coast hip hop": "east coast rap",
    "east coast hip-hop": "east coast rap",
    "eastcoast hiphop": "east coast rap",
    "eastcoast hip-hop": "east coast rap",
    "east coast hiphop": "east coast rap",
    "southern hip hop": "southern rap",
    "southern hiphop": "southern rap",
    "southern hip-hop": "southern rap",
    "uk hip hop": "uk rap",
    "uk hiphop": "uk rap",
    "uk hip-hop": "uk rap",
    # Same descriptor-exception pattern, found while auditing the live tags
    # table — not individually named in the issue, but the identical rule:
    "midwest hip hop": "midwest rap",
    "left-field hip-hop": "left-field rap",
    "alt hip hop": "alternative hip hop",
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
}

# Issue #19 — a tag that should become MULTIPLE separate tags rather than
# either collapsing into one or staying joined by "/" (the generic
# slash-split logic below is wrong for this one specifically: splitting
# "alternative/indie rock" on "/" gives "alternative" + "indie rock", not
# the two full genre names Chris actually wants).
SPLIT_TAGS = {
    "alternative/indie rock": ("alternative rock", "indie rock"),
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
        if "/" in clean_tag:
            sub_tags = [sub.strip() for sub in clean_tag.split("/")]
            normalized_sub_tags = [TAG_MAPPING.get(sub, sub) for sub in sub_tags]
            seen = []
            for sub in normalized_sub_tags:
                if sub not in seen:
                    seen.append(sub)
            clean_tag = "/".join(seen)
        else:
            clean_tag = TAG_MAPPING.get(clean_tag, clean_tag)

        if clean_tag not in cleaned_tags:
            cleaned_tags.append(clean_tag)

    return cleaned_tags
