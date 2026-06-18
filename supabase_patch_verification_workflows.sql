-- Add verification_workflows column that webhook.py and processor.py write to.
-- The old supabase_patch_verification_append.sql created verification_checked_workflows
-- but the code uses verification_workflows — this fixes the mismatch.

ALTER TABLE ci_runs ADD COLUMN IF NOT EXISTS verification_workflows JSONB DEFAULT '[]';
