-- Fix: agent_calls logging silently failing because these columns don't exist.
-- Run this in the Supabase SQL editor.

ALTER TABLE public.agent_calls ADD COLUMN IF NOT EXISTS estimated_cost_usd DOUBLE PRECISION;
ALTER TABLE public.agent_calls ADD COLUMN IF NOT EXISTS diagnosis_outcome TEXT;
