-- One-time cleanup for issue #22: merges the ~766 duplicate album rows
-- (766 extra rows across 762 title+artist groups, out of 1965 total albums)
-- that accumulated because get_metadata() could resolve the same
-- title+artist to a different MusicBrainz release-group mbid on separate
-- FULL_SYNC runs, and album_push_logic.py's on_conflict="mbid" upsert had
-- no way to recognize that as the same album. See the code fix in
-- src/ingestion/album_finder.py (deterministic tie-break) and
-- src/ingestion/album_push_logic.py (find_existing_album_id, checks
-- title+artist before falling back to the mbid-keyed upsert) — this script
-- only cleans up rows that already exist from before that fix.
--
-- Survivor per group = the OLDEST row (earliest created_at, ties broken by
-- id) — it's the one most likely to be the album's original entry, with
-- the later duplicate(s) being the accidental FULL_SYNC re-inserts.
--
-- HOW TO RUN THIS SAFELY:
--   1. Run PART A alone first (it's read-only) and look at the row counts
--      and sample rows it prints. Confirm the numbers roughly match what's
--      described above (762 groups / 766 extra rows) before going further.
--   2. Run PART B as one transaction. It prints intermediate counts as it
--      goes. If anything looks wrong, ROLLBACK; instead of COMMIT; at the
--      very end — nothing is permanent until COMMIT runs.
--   3. Optional cleanup: once you're confident the merge is correct, you
--      can DROP TABLE albums_dupe_cleanup_backup_2026_09_12; to remove the
--      backup this script creates in PART B step 0.
--
-- Safe to re-run PART A/B again later — every step re-derives duplicate
-- groups from whatever's currently in the table, so if some other process
-- creates new duplicates before the code fix is deployed, running this
-- again will just clean those up too (and do nothing if there's nothing
-- left to merge).


-- ============================================================
-- PART A — read-only: see what would be affected before touching anything
-- ============================================================

WITH album_artist AS (
    SELECT
        ac.album_id,
        string_agg(DISTINCT lower(trim(ar.name)), ', ' ORDER BY lower(trim(ar.name))) AS artist_key
    FROM album_contributions ac
    JOIN artists ar ON ar.id = ac.person_id
    WHERE ac.role = 'artist'
    GROUP BY ac.album_id
),
groups AS (
    SELECT
        lower(trim(al.title)) AS title_key,
        aa.artist_key,
        array_agg(al.id ORDER BY al.created_at, al.id) AS album_ids,
        array_agg(al.title ORDER BY al.created_at, al.id) AS titles,
        count(*) AS row_count
    FROM albums al
    JOIN album_artist aa ON aa.album_id = al.id
    GROUP BY lower(trim(al.title)), aa.artist_key
    HAVING count(*) > 1
)
SELECT
    (SELECT count(*) FROM groups) AS duplicate_groups,
    (SELECT sum(row_count) FROM groups) AS total_rows_in_dupe_groups,
    (SELECT sum(row_count - 1) FROM groups) AS extra_rows_to_remove;

-- Sample of the actual groups, so you can eyeball a few before running Part B:
WITH album_artist AS (
    SELECT
        ac.album_id,
        string_agg(DISTINCT lower(trim(ar.name)), ', ' ORDER BY lower(trim(ar.name))) AS artist_key
    FROM album_contributions ac
    JOIN artists ar ON ar.id = ac.person_id
    WHERE ac.role = 'artist'
    GROUP BY ac.album_id
),
groups AS (
    SELECT
        al.title,
        aa.artist_key,
        array_agg(al.id ORDER BY al.created_at, al.id) AS album_ids,
        array_agg(al.mbid ORDER BY al.created_at, al.id) AS mbids,
        count(*) AS row_count
    FROM albums al
    JOIN album_artist aa ON aa.album_id = al.id
    GROUP BY al.title, aa.artist_key
    HAVING count(*) > 1
)
SELECT * FROM groups ORDER BY row_count DESC, title LIMIT 25;


-- ============================================================
-- PART B — the actual cleanup. Run as one transaction.
-- ============================================================

BEGIN;

-- Step 0: back up every row this script is about to delete, so there's a
-- way back if something looks wrong after the fact.
CREATE TABLE IF NOT EXISTS albums_dupe_cleanup_backup_2026_09_12 (LIKE albums INCLUDING ALL);

CREATE TEMP TABLE _dupe_plan AS
WITH album_artist AS (
    SELECT
        ac.album_id,
        string_agg(DISTINCT lower(trim(ar.name)), ', ' ORDER BY lower(trim(ar.name))) AS artist_key
    FROM album_contributions ac
    JOIN artists ar ON ar.id = ac.person_id
    WHERE ac.role = 'artist'
    GROUP BY ac.album_id
),
groups AS (
    SELECT
        array_agg(al.id ORDER BY al.created_at, al.id) AS album_ids
    FROM albums al
    JOIN album_artist aa ON aa.album_id = al.id
    GROUP BY lower(trim(al.title)), aa.artist_key
    HAVING count(*) > 1
)
SELECT
    album_ids[1] AS survivor_id,
    album_ids[2:array_length(album_ids, 1)] AS dupe_ids
FROM groups;

SELECT count(*) AS groups_planned, sum(array_length(dupe_ids, 1)) AS rows_to_delete FROM _dupe_plan;

-- Step 1: snapshot the rows about to be deleted.
INSERT INTO albums_dupe_cleanup_backup_2026_09_12
SELECT a.*
FROM albums a
JOIN _dupe_plan p ON a.id = ANY (p.dupe_ids);

-- Step 2: backfill any NULL enrichment field on the survivor from its
-- (arbitrarily chosen, first) duplicate — never overwrites a value the
-- survivor already has. mbid is deliberately left alone: the code fix
-- means future runs correct it via title+artist matching regardless of
-- what it's currently set to, so there's no need to risk the UNIQUE
-- constraint here.
UPDATE albums s
SET
    release_year      = COALESCE(s.release_year, d.release_year),
    avg_length        = COALESCE(s.avg_length, d.avg_length),
    primary_type      = COALESCE(s.primary_type, d.primary_type),
    spotify_album_id  = COALESCE(s.spotify_album_id, d.spotify_album_id),
    spotify_cover_url = COALESCE(s.spotify_cover_url, d.spotify_cover_url),
    metadata_source   = COALESCE(s.metadata_source, d.metadata_source)
FROM _dupe_plan p
JOIN albums d ON d.id = p.dupe_ids[1]
WHERE s.id = p.survivor_id;

-- Step 3: repoint contributions (artist + producer credits) from every
-- duplicate onto the survivor. UPDATE doesn't support ON CONFLICT, and a
-- plain "delete anything colliding with the survivor, then UPDATE the
-- rest" isn't enough either — a 3-row group's two duplicates can collide
-- with EACH OTHER (both crediting the same person/role) without either one
-- colliding with the survivor first. INSERT ... ON CONFLICT DO NOTHING
-- handles that correctly regardless of how many duplicates share a link,
-- since Postgres resolves each inserted row's conflict independently.
INSERT INTO album_contributions (album_id, person_id, role)
SELECT DISTINCT p.survivor_id, ac.person_id, ac.role
FROM album_contributions ac
JOIN _dupe_plan p ON ac.album_id = ANY (p.dupe_ids)
ON CONFLICT (album_id, person_id, role) DO NOTHING;

DELETE FROM album_contributions ac
USING _dupe_plan p
WHERE ac.album_id = ANY (p.dupe_ids);

-- Step 4: same idea for tags (PK is (album_id, tag_id)).
INSERT INTO album_tags (album_id, tag_id)
SELECT DISTINCT p.survivor_id, at.tag_id
FROM album_tags at
JOIN _dupe_plan p ON at.album_id = ANY (p.dupe_ids)
ON CONFLICT (album_id, tag_id) DO NOTHING;

DELETE FROM album_tags at
USING _dupe_plan p
WHERE at.album_id = ANY (p.dupe_ids);

-- Step 5: delete the now-empty duplicate album rows.
DELETE FROM albums a
USING _dupe_plan p
WHERE a.id = ANY (p.dupe_ids);

-- Step 6: verify — should return zero rows.
WITH album_artist AS (
    SELECT
        ac.album_id,
        string_agg(DISTINCT lower(trim(ar.name)), ', ' ORDER BY lower(trim(ar.name))) AS artist_key
    FROM album_contributions ac
    JOIN artists ar ON ar.id = ac.person_id
    WHERE ac.role = 'artist'
    GROUP BY ac.album_id
)
SELECT al.title, aa.artist_key, count(*)
FROM albums al
JOIN album_artist aa ON aa.album_id = al.id
GROUP BY al.title, aa.artist_key
HAVING count(*) > 1;

-- If step 6 returned zero rows and the counts from earlier steps looked
-- right: COMMIT;
-- If anything looks wrong: ROLLBACK;
