"""
One-off backfill (2026-11-16 tag review — the weekly tag-review tool,
see the claude/ project docs): apply every TAG_MAPPING rename/alias,
TAG_HIERARCHY parent-expansion, and SPLIT_TAGS split now encoded in
cleaning_methods.py to albums that were already tagged *before* those
entries existed.

Why this is needed at all: clean_and_normalize_tags() only runs on tags
as they come in from MusicBrainz/Last.fm/Spotify during ingestion. Adding
a new TAG_MAPPING/TAG_HIERARCHY/SPLIT_TAGS entry fixes new albums going
forward automatically, but tags/album_tags is an id-keyed FK model —
an album already tagged "gangsta rap" has a row pointing at that tag's
existing id, and nothing about editing a Python dict changes that row.
This script is the one-time (well, re-runnable) catch-up pass over the
albums that already exist.

What it does, in order:
  1. TAGS_TO_REMOVE additions (currently just "king billionheir" — not a
     genre, per the tag review's "Shouldn't be a tag" note) — delete the
     tag and its album_tags rows entirely.
  2. SPLIT_TAGS — for every album tagged with the old composite tag
     (e.g. "hip-hop/r&b"), attach both of its replacement tags
     (creating them if they don't exist yet), then delete the old tag.
  3. TAG_MAPPING — for every album tagged with an old/duplicate spelling
     (e.g. "gangsta rap" -> "gangsta hip-hop", "usa" -> "american"),
     attach the canonical tag and delete the old one. Runs over the
     WHOLE dict, not just the 2026-11-16 additions, so it also mops up
     any pre-issue-#19 tag spellings that are still sitting on older
     albums.
  4. TAG_HIERARCHY — for every album tagged with a tag that implies a
     broader one (e.g. "chicago drill" implies "hip-hop" and "drill"),
     attach the parent tag(s) too, without removing the specific one.
     Runs after step 3 so a tag that was just renamed (e.g. "gangsta
     rap" -> "gangsta hip-hop") picks up its hierarchy parent under its
     new name.

Run locally once, with your usual supabase_url/supabase_key in .env:
    python -m src.ingestion.backfill_tag_hierarchy --execute

Without --execute it's a dry run: prints exactly what it would do
(tags created, tags merged/deleted, album_tags rows added) without
writing anything, so you can sanity-check the plan first.

Safe to re-run: every step is "merge/attach if not already there", so
running it twice (or after adding more entries later) just does nothing
extra for albums already caught up.
"""

import argparse
import os
import sys

from dotenv import load_dotenv
from supabase import create_client, Client

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ingestion.cleaning_methods import TAG_MAPPING, TAG_HIERARCHY, SPLIT_TAGS, TAGS_TO_REMOVE

load_dotenv()

url = os.environ.get("supabase_url")
key = os.environ.get("supabase_key")
supabase: Client = create_client(url, key)

PAGE_SIZE = 1000


def _paginate(table: str, select: str) -> list[dict]:
    rows, start = [], 0
    while True:
        resp = supabase.table(table).select(select).range(start, start + PAGE_SIZE - 1).execute()
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return rows


def _load_state():
    """name -> tag id, and the full set of existing (album_id, tag_id) pairs
    (loaded once up front so duplicate-insert checks are a local set lookup,
    not a round trip per album)."""
    tags_by_name = {r["name"]: r["id"] for r in _paginate("tags", "id, name")}
    pairs = {(r["album_id"], r["tag_id"]) for r in _paginate("album_tags", "album_id, tag_id")}
    return tags_by_name, pairs


def _albums_for_tag(tag_id: str, pairs: set) -> list[str]:
    return [album_id for (album_id, t_id) in pairs if t_id == tag_id]


def _get_or_create_tag(name: str, tags_by_name: dict, dry_run: bool) -> str:
    if name in tags_by_name:
        return tags_by_name[name]
    if dry_run:
        fake_id = f"<new:{name}>"
        tags_by_name[name] = fake_id
        print(f"    would create tag {name!r}")
        return fake_id
    resp = supabase.table("tags").insert({"name": name}).execute()
    new_id = resp.data[0]["id"]
    tags_by_name[name] = new_id
    print(f"    created tag {name!r} ({new_id})")
    return new_id


