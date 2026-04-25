-- =========================================================================
-- Drufiy schema v2 — full migration
-- Run this in Supabase SQL Editor (Dashboard → SQL Editor → New query)
-- =========================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- =========================================================================
-- User profiles (standalone — no auth.users dependency for sprint)
-- =========================================================================
CREATE TABLE IF NOT EXISTS public.user_profiles (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  github_username TEXT NOT NULL,
  github_user_id BIGINT NOT NULL UNIQUE,
  github_access_token_encrypted BYTEA,
  email TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_user_profiles_github_user_id ON public.user_profiles(github_user_id);

-- =========================================================================
-- Connected GitHub repos
-- =========================================================================
CREATE TABLE IF NOT EXISTS public.connected_repos (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES public.user_profiles(id) ON DELETE CASCADE NOT NULL,
  github_repo_id BIGINT NOT NULL,
  repo_name TEXT NOT NULL,
  repo_full_name TEXT NOT NULL,
  default_branch TEXT DEFAULT 'main',
  webhook_id BIGINT NOT NULL,
  is_active BOOLEAN DEFAULT true,
  rate_limit_window_start TIMESTAMPTZ DEFAULT now(),
  rate_limit_count INT DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_connected_repos_user_id ON public.connected_repos(user_id);
CREATE INDEX IF NOT EXISTS idx_connected_repos_github_repo_id ON public.connected_repos(github_repo_id);

-- =========================================================================
-- Known-good workflow file cache
-- =========================================================================
CREATE TABLE IF NOT EXISTS public.known_good_files (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  repo_id UUID REFERENCES public.connected_repos(id) ON DELETE CASCADE NOT NULL,
  file_path TEXT NOT NULL,
  content TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  verified_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(repo_id, file_path)
);

CREATE INDEX IF NOT EXISTS idx_known_good_files_repo_id ON public.known_good_files(repo_id);

-- =========================================================================
-- CI workflow runs
-- =========================================================================
CREATE TABLE IF NOT EXISTS public.ci_runs (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  repo_id UUID REFERENCES public.connected_repos(id) ON DELETE CASCADE NOT NULL,
  github_run_id BIGINT NOT NULL UNIQUE,
  github_workflow_id BIGINT,
  github_workflow_name TEXT,
  run_name TEXT,
  branch TEXT,
  commit_sha TEXT NOT NULL,
  commit_message TEXT,
  status TEXT DEFAULT 'pending',
  fix_branch_name TEXT,
  error_message TEXT,
  logs_url TEXT,
  verification_checked_workflows JSONB DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ci_runs_repo_id ON public.ci_runs(repo_id);
CREATE INDEX IF NOT EXISTS idx_ci_runs_github_run_id ON public.ci_runs(github_run_id);
CREATE INDEX IF NOT EXISTS idx_ci_runs_commit_sha ON public.ci_runs(commit_sha);
CREATE INDEX IF NOT EXISTS idx_ci_runs_status ON public.ci_runs(status);
CREATE INDEX IF NOT EXISTS idx_ci_runs_fix_branch ON public.ci_runs(fix_branch_name) WHERE fix_branch_name IS NOT NULL;

-- =========================================================================
-- AI diagnoses
-- =========================================================================
CREATE TABLE IF NOT EXISTS public.diagnoses (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  run_id UUID REFERENCES public.ci_runs(id) ON DELETE CASCADE NOT NULL,
  iteration INT DEFAULT 1 CHECK (iteration IN (1, 2)),
  problem_summary TEXT NOT NULL,
  root_cause TEXT NOT NULL,
  fix_description TEXT NOT NULL,
  fix_type TEXT NOT NULL CHECK (fix_type IN ('safe_auto_apply', 'review_recommended', 'manual_required')),
  confidence FLOAT CHECK (confidence BETWEEN 0.0 AND 1.0),
  is_flaky_test BOOLEAN DEFAULT false,
  category TEXT,
  logs_truncated_warning BOOLEAN DEFAULT false,
  files_changed JSONB,
  github_pr_url TEXT,
  github_pr_number INT,
  verification_status TEXT CHECK (verification_status IN ('verified', 'failed', 'iterating') OR verification_status IS NULL),
  verification_checked_workflows JSONB,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_diagnoses_run_id ON public.diagnoses(run_id);
CREATE INDEX IF NOT EXISTS idx_diagnoses_iteration ON public.diagnoses(run_id, iteration);

-- =========================================================================
-- Agent call log
-- =========================================================================
CREATE TABLE IF NOT EXISTS public.agent_calls (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  run_id UUID REFERENCES public.ci_runs(id) ON DELETE CASCADE,
  call_type TEXT NOT NULL,
  model TEXT NOT NULL,
  input_messages JSONB NOT NULL,
  output_raw TEXT,
  output_parsed JSONB,
  tool_call_valid BOOLEAN DEFAULT true,
  validation_error TEXT,
  input_tokens INT,
  output_tokens INT,
  latency_ms INT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_calls_run_id ON public.agent_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_calls_created_at ON public.agent_calls(created_at DESC);

-- =========================================================================
-- Supabase Realtime
-- =========================================================================
ALTER TABLE public.ci_runs REPLICA IDENTITY FULL;
ALTER TABLE public.diagnoses REPLICA IDENTITY FULL;

-- =========================================================================
-- Row Level Security (backend uses service_role key — bypasses RLS)
-- =========================================================================
ALTER TABLE public.user_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.connected_repos ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ci_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.diagnoses ENABLE ROW LEVEL SECURITY;

CREATE POLICY "users read own profile" ON public.user_profiles
  FOR SELECT USING (true);

CREATE POLICY "users read own repos" ON public.connected_repos
  FOR SELECT USING (true);

CREATE POLICY "users read own ci_runs" ON public.ci_runs
  FOR SELECT USING (true);

CREATE POLICY "users read own diagnoses" ON public.diagnoses
  FOR SELECT USING (true);

-- =========================================================================
-- Helper RPCs
-- =========================================================================
CREATE OR REPLACE FUNCTION store_encrypted_token(
  p_user_id UUID, p_token TEXT, p_key TEXT
) RETURNS VOID AS $$
  UPDATE public.user_profiles
  SET github_access_token_encrypted = pgp_sym_encrypt(p_token, p_key),
      updated_at = now()
  WHERE id = p_user_id;
$$ LANGUAGE SQL SECURITY DEFINER;

CREATE OR REPLACE FUNCTION get_decrypted_token(
  p_user_id UUID, p_key TEXT
) RETURNS TEXT AS $$
  SELECT pgp_sym_decrypt(github_access_token_encrypted, p_key)
  FROM public.user_profiles WHERE id = p_user_id;
$$ LANGUAGE SQL SECURITY DEFINER;

CREATE OR REPLACE FUNCTION check_and_increment_webhook_rate_limit(
  p_repo_id UUID, p_max INT, p_window_seconds INT
) RETURNS JSON AS $$
DECLARE
  v_window_start TIMESTAMPTZ;
  v_count INT;
BEGIN
  SELECT rate_limit_window_start, rate_limit_count
  INTO v_window_start, v_count
  FROM public.connected_repos WHERE id = p_repo_id
  FOR UPDATE;

  IF v_window_start IS NULL OR v_window_start < now() - (p_window_seconds || ' seconds')::INTERVAL THEN
    UPDATE public.connected_repos
    SET rate_limit_window_start = now(), rate_limit_count = 1
    WHERE id = p_repo_id;
    RETURN json_build_object('allowed', true, 'count', 1);
  END IF;

  IF v_count >= p_max THEN
    RETURN json_build_object('allowed', false, 'count', v_count);
  END IF;

  UPDATE public.connected_repos
  SET rate_limit_count = rate_limit_count + 1
  WHERE id = p_repo_id;
  RETURN json_build_object('allowed', true, 'count', v_count + 1);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
