-- Milestone 1 backend reliability patch.
-- Run in Supabase SQL Editor for existing projects before deploying this backend.

ALTER TABLE public.connected_repos
ADD COLUMN IF NOT EXISTS auto_merge BOOLEAN DEFAULT false;

ALTER TABLE public.connected_repos
ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();

ALTER TABLE public.diagnoses
ADD COLUMN IF NOT EXISTS speculative BOOLEAN DEFAULT false;

ALTER TABLE public.diagnoses
ADD COLUMN IF NOT EXISTS required_secrets JSONB NOT NULL DEFAULT '[]'::jsonb;

DO $$
BEGIN
  ALTER TABLE public.diagnoses
  DROP CONSTRAINT IF EXISTS diagnoses_iteration_check;

  ALTER TABLE public.diagnoses
  ADD CONSTRAINT diagnoses_iteration_check CHECK (iteration BETWEEN 1 AND 4);
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_ci_runs_repo_commit_run_unique
ON public.ci_runs(repo_id, commit_sha, github_run_id);

CREATE INDEX IF NOT EXISTS idx_diagnoses_required_secrets
ON public.diagnoses USING GIN (required_secrets)
WHERE jsonb_array_length(required_secrets) > 0;