def _attach(album_id: str, tag_id: str, pairs: set, dry_run: bool) -> bool:
    if (album_id, tag_id) in pairs:
        return False
    pairs.add((album_id, tag_id))
    if not dry_run:
        supabase.table("album_tags").insert({"album_id": album_id, "tag_id": tag_id}).execute()
    return True


def _delete_tag(name: str, tag_id: str, tags_by_name: dict, pairs: set, dry_run: bool):
    album_ids = _albums_for_tag(tag_id, pairs)
    print(f"    deleting tag {name!r} ({len(album_ids)} album_tags row(s))")
    if not dry_run:
        supabase.table("album_tags").delete().eq("tag_id", tag_id).execute()
        supabase.table("tags").delete().eq("id", tag_id).execute()
    pairs.difference_update((a, tag_id) for a in album_ids)
    tags_by_name.pop(name, None)


def remove_junk(tags_by_name: dict, pairs: set, dry_run: bool):
    print("\n== 1. TAGS_TO_REMOVE ==")
    for name in sorted(TAGS_TO_REMOVE):
        tag_id = tags_by_name.get(name)
        if not tag_id:
            continue
        print(f"  {name!r} is live — removing")
        _delete_tag(name, tag_id, tags_by_name, pairs, dry_run)


def apply_splits(tags_by_name: dict, pairs: set, dry_run: bool):
    print("\n== 2. SPLIT_TAGS ==")
    for old_name, new_names in SPLIT_TAGS.items():
        old_id = tags_by_name.get(old_name)
        if not old_id:
            continue
        album_ids = _albums_for_tag(old_id, pairs)
        print(f"  {old_name!r} -> {list(new_names)} ({len(album_ids)} album(s))")
        new_ids = [_get_or_create_tag(n, tags_by_name, dry_run) for n in new_names]
        for album_id in album_ids:
            for new_id in new_ids:
                _attach(album_id, new_id, pairs, dry_run)
        _delete_tag(old_name, old_id, tags_by_name, pairs, dry_run)


def apply_mapping(tags_by_name: dict, pairs: set, dry_run: bool):
    print("\n== 3. TAG_MAPPING (renames / aliases / duplicate collapse) ==")
    for old_name, new_name in TAG_MAPPING.items():
        if old_name == new_name:
            continue
        old_id = tags_by_name.get(old_name)
        if not old_id:
            continue
        album_ids = _albums_for_tag(old_id, pairs)
        print(f"  {old_name!r} -> {new_name!r} ({len(album_ids)} album(s))")
        new_id = _get_or_create_tag(new_name, tags_by_name, dry_run)
        for album_id in album_ids:
            _attach(album_id, new_id, pairs, dry_run)
        _delete_tag(old_name, old_id, tags_by_name, pairs, dry_run)


def apply_hierarchy(tags_by_name: dict, pairs: set, dry_run: bool):
    print("\n== 4. TAG_HIERARCHY (attach parent tag(s), keep the specific one) ==")
    # Transitive, same fixed-point idea as clean_and_normalize_tags: a parent
    # can itself have a parent (none currently do, but this costs nothing
    # and keeps the script correct if that changes later).
    for _ in range(4):
        changed = False
        for child_name, parents in TAG_HIERARCHY.items():
            child_id = tags_by_name.get(child_name)
            if not child_id:
                continue
            album_ids = _albums_for_tag(child_id, pairs)
            if not album_ids:
                continue
            for parent_name in parents:
                parent_id = _get_or_create_tag(parent_name, tags_by_name, dry_run)
                added = 0
                for album_id in album_ids:
                    if _attach(album_id, parent_id, pairs, dry_run):
                        added += 1
                if added:
                    print(f"  {child_name!r} -> +{added} album(s) tagged {parent_name!r}")
                    changed = True
        if not changed:
            break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Actually write changes. Without this flag, runs as a dry run (prints the plan only).")
    args = parser.parse_args()
    dry_run = not args.execute

    print("DRY RUN — nothing will be written. Pass --execute to apply." if dry_run else "EXECUTING — writing changes.")

    tags_by_name, pairs = _load_state()
    remove_junk(tags_by_name, pairs, dry_run)
    apply_splits(tags_by_name, pairs, dry_run)
    apply_mapping(tags_by_name, pairs, dry_run)
    apply_hierarchy(tags_by_name, pairs, dry_run)

    print("\nDone." + (" Re-run with --execute to apply." if dry_run else ""))


if __name__ == "__main__":
    main()
