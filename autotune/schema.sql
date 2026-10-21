-- Autotune evidence and job store, schema version 3.
-- Git holds harness file history. These tables hold observations, decisions and referenced SHAs.

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
    episode_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    lifecycle_id TEXT NOT NULL,
    lifecycle_source TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin = 'foreground'),
    task_id TEXT,
    started_at TEXT,
    completed_at TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    end_reason TEXT NOT NULL CHECK (end_reason IN ('answer', 'aborted', 'refusal', 'error', 'unknown')),
    duration_ms INTEGER,
    model TEXT,
    transcript_path TEXT,
    repository_json TEXT NOT NULL,
    loaded_harness_json TEXT NOT NULL,
    coverage_json TEXT NOT NULL,
    usage_json TEXT,
    review_status TEXT NOT NULL DEFAULT 'pending',
    UNIQUE (session_id, turn_id)
);

CREATE INDEX IF NOT EXISTS episodes_completed ON episodes (completed_at);

CREATE TABLE IF NOT EXISTS evidence (
    episode_id TEXT NOT NULL REFERENCES episodes (episode_id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    content_sha256 TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    truncated INTEGER NOT NULL DEFAULT 0,
    retain_until TEXT NOT NULL,
    expired_at TEXT,
    PRIMARY KEY (episode_id, evidence_id)
);

CREATE INDEX IF NOT EXISTS evidence_retain ON evidence (retain_until) WHERE expired_at IS NULL;

CREATE TABLE IF NOT EXISTS foreground_activity (
    session_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('active', 'idle', 'ended')),
    turn_id TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capture_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    turn_id TEXT,
    code TEXT NOT NULL,
    detail TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL CHECK (job_type IN ('review', 'propose', 'evaluate', 'consolidate', 'apply', 'verify_active', 'reproduce_regression')),
    dedup_key TEXT NOT NULL UNIQUE,
    episode_id TEXT REFERENCES episodes (episode_id) ON DELETE CASCADE,
    candidate_id TEXT REFERENCES candidates (candidate_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued', 'leased', 'succeeded', 'retry_wait', 'blocked_budget', 'blocked_auth', 'blocked_environment', 'failed', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    next_eligible_at TEXT NOT NULL,
    last_error TEXT,
    last_error_detail TEXT,
    result_json TEXT,
    cancel_requested_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS jobs_dispatch ON jobs (state, priority, id);

CREATE TABLE IF NOT EXISTS model_calls (
    call_id TEXT PRIMARY KEY,
    job_id INTEGER REFERENCES jobs (id) ON DELETE SET NULL,
    attempt INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('review', 'author', 'judge', 'curator', 'replay')),
    requested_model TEXT,
    resolved_model TEXT,
    limits_json TEXT NOT NULL DEFAULT '{}',
    usage_json TEXT,
    usage_uncertain INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    error_code TEXT,
    cost_usd REAL,
    duration_ms INTEGER,
    cli_session_id TEXT
);

CREATE INDEX IF NOT EXISTS model_calls_day ON model_calls (role, started_at);

CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes (episode_id) ON DELETE CASCADE,
    job_id INTEGER REFERENCES jobs (id) ON DELETE SET NULL,
    call_id TEXT REFERENCES model_calls (call_id) ON DELETE SET NULL,
    task_stage TEXT NOT NULL,
    review_json TEXT NOT NULL,
    recommendation TEXT NOT NULL CHECK (recommendation IN ('no_change', 'investigate', 'propose_candidate')),
    reviewer_model TEXT,
    rubric_commit TEXT,
    input_truncated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'valid' CHECK (status IN ('valid', 'rejected')),
    adjustments_json TEXT NOT NULL DEFAULT '[]',
    input_evidence_json TEXT NOT NULL DEFAULT '[]',
    omitted_evidence_json TEXT NOT NULL DEFAULT '[]',
    failure_signature TEXT,
    session_id TEXT,
    UNIQUE (episode_id, job_id)
);

CREATE INDEX IF NOT EXISTS reviews_signature ON reviews (failure_signature);

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    source_reviews_json TEXT NOT NULL DEFAULT '[]',
    hypothesis TEXT NOT NULL,
    effect_metric TEXT NOT NULL,
    allowed_paths_json TEXT NOT NULL,
    change_kind TEXT NOT NULL CHECK (change_kind IN ('instruction', 'skill', 'consolidation', 'hook_script', 'tool_config', 'revert')),
    branch TEXT,
    base_commit TEXT NOT NULL,
    candidate_commit TEXT,
    applied_commit TEXT,
    reverted_commit TEXT,
    evaluator_commit TEXT,
    policy_digest TEXT,
    required_cases_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('proposed', 'editing', 'validating', 'evaluating', 'rejected', 'inconclusive', 'accepted', 'waiting_to_apply', 'stale', 'applied', 'observed_active', 'regression_suspected', 'reverted', 'needs_scope_change')),
    reason TEXT,
    failure_signature TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    worktree_path TEXT,
    author_call_id TEXT,
    author_summary TEXT,
    validation_json TEXT NOT NULL DEFAULT '{}',
    source_sessions_json TEXT NOT NULL DEFAULT '[]',
    gate_json TEXT,
    target_branch TEXT,
    task_family TEXT,
    applied_at TEXT,
    reverted_at TEXT,
    branch_deleted_at TEXT,
    exposure TEXT
);

CREATE INDEX IF NOT EXISTS candidates_signature ON candidates (failure_signature);
CREATE INDEX IF NOT EXISTS candidates_status ON candidates (status);

CREATE TABLE IF NOT EXISTS evaluation_runs (
    run_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates (candidate_id) ON DELETE CASCADE,
    case_id TEXT NOT NULL,
    arm TEXT NOT NULL CHECK (arm IN ('baseline', 'candidate')),
    display_label TEXT,
    trial INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    fixture_digest TEXT,
    harness_commit TEXT NOT NULL,
    model TEXT,
    cli_build TEXT,
    environment_json TEXT NOT NULL DEFAULT '{}',
    outcome TEXT CHECK (outcome IN ('success', 'task_failure', 'constraint_violation', 'timeout', 'infrastructure_error', 'invalid_output', 'ungradeable')),
    checks_json TEXT NOT NULL DEFAULT '{}',
    judge_json TEXT,
    usage_json TEXT,
    artifacts_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_code TEXT,
    display_order INTEGER,
    duration_ms INTEGER,
    UNIQUE (candidate_id, case_id, arm, trial, attempt)
);

CREATE TABLE IF NOT EXISTS skill_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id TEXT REFERENCES episodes (episode_id) ON DELETE CASCADE,
    skill TEXT NOT NULL,
    source TEXT,
    path TEXT,
    event TEXT NOT NULL CHECK (event IN ('load', 'invocation', 'edit', 'merge', 'outcome')),
    candidate_id TEXT REFERENCES candidates (candidate_id) ON DELETE SET NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    occurred_at TEXT NOT NULL
);
