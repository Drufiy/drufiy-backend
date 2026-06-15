-- Add external_checks_note column to ci_runs
-- Stores a human-readable note when external checks (Vercel, Netlify, etc.)
-- are failing after Prash's CI fix is verified.
ALTER TABLE ci_runs ADD COLUMN IF NOT EXISTS external_checks_note TEXT;
