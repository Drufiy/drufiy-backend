ALTER TABLE public.agent_calls
ADD COLUMN IF NOT EXISTS estimated_cost_usd DOUBLE PRECISION;

ALTER TABLE public.agent_calls
ADD COLUMN IF NOT EXISTS diagnosis_outcome TEXT
CHECK (diagnosis_outcome IN ('verified', 'exhausted', 'diagnosis_failed') OR diagnosis_outcome IS NULL);
