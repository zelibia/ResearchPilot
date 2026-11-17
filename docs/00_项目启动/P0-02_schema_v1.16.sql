-- ResearchPilot v1.16 P0-02: screening inbox
-- Depends on P0-01_schema_v1.16.sql and the existing projects table.

PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS import_batches (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    source_surface      TEXT NOT NULL CHECK (
                            source_surface IN ('upload', 'drop', 'clipboard', 'api')
                        ),
    status              TEXT NOT NULL DEFAULT 'open' CHECK (
                            status IN ('open', 'processing', 'completed', 'failed')
                        ),
    created_by          TEXT NOT NULL DEFAULT '',
    note                TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_import_batches_project_created
    ON import_batches(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS import_candidates (
    id                          TEXT PRIMARY KEY,
    batch_id                    TEXT NOT NULL,
    project_id                  TEXT NOT NULL,
    status                      TEXT NOT NULL DEFAULT 'uploading' CHECK (
                                    status IN (
                                        'uploading', 'uploaded', 'extracting', 'ready',
                                        'screening_queued', 'screening', 'screened',
                                        'screening_failed', 'extraction_failed', 'confirming',
                                        'confirmed', 'skipped', 'deleted', 'expired'
                                    )
                                ),
    source_kind                 TEXT NOT NULL CHECK (
                                    source_kind IN ('pdf', 'image', 'doi', 'url', 'manual')
                                ),
    display_title               TEXT NOT NULL DEFAULT '',
    original_filename           TEXT NOT NULL DEFAULT '',
    mime_type                   TEXT NOT NULL DEFAULT '',
    page_count                  INTEGER CHECK (page_count IS NULL OR page_count >= 0),
    language                    TEXT NOT NULL DEFAULT '',
    text_layer_status           TEXT NOT NULL DEFAULT 'missing' CHECK (
                                    text_layer_status IN ('available', 'missing', 'partial')
                                ),
    metadata_json               TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    metadata_provenance_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_provenance_json)),
    metadata_warnings_json      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(metadata_warnings_json)),
    fulltext_char_count         INTEGER NOT NULL DEFAULT 0 CHECK (fulltext_char_count >= 0),
    duplicate_status            TEXT NOT NULL DEFAULT 'none' CHECK (
                                    duplicate_status IN (
                                        'none', 'exact_sha256', 'exact_doi',
                                        'possible_title', 'conflict'
                                    )
                                ),
    duplicate_document_id       TEXT,
    duplicate_basis             TEXT NOT NULL DEFAULT '',
    read_recommendation         TEXT NOT NULL DEFAULT 'pending' CHECK (
                                    read_recommendation IN (
                                        'must_read', 'recommended', 'optional',
                                        'not_recommended', 'insufficient_evidence', 'pending', 'failed'
                                    )
                                ),
    citation_recommendation     TEXT NOT NULL DEFAULT 'pending' CHECK (
                                    citation_recommendation IN (
                                        'strong_candidate', 'candidate', 'optional',
                                        'not_candidate', 'insufficient_evidence', 'pending', 'failed'
                                    )
                                ),
    reading_priority_score      INTEGER CHECK (
                                    reading_priority_score IS NULL OR
                                    reading_priority_score BETWEEN 0 AND 100
                                ),
    confidence_adjusted         REAL CHECK (
                                    confidence_adjusted IS NULL OR
                                    confidence_adjusted BETWEEN 0 AND 1
                                ),
    current_evaluation_id       TEXT,
    confirmed_document_id       TEXT,
    confirmed_project_document_id TEXT,
    error_code                  TEXT NOT NULL DEFAULT '',
    error_message               TEXT NOT NULL DEFAULT '',
    revision                    INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    skipped_until               TEXT,
    purge_after                 TEXT,
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL,
    deleted_at                  TEXT,
    FOREIGN KEY (batch_id) REFERENCES import_batches(id) ON DELETE CASCADE,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (duplicate_document_id) REFERENCES library_documents(id) ON DELETE SET NULL,
    FOREIGN KEY (confirmed_document_id) REFERENCES library_documents(id) ON DELETE SET NULL,
    FOREIGN KEY (confirmed_project_document_id) REFERENCES project_documents(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_import_candidates_queue
    ON import_candidates(project_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS ix_import_candidates_batch
    ON import_candidates(batch_id, created_at);

CREATE INDEX IF NOT EXISTS ix_import_candidates_duplicate
    ON import_candidates(duplicate_document_id)
    WHERE duplicate_document_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_import_candidates_purge
    ON import_candidates(purge_after)
    WHERE deleted_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS candidate_files (
    id                  TEXT PRIMARY KEY,
    candidate_id        TEXT NOT NULL,
    role                TEXT NOT NULL CHECK (role IN ('source_file', 'ocr_text', 'thumbnail')),
    file_name           TEXT NOT NULL,
    storage_key         TEXT NOT NULL,
    mime_type           TEXT NOT NULL DEFAULT '',
    sha256              TEXT NOT NULL,
    size_bytes          INTEGER NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
    page_count          INTEGER CHECK (page_count IS NULL OR page_count >= 0),
    text_layer_status   TEXT NOT NULL DEFAULT 'missing' CHECK (
                            text_layer_status IN ('available', 'missing', 'partial')
                        ),
    extraction_status   TEXT NOT NULL DEFAULT 'pending' CHECK (
                            extraction_status IN (
                                'pending', 'running', 'done', 'partial', 'failed', 'not_applicable'
                            )
                        ),
    extraction_error    TEXT NOT NULL DEFAULT '',
    promoted_file_asset_id TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    deleted_at          TEXT,
    UNIQUE (candidate_id, role, sha256),
    FOREIGN KEY (candidate_id) REFERENCES import_candidates(id) ON DELETE CASCADE,
    FOREIGN KEY (promoted_file_asset_id) REFERENCES file_assets(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_candidate_files_sha
    ON candidate_files(sha256);

CREATE TABLE IF NOT EXISTS candidate_duplicate_matches (
    id                  TEXT PRIMARY KEY,
    candidate_id        TEXT NOT NULL,
    document_id         TEXT NOT NULL,
    match_basis         TEXT NOT NULL CHECK (
                            match_basis IN ('sha256', 'doi', 'title', 'manual')
                        ),
    match_status        TEXT NOT NULL CHECK (
                            match_status IN ('exact', 'possible', 'conflict')
                        ),
    title_similarity    REAL CHECK (title_similarity IS NULL OR title_similarity BETWEEN 0 AND 1),
    detail_json         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
    created_at          TEXT NOT NULL,
    UNIQUE (candidate_id, document_id, match_basis),
    FOREIGN KEY (candidate_id) REFERENCES import_candidates(id) ON DELETE CASCADE,
    FOREIGN KEY (document_id) REFERENCES library_documents(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_candidate_duplicate_matches_candidate
    ON candidate_duplicate_matches(candidate_id, match_status);

CREATE TABLE IF NOT EXISTS candidate_ai_runs (
    id                  TEXT PRIMARY KEY,
    candidate_id        TEXT NOT NULL,
    attempt_no          INTEGER NOT NULL CHECK (attempt_no >= 1),
    status              TEXT NOT NULL CHECK (
                            status IN (
                                'queued', 'running', 'succeeded', 'failed',
                                'timed_out', 'cancelled', 'superseded'
                            )
                        ),
    provider            TEXT NOT NULL DEFAULT '',
    model               TEXT NOT NULL DEFAULT '',
    prompt_version      TEXT NOT NULL,
    input_hash          TEXT NOT NULL,
    input_scope_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(input_scope_json)),
    project_context_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(project_context_json)),
    input_preview_md    TEXT NOT NULL DEFAULT '',
    started_at          TEXT,
    finished_at         TEXT,
    elapsed_ms          INTEGER,
    lease_owner         TEXT NOT NULL DEFAULT '',
    lease_expires_at    TEXT,
    error_code          TEXT NOT NULL DEFAULT '',
    error_message       TEXT NOT NULL DEFAULT '',
    usage_json          TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(usage_json)),
    raw_response_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(raw_response_json)),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (candidate_id, attempt_no),
    FOREIGN KEY (candidate_id) REFERENCES import_candidates(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_candidate_ai_runs_queue
    ON candidate_ai_runs(status, created_at);

CREATE INDEX IF NOT EXISTS ix_candidate_ai_runs_lease
    ON candidate_ai_runs(status, lease_expires_at);

CREATE INDEX IF NOT EXISTS ix_candidate_ai_runs_candidate
    ON candidate_ai_runs(candidate_id, created_at DESC);

CREATE TABLE IF NOT EXISTS candidate_ai_evaluations (
    id                      TEXT PRIMARY KEY,
    candidate_id            TEXT NOT NULL,
    run_id                  TEXT NOT NULL UNIQUE,
    schema_version          TEXT NOT NULL,
    topic_match_score       INTEGER NOT NULL CHECK (topic_match_score BETWEEN 0 AND 100),
    academic_value_score    INTEGER NOT NULL CHECK (academic_value_score BETWEEN 0 AND 100),
    citation_potential_score INTEGER NOT NULL CHECK (citation_potential_score BETWEEN 0 AND 100),
    reading_priority_score  INTEGER NOT NULL CHECK (reading_priority_score BETWEEN 0 AND 100),
    read_recommendation     TEXT NOT NULL CHECK (
                                read_recommendation IN (
                                    'must_read', 'recommended', 'optional',
                                    'not_recommended', 'insufficient_evidence'
                                )
                            ),
    citation_recommendation TEXT NOT NULL CHECK (
                                citation_recommendation IN (
                                    'strong_candidate', 'candidate', 'optional',
                                    'not_candidate', 'insufficient_evidence'
                                )
                            ),
    confidence_raw          REAL NOT NULL CHECK (confidence_raw BETWEEN 0 AND 1),
    confidence_adjusted     REAL NOT NULL CHECK (confidence_adjusted BETWEEN 0 AND 1),
    confidence_label        TEXT NOT NULL CHECK (confidence_label IN ('high', 'medium', 'low')),
    input_coverage          TEXT NOT NULL CHECK (
                                input_coverage IN ('fulltext', 'abstract', 'metadata_only')
                            ),
    evidence_status         TEXT NOT NULL CHECK (
                                evidence_status IN ('verified', 'partial', 'none')
                            ),
    conclusion_summary      TEXT NOT NULL,
    result_json             TEXT NOT NULL CHECK (json_valid(result_json)),
    created_at              TEXT NOT NULL,
    FOREIGN KEY (candidate_id) REFERENCES import_candidates(id) ON DELETE CASCADE,
    FOREIGN KEY (run_id) REFERENCES candidate_ai_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_candidate_ai_evaluations_candidate
    ON candidate_ai_evaluations(candidate_id, created_at DESC);

CREATE TABLE IF NOT EXISTS candidate_evaluation_reasons (
    id                  TEXT PRIMARY KEY,
    evaluation_id       TEXT NOT NULL,
    dimension           TEXT NOT NULL CHECK (
                            dimension IN (
                                'topic_match', 'academic_value',
                                'citation_potential', 'reading_priority'
                            )
                        ),
    polarity            TEXT NOT NULL CHECK (polarity IN ('positive', 'negative', 'risk', 'neutral')),
    reason_text         TEXT NOT NULL,
    source_kind         TEXT NOT NULL CHECK (source_kind IN ('metadata', 'abstract', 'fulltext')),
    evidence_page_index INTEGER,
    evidence_quote      TEXT NOT NULL DEFAULT '',
    match_status        TEXT NOT NULL DEFAULT 'none' CHECK (
                            match_status IN ('exact', 'partial', 'none', 'no_text_layer')
                        ),
    created_at          TEXT NOT NULL,
    FOREIGN KEY (evaluation_id) REFERENCES candidate_ai_evaluations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_candidate_evaluation_reasons_eval
    ON candidate_evaluation_reasons(evaluation_id, dimension);

CREATE TABLE IF NOT EXISTS candidate_decisions (
    id                  TEXT PRIMARY KEY,
    candidate_id        TEXT NOT NULL,
    evaluation_id       TEXT,
    decision            TEXT NOT NULL CHECK (
                            decision IN (
                                'confirmed_store', 'confirmed_deep_read',
                                'skipped', 'deleted'
                            )
                        ),
    read_intent         INTEGER NOT NULL DEFAULT 0 CHECK (read_intent IN (0, 1)),
    cite_intent         INTEGER NOT NULL DEFAULT 0 CHECK (cite_intent IN (0, 1)),
    note                TEXT NOT NULL DEFAULT '',
    actor_id            TEXT NOT NULL DEFAULT '',
    idempotency_key     TEXT NOT NULL UNIQUE,
    document_id         TEXT,
    project_document_id TEXT,
    created_at          TEXT NOT NULL,
    FOREIGN KEY (candidate_id) REFERENCES import_candidates(id) ON DELETE CASCADE,
    FOREIGN KEY (evaluation_id) REFERENCES candidate_ai_evaluations(id) ON DELETE SET NULL,
    FOREIGN KEY (document_id) REFERENCES library_documents(id) ON DELETE SET NULL,
    FOREIGN KEY (project_document_id) REFERENCES project_documents(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_candidate_decisions_candidate_created
    ON candidate_decisions(candidate_id, created_at DESC);

CREATE TABLE IF NOT EXISTS candidate_operation_logs (
    id                  TEXT PRIMARY KEY,
    candidate_id        TEXT NOT NULL,
    action              TEXT NOT NULL,
    from_status         TEXT,
    to_status           TEXT NOT NULL,
    actor_id            TEXT NOT NULL DEFAULT '',
    detail_json         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
    created_at          TEXT NOT NULL,
    FOREIGN KEY (candidate_id) REFERENCES import_candidates(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_candidate_operation_logs_candidate
    ON candidate_operation_logs(candidate_id, created_at);

COMMIT;
