-- Fix A13: Add source column to ci_runs for filtering smoke tests vs real users.
-- Run this in the Supabase SQL editor.

ALTER TABLE public.ci_runs ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'user';

-- Backfill existing smoke-test runs based on commit_message patterns.
UPDATE public.ci_runs
SET source = 'smoke_test'
WHERE source = 'user'
  AND (
    lower(commit_message) LIKE '%drufiy smoke test%'
    OR lower(commit_message) LIKE '%test ci%'
    OR lower(commit_message) LIKE '%chore: retrigger%'
    OR lower(commit_message) LIKE '%chore: test%'
  );
