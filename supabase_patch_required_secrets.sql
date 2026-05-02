-- A3: required_secrets column on diagnoses
ALTER TABLE public.diagnoses
ADD COLUMN IF NOT EXISTS required_secrets TEXT[] NOT NULL DEFAULT '{}';

-- Index for quick lookup of environment failures with missing secrets
CREATE INDEX IF NOT EXISTS idx_diagnoses_required_secrets
ON public.diagnoses USING GIN (required_secrets)
WHERE array_length(required_secrets, 1) > 0;
