-- Milestone 3 auth/security patch.
-- Run in Supabase SQL Editor to enable JWT logout revocation and GitHub App repo tokens.

ALTER TABLE public.connected_repos
ADD COLUMN IF NOT EXISTS github_app_installation_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_connected_repos_installation_id
ON public.connected_repos(github_app_installation_id);

CREATE TABLE IF NOT EXISTS public.app_installations (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES public.user_profiles(id) ON DELETE CASCADE NOT NULL,
  installation_id BIGINT NOT NULL UNIQUE,
  account_login TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE public.app_installations ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS public.jwt_revocations (
  jti TEXT PRIMARY KEY,
  user_id UUID REFERENCES public.user_profiles(id) ON DELETE CASCADE,
  expires_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_jwt_revocations_expires_at
ON public.jwt_revocations(expires_at);

ALTER TABLE public.jwt_revocations ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION prune_expired_jwt_revocations()
RETURNS INTEGER AS $$
DECLARE
  v_deleted INTEGER;
BEGIN
  DELETE FROM public.jwt_revocations
  WHERE expires_at < now();

  GET DIAGNOSTICS v_deleted = ROW_COUNT;
  RETURN COALESCE(v_deleted, 0);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE OR REPLACE FUNCTION append_verification_workflow(
  p_run_id UUID,
  p_entry JSONB
) RETURNS JSONB AS $$
DECLARE
  v_result JSONB;
BEGIN
  UPDATE public.ci_runs
  SET verification_checked_workflows =
        COALESCE(verification_checked_workflows, '[]'::jsonb) || jsonb_build_array(p_entry),
      updated_at = now()
  WHERE id = p_run_id
  RETURNING verification_checked_workflows INTO v_result;

  RETURN v_result;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
