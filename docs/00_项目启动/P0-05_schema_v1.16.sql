-- ResearchPilot v1.16 P0-05 reading outline and revision history.
-- This schema supersedes P0-01 outline_versions/outline_nodes for new data.
-- Existing P0-01 outline rows, if any, must be copied into these tables by the
-- P0-05 migration and retained unchanged until cutover verification completes.

PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS reading_outlines (
    id                  TEXT PRIMARY KEY,
    document_id         TEXT NOT NULL,
    kind                TEXT NOT NULL DEFAULT 'reading_outline' CHECK (
                            kind IN ('reading_outline', 'paper_structure')
                        ),
    current_revision_id TEXT,
    current_revision_no INTEGER NOT NULL DEFAULT 0 CHECK (current_revision_no >= 0),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (document_id, kind),
    FOREIGN KEY (document_id) REFERENCES library_documents(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS outline_ai_runs (
    id                  TEXT PRIMARY KEY,
    outline_id          TEXT NOT NULL,
    base_revision_id    TEXT,
    base_revision_no    INTEGER NOT NULL DEFAULT 0 CHECK (base_revision_no >= 0),
    status              TEXT NOT NULL CHECK (
                            status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')
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
    FOREIGN KEY (outline_id) REFERENCES reading_outlines(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_outline_ai_runs_outline_created
    ON outline_ai_runs(outline_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_outline_ai_runs_status
    ON outline_ai_runs(status, created_at);

CREATE TABLE IF NOT EXISTS outline_revisions (
    id                  TEXT PRIMARY KEY,
    outline_id          TEXT NOT NULL,
    revision_no         INTEGER NOT NULL CHECK (revision_no >= 1),
    base_revision_id    TEXT,
    base_revision_no    INTEGER,
    status              TEXT NOT NULL CHECK (
                            status IN ('applied', 'ai_candidate', 'rejected')
                        ),
    source              TEXT NOT NULL CHECK (
                            source IN ('manual', 'ai', 'ai_merge', 'rollback', 'legacy', 'migration')
                        ),
    tree_json           TEXT NOT NULL CHECK (json_valid(tree_json)),
    markdown            TEXT NOT NULL DEFAULT '',
    change_summary      TEXT NOT NULL DEFAULT '',
    created_by          TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    ai_run_id           TEXT,
    legacy_source_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(legacy_source_json)),
    legacy_source_hash  TEXT NOT NULL DEFAULT '',
    UNIQUE (outline_id, revision_no),
    FOREIGN KEY (outline_id) REFERENCES reading_outlines(id) ON DELETE CASCADE,
    FOREIGN KEY (base_revision_id) REFERENCES outline_revisions(id) ON DELETE SET NULL,
    FOREIGN KEY (ai_run_id) REFERENCES outline_ai_runs(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_outline_revisions_legacy_source
    ON outline_revisions(outline_id, legacy_source_hash)
    WHERE legacy_source_hash <> '';

CREATE INDEX IF NOT EXISTS ix_outline_revisions_outline_revision
    ON outline_revisions(outline_id, revision_no DESC);

CREATE INDEX IF NOT EXISTS ix_outline_revisions_ai_run
    ON outline_revisions(ai_run_id)
    WHERE ai_run_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS trg_reading_outline_current_revision_guard
BEFORE UPDATE OF current_revision_id, current_revision_no ON reading_outlines
WHEN NEW.current_revision_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'current outline revision must belong to the outline and be applied')
    WHERE NOT EXISTS (
        SELECT 1
        FROM outline_revisions r
        WHERE r.id = NEW.current_revision_id
          AND r.outline_id = NEW.id
          AND r.revision_no = NEW.current_revision_no
          AND r.status = 'applied'
    );
END;

CREATE TABLE IF NOT EXISTS outline_node_entities (
    id                  TEXT PRIMARY KEY,
    outline_id          TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    created_by          TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (outline_id) REFERENCES reading_outlines(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_outline_node_entities_outline
    ON outline_node_entities(outline_id);

CREATE TABLE IF NOT EXISTS outline_revision_nodes (
    revision_id         TEXT NOT NULL,
    node_id             TEXT NOT NULL,
    parent_node_id      TEXT,
    order_index         INTEGER NOT NULL CHECK (order_index >= 0),
    heading_level       INTEGER NOT NULL CHECK (heading_level BETWEEN 1 AND 6),
    title               TEXT NOT NULL,
    body_markdown       TEXT NOT NULL DEFAULT '',
    anchor_json         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(anchor_json)),
    note_markdown       TEXT NOT NULL DEFAULT '',
    locked              INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
    user_edited         INTEGER NOT NULL DEFAULT 0 CHECK (user_edited IN (0, 1)),
    origin              TEXT NOT NULL DEFAULT 'ai' CHECK (origin IN ('ai', 'user', 'mixed', 'legacy')),
    content_hash        TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (revision_id, node_id),
    FOREIGN KEY (revision_id) REFERENCES outline_revisions(id) ON DELETE CASCADE,
    FOREIGN KEY (node_id) REFERENCES outline_node_entities(id) ON DELETE CASCADE,
    FOREIGN KEY (revision_id, parent_node_id)
        REFERENCES outline_revision_nodes(revision_id, node_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_outline_revision_nodes_position
    ON outline_revision_nodes(revision_id, COALESCE(parent_node_id, ''), order_index);

CREATE INDEX IF NOT EXISTS ix_outline_revision_nodes_stable_node
    ON outline_revision_nodes(node_id, revision_id);

CREATE TABLE IF NOT EXISTS outline_node_annotations (
    revision_id         TEXT NOT NULL,
    node_id             TEXT NOT NULL,
    annotation_id       TEXT NOT NULL,
    relation_type       TEXT NOT NULL DEFAULT 'evidence' CHECK (
                            relation_type IN ('evidence', 'related', 'follow_up')
                        ),
    created_at          TEXT NOT NULL,
    PRIMARY KEY (revision_id, node_id, annotation_id),
    FOREIGN KEY (revision_id, node_id)
        REFERENCES outline_revision_nodes(revision_id, node_id) ON DELETE CASCADE,
    FOREIGN KEY (annotation_id) REFERENCES annotations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_outline_node_annotations_annotation
    ON outline_node_annotations(annotation_id, revision_id);

CREATE TABLE IF NOT EXISTS project_outline_node_states (
    project_id          TEXT NOT NULL,
    outline_id          TEXT NOT NULL,
    node_id             TEXT NOT NULL,
    completed           INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
    completed_at        TEXT,
    last_viewed_at      TEXT,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (project_id, outline_id, node_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (outline_id) REFERENCES reading_outlines(id) ON DELETE CASCADE,
    FOREIGN KEY (node_id) REFERENCES outline_node_entities(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_project_outline_node_states_outline
    ON project_outline_node_states(project_id, outline_id, completed);

CREATE TABLE IF NOT EXISTS outline_merge_conflicts (
    id                  TEXT PRIMARY KEY,
    outline_id          TEXT NOT NULL,
    ai_run_id           TEXT NOT NULL,
    candidate_revision_id TEXT NOT NULL,
    base_revision_no    INTEGER NOT NULL,
    current_revision_no INTEGER NOT NULL,
    node_id             TEXT,
    conflict_type       TEXT NOT NULL CHECK (
                            conflict_type IN (
                                'both_modified', 'user_deleted_ai_modified',
                                'ai_deleted_user_modified', 'move_vs_content',
                                'anchor_conflict', 'parent_conflict'
                            )
                        ),
    base_node_json      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(base_node_json)),
    current_node_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(current_node_json)),
    ai_node_json        TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(ai_node_json)),
    resolution          TEXT NOT NULL DEFAULT 'pending' CHECK (
                            resolution IN (
                                'pending', 'keep_current', 'accept_ai', 'manual_merge', 'dismissed'
                            )
                        ),
    resolved_by         TEXT NOT NULL DEFAULT '',
    resolved_at         TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    FOREIGN KEY (outline_id) REFERENCES reading_outlines(id) ON DELETE CASCADE,
    FOREIGN KEY (ai_run_id) REFERENCES outline_ai_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (candidate_revision_id) REFERENCES outline_revisions(id) ON DELETE CASCADE,
    FOREIGN KEY (node_id) REFERENCES outline_node_entities(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_outline_merge_conflicts_revision
    ON outline_merge_conflicts(candidate_revision_id, resolution, node_id);

COMMIT;
