-- NOT YET APPLIED — run this against Supabase yourself (via the SQL editor
-- or the Supabase MCP tool), then this comment can be updated/removed.
-- Claude's auto-mode permissions block schema-modifying actions against
-- this shared project (same "[Modify Shared Resources]" denial as the
-- 2026-11-06 duplicate-row cleanup), so this one couldn't be applied
-- directly the way spotify_fallback_columns_migration.sql etc. were.
--
-- Usage telemetry for /api/recommend (book-research-proposal doc,
-- 2026-11-10 update, "ML engineering discipline" section #2 in the
-- suggested sequencing): previously there was no record at all of what
-- this endpoint actually returns for real queries. backend/main.py's
-- _log_recommendation() writes one row per successful match (query text,
-- the artist hint if given, which album it matched, and the top-N result
-- ids/similarities) — this is what unlocks the hand-review sampling and
-- the listener_ratings online-signal comparison the eval-framework
-- section of that doc called for.
--
-- Insert-only RLS, same pattern as album_requests and listener_ratings
-- (see the comment on /api/request-album in backend/main.py) — the
-- public-facing backend's Supabase key stays unable to SELECT/UPDATE/
-- DELETE anything here or anywhere else, consistent with the module's
-- read-only-by-design principle.

CREATE TABLE IF NOT EXISTS public.recommend_logs (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  created_at timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
  query_text text NOT NULL,
  query_artist_hint text,
  n_requested integer NOT NULL DEFAULT 5,
  matched_album_id uuid,
  matched_title text,
  result_album_ids uuid[] NOT NULL DEFAULT '{}',
  result_similarities numeric[] NOT NULL DEFAULT '{}',
  CONSTRAINT recommend_logs_pkey PRIMARY KEY (id),
  CONSTRAINT recommend_logs_matched_album_id_fkey
    FOREIGN KEY (matched_album_id) REFERENCES public.albums(id) ON DELETE SET NULL
);

ALTER TABLE public.recommend_logs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Enable insert for all users"
ON public.recommend_logs
FOR INSERT
TO public
WITH CHECK (true);

COMMENT ON TABLE public.recommend_logs IS 'Usage telemetry for /api/recommend — one row per successful match, written by backend/main.py:_log_recommendation(). Insert-only from the API; read it yourself via the Supabase table editor or SQL, same as album_requests/failed_lookups.';
