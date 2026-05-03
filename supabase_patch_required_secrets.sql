-- A3: required_secrets column on diagnoses
-- Stored as JSONB (consistent with files_changed column)
ALTER TABLE public.diagnoses
DROP COLUMN IF EXISTS required_secrets;

ALTER TABLE public.diagnoses
ADD COLUMN IF NOT EXISTS required_secrets JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE INDEX IF NOT EXISTS idx_diagnoses_required_secrets
ON public.diagnoses USING GIN (required_secrets)
WHERE jsonb_array_length(required_secrets) > 0;
