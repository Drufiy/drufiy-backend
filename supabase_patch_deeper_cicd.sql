-- Session N+4: Deeper CI/CD — flaky test tracking table
-- Run this in Supabase SQL Editor

CREATE TABLE IF NOT EXISTS flaky_tests (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    repo_id UUID NOT NULL REFERENCES connected_repos(id) ON DELETE CASCADE,
    test_file TEXT NOT NULL,
    test_name TEXT,
    fail_count INT DEFAULT 1,
    pass_after_retry_count INT DEFAULT 1,
    last_seen_at TIMESTAMPTZ DEFAULT now(),
    first_seen_at TIMESTAMPTZ DEFAULT now(),
    is_active BOOLEAN DEFAULT true,
    UNIQUE(repo_id, test_file, test_name)
);

CREATE INDEX IF NOT EXISTS idx_flaky_tests_repo ON flaky_tests(repo_id, is_active);
