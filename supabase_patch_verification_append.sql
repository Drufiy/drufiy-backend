-- ── Fix: atomic append to verification_checked_workflows ─────────────────────
-- Replaces the read-modify-write pattern in webhook.py with a single
-- server-side UPDATE that is race-condition-safe.
-- Returns the updated JSONB array so the caller doesn't need a second SELECT.

CREATE OR REPLACE FUNCTION append_verification_workflow(
    p_run_id  UUID,
    p_entry   JSONB
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_result JSONB;
BEGIN
    UPDATE ci_runs
    SET
        verification_checked_workflows =
            COALESCE(verification_checked_workflows, '[]'::jsonb)
            || jsonb_build_array(p_entry),
        updated_at = NOW()
    WHERE id = p_run_id
    RETURNING verification_checked_workflows INTO v_result;

    RETURN v_result;
END;
$$;
