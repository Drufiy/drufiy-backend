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
