-- ResearchPilot v1.16 P0-06 project summary and version history.
-- Depends on P0-01, P0-04, and P0-05 schemas.
-- This schema supersedes P0-01 summary_versions/sections for new data.

PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS project_summaries (
    id                          TEXT PRIMARY KEY,
    project_id                  TEXT NOT NULL UNIQUE,
    current_official_revision_id TEXT,
    current_revision_no          INTEGER NOT NULL DEFAULT 0 CHECK (current_revision_no >= 0),
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS summary_ai_runs (
    id                  TEXT PRIMARY KEY,
    summary_id          TEXT NOT NULL,
    mode                TEXT NOT NULL CHECK (mode IN ('independent', 'incremental')),
    base_revision_id    TEXT,
    base_revision_no    INTEGER,
    scope_type          TEXT NOT NULL CHECK (
                            scope_type IN ('single_document', 'multi_document', 'day', 'project')
                        ),
    scope_json          TEXT NOT NULL CHECK (json_valid(scope_json)),
    status              TEXT NOT NULL CHECK (
                            status IN ('queued', 'running', 'partial', 'succeeded', 'failed', 'cancelled')
                        ),
    provider            TEXT NOT NULL DEFAULT '',
    model               TEXT NOT NULL DEFAULT '',
    prompt_version      TEXT NOT NULL,
    input_hash          TEXT NOT NULL,
    input_scope_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(input_scope_json)),
    result_json         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(result_json)),
    error_code          TEXT NOT NULL DEFAULT '',
    error_message       TEXT NOT NULL DEFAULT '',
    requested_by        TEXT NOT NULL DEFAULT '',
    started_at          TEXT,
    finished_at         TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (summary_id) REFERENCES project_summaries(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_summary_ai_runs_summary_created
    ON summary_ai_runs(summary_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_summary_ai_runs_status
    ON summary_ai_runs(status, created_at);

CREATE TABLE IF NOT EXISTS summary_revisions (
    id                  TEXT PRIMARY KEY,
    summary_id          TEXT NOT NULL,
    revision_no         INTEGER NOT NULL CHECK (revision_no >= 1),
    mode                TEXT NOT NULL CHECK (mode IN ('independent', 'incremental')),
    base_revision_id    TEXT,
    base_revision_no    INTEGER,
    status              TEXT NOT NULL CHECK (status IN ('candidate', 'official', 'rejected')),
    source              TEXT NOT NULL CHECK (
                            source IN ('ai', 'manual', 'rollback', 'legacy', 'migration')
                        ),
    scope_type          TEXT NOT NULL CHECK (
                            scope_type IN ('single_document', 'multi_document', 'day', 'project')
                        ),
    scope_json          TEXT NOT NULL CHECK (json_valid(scope_json)),
    title               TEXT NOT NULL DEFAULT '',
    markdown            TEXT NOT NULL DEFAULT '',
    structured_json     TEXT NOT NULL CHECK (json_valid(structured_json)),
    change_summary      TEXT NOT NULL DEFAULT '',
    content_hash        TEXT NOT NULL,
    created_by          TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    confirmed_by        TEXT NOT NULL DEFAULT '',
    confirmed_at        TEXT,
    ai_run_id           TEXT,
    legacy_source_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(legacy_source_json)),
    legacy_source_hash  TEXT NOT NULL DEFAULT '',
    UNIQUE (summary_id, revision_no),
    FOREIGN KEY (summary_id) REFERENCES project_summaries(id) ON DELETE CASCADE,
    FOREIGN KEY (base_revision_id) REFERENCES summary_revisions(id) ON DELETE SET NULL,
    FOREIGN KEY (ai_run_id) REFERENCES summary_ai_runs(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_summary_revisions_legacy_source
    ON summary_revisions(summary_id, legacy_source_hash)
    WHERE legacy_source_hash <> '';

CREATE INDEX IF NOT EXISTS ix_summary_revisions_summary_revision
    ON summary_revisions(summary_id, revision_no DESC);

CREATE TRIGGER IF NOT EXISTS trg_project_summary_current_revision_guard
BEFORE UPDATE OF current_official_revision_id, current_revision_no ON project_summaries
WHEN NEW.current_official_revision_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'current summary revision must belong to the summary and be official')
    WHERE NOT EXISTS (
        SELECT 1
        FROM summary_revisions r
        WHERE r.id = NEW.current_official_revision_id
          AND r.summary_id = NEW.id
          AND r.revision_no = NEW.current_revision_no
          AND r.status = 'official'
    );
END;

CREATE TABLE IF NOT EXISTS summary_section_entities (
    id                  TEXT PRIMARY KEY,
    summary_id          TEXT NOT NULL,
    stable_key          TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    UNIQUE (summary_id, stable_key),
    FOREIGN KEY (summary_id) REFERENCES project_summaries(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_summary_section_entities_summary
    ON summary_section_entities(summary_id, stable_key);

CREATE TABLE IF NOT EXISTS summary_revision_sections (
    revision_id         TEXT NOT NULL,
    section_id          TEXT NOT NULL,
    parent_section_id   TEXT,
    stable_key          TEXT NOT NULL,
    order_index         INTEGER NOT NULL CHECK (order_index >= 0),
    title               TEXT NOT NULL,
    body_markdown       TEXT NOT NULL DEFAULT '',
    change_status       TEXT CHECK (
                            change_status IS NULL OR change_status IN (
                                'added', 'changed', 'unchanged',
                                'removed', 'insufficient_evidence'
                            )
                        ),
    base_section_id     TEXT,
    content_hash        TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (revision_id, section_id),
    UNIQUE (revision_id, stable_key),
    FOREIGN KEY (revision_id) REFERENCES summary_revisions(id) ON DELETE CASCADE,
    FOREIGN KEY (section_id) REFERENCES summary_section_entities(id) ON DELETE CASCADE,
    FOREIGN KEY (revision_id, parent_section_id)
        REFERENCES summary_revision_sections(revision_id, section_id) ON DELETE CASCADE,
    FOREIGN KEY (base_section_id) REFERENCES summary_section_entities(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_summary_revision_sections_position
    ON summary_revision_sections(revision_id, COALESCE(parent_section_id, ''), order_index);

CREATE TABLE IF NOT EXISTS summary_revision_documents (
    revision_id         TEXT NOT NULL,
    document_id         TEXT NOT NULL,
    inclusion_status    TEXT NOT NULL CHECK (inclusion_status IN ('included', 'excluded')),
    exclusion_reason    TEXT NOT NULL DEFAULT '',
    file_sha256         TEXT NOT NULL DEFAULT '',
    outline_revision_id TEXT,
    outline_revision_no INTEGER,
    annotation_count    INTEGER NOT NULL DEFAULT 0 CHECK (annotation_count >= 0),
    source_updated_at   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (revision_id, document_id),
    FOREIGN KEY (revision_id) REFERENCES summary_revisions(id) ON DELETE CASCADE,
    FOREIGN KEY (document_id) REFERENCES library_documents(id) ON DELETE RESTRICT,
    FOREIGN KEY (outline_revision_id) REFERENCES outline_revisions(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_summary_revision_documents_document
    ON summary_revision_documents(document_id, revision_id);

CREATE TABLE IF NOT EXISTS summary_revision_section_evidence (
    id                  TEXT PRIMARY KEY,
    revision_id         TEXT NOT NULL,
    section_id          TEXT NOT NULL,
    evidence_token      TEXT NOT NULL,
    document_id         TEXT NOT NULL,
    outline_node_id     TEXT,
    annotation_id       TEXT,
    claim_id            TEXT,
    anchor_json         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(anchor_json)),
    quote               TEXT NOT NULL DEFAULT '',
    source_label        TEXT NOT NULL DEFAULT '',
    evidence_status     TEXT NOT NULL CHECK (
                            evidence_status IN (
                                'exact', 'partial', 'none', 'insufficient'
                            )
                        ),
    created_at          TEXT NOT NULL,
    UNIQUE (revision_id, section_id, evidence_token),
    FOREIGN KEY (revision_id, section_id)
        REFERENCES summary_revision_sections(revision_id, section_id) ON DELETE CASCADE,
    FOREIGN KEY (document_id) REFERENCES library_documents(id) ON DELETE RESTRICT,
    FOREIGN KEY (outline_node_id) REFERENCES outline_node_entities(id) ON DELETE SET NULL,
    FOREIGN KEY (annotation_id) REFERENCES annotations(id) ON DELETE SET NULL,
    FOREIGN KEY (claim_id) REFERENCES paper_claims(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_summary_revision_section_evidence_document
    ON summary_revision_section_evidence(revision_id, document_id);

CREATE INDEX IF NOT EXISTS ix_summary_revision_section_evidence_outline
    ON summary_revision_section_evidence(outline_node_id, revision_id);

CREATE INDEX IF NOT EXISTS ix_summary_revision_section_evidence_annotation
    ON summary_revision_section_evidence(annotation_id, revision_id);

CREATE TABLE IF NOT EXISTS summary_operation_logs (
    id                  TEXT PRIMARY KEY,
    summary_id          TEXT NOT NULL,
    revision_id         TEXT,
    action              TEXT NOT NULL CHECK (
                            action IN (
                                'generated', 'edited', 'confirmed', 'rejected', 'rolled_back'
                            )
                        ),
    actor_id            TEXT NOT NULL DEFAULT '',
    detail_json         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
    created_at          TEXT NOT NULL,
    FOREIGN KEY (summary_id) REFERENCES project_summaries(id) ON DELETE CASCADE,
    FOREIGN KEY (revision_id) REFERENCES summary_revisions(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_summary_operation_logs_summary
    ON summary_operation_logs(summary_id, created_at DESC);

COMMIT;
