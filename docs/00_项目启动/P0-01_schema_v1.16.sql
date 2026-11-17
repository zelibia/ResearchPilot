-- ResearchPilot v1.16 P0-01 target schema
-- SQLite 3.35+ (JSON1 + partial indexes are required)
-- This file only creates/extends the v1.16 data-model tables. It does not
-- mutate the legacy projects/papers/... tables.

PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

-- ---------------------------------------------------------------------------
-- Migration audit and idempotency
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS migration_runs (
    id                  TEXT PRIMARY KEY,
    schema_from         TEXT NOT NULL DEFAULT '',
    schema_to           TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (
                            status IN (
                                'pending', 'preflight', 'backed_up', 'schema_created',
                                'documents_built', 'memberships_built', 'files_built',
                                'derived_backfilled', 'validated', 'cutover_ready',
                                'committed', 'failed'
                            )
                        ),
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    backup_path         TEXT NOT NULL DEFAULT '',
    backup_sha256       TEXT NOT NULL DEFAULT '',
    source_counts_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(source_counts_json)),
    result_counts_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(result_counts_json)),
    error_json          TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(error_json)),
    note                TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_migration_runs_status_started
    ON migration_runs(status, started_at DESC);

CREATE TABLE IF NOT EXISTS migration_id_map (
    run_id              TEXT NOT NULL,
    entity_type         TEXT NOT NULL,
    legacy_table        TEXT NOT NULL,
    legacy_id           TEXT NOT NULL,
    new_id              TEXT NOT NULL,
    match_basis         TEXT NOT NULL CHECK (
                            match_basis IN (
                                'sha256', 'doi', 'title', 'legacy_id',
                                'embedded', 'manual_override'
                            )
                        ),
    created_at          TEXT NOT NULL,
    PRIMARY KEY (run_id, entity_type, legacy_table, legacy_id),
    FOREIGN KEY (run_id) REFERENCES migration_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_migration_id_map_new
    ON migration_id_map(entity_type, new_id);

CREATE TABLE IF NOT EXISTS migration_conflicts (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,
    kind                TEXT NOT NULL CHECK (
                            kind IN (
                                'doi_title_mismatch',
                                'same_file_different_doi',
                                'title_collision',
                                'ambiguous_title_match',
                                'metadata_conflict',
                                'missing_file',
                                'invalid_outline',
                                'invalid_annotation',
                                'unknown_legacy_shape'
                            )
                        ),
    legacy_ids_json     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(legacy_ids_json)),
    document_ids_json   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(document_ids_json)),
    detail_json         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
    resolution          TEXT NOT NULL DEFAULT 'pending' CHECK (
                            resolution IN ('pending', 'auto_resolved', 'manual_resolved', 'kept_separate')
                        ),
    resolved_by         TEXT NOT NULL DEFAULT '',
    resolved_at         TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES migration_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_migration_conflicts_run_resolution
    ON migration_conflicts(run_id, resolution, kind);

CREATE TABLE IF NOT EXISTS migration_unmapped_records (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,
    legacy_table        TEXT NOT NULL,
    legacy_id           TEXT NOT NULL,
    payload_json        TEXT NOT NULL CHECK (json_valid(payload_json)),
    reason              TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    UNIQUE (run_id, legacy_table, legacy_id),
    FOREIGN KEY (run_id) REFERENCES migration_runs(id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Canonical library document
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS library_documents (
    id                      TEXT PRIMARY KEY,
    identity_key            TEXT NOT NULL UNIQUE,
    doi_key                 TEXT,
    title_key               TEXT,
    entry_type              TEXT NOT NULL DEFAULT 'other',
    title                   TEXT NOT NULL DEFAULT '',
    title_raw               TEXT NOT NULL DEFAULT '',
    title_source            TEXT NOT NULL DEFAULT '',
    authors_json            TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(authors_json)),
    authors_raw             TEXT NOT NULL DEFAULT '',
    year                    TEXT NOT NULL DEFAULT '',
    venue                   TEXT NOT NULL DEFAULT '',
    volume                  TEXT NOT NULL DEFAULT '',
    issue                   TEXT NOT NULL DEFAULT '',
    pages                   TEXT NOT NULL DEFAULT '',
    publisher               TEXT NOT NULL DEFAULT '',
    abstract                TEXT NOT NULL DEFAULT '',
    language                TEXT NOT NULL DEFAULT '',
    source_urls_json        TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(source_urls_json)),
    external_ids_json       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(external_ids_json)),
    external_metadata_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(external_metadata_json)),
    metadata_checks_json    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(metadata_checks_json)),
    guess_sources_json      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(guess_sources_json)),
    parse_warnings_json     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(parse_warnings_json)),
    verification_status     TEXT NOT NULL DEFAULT 'unverified' CHECK (
                                verification_status IN (
                                    'unverified', 'verified', 'partial', 'conflict'
                                )
                            ),
    verified_at             TEXT NOT NULL DEFAULT '',
    legacy_created_at       TEXT NOT NULL DEFAULT '',
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    deleted_at              TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_library_documents_doi_key
    ON library_documents(doi_key)
    WHERE doi_key IS NOT NULL AND doi_key <> '';

CREATE INDEX IF NOT EXISTS ix_library_documents_title_key
    ON library_documents(title_key)
    WHERE title_key IS NOT NULL AND title_key <> '';

CREATE INDEX IF NOT EXISTS ix_library_documents_year
    ON library_documents(year);

-- One row per distinct observed value/source. The canonical value lives in
-- library_documents; this table keeps field provenance and merge history.
CREATE TABLE IF NOT EXISTS document_field_values (
    id                  TEXT PRIMARY KEY,
    document_id         TEXT NOT NULL,
    field_name          TEXT NOT NULL,
    value_json          TEXT NOT NULL CHECK (json_valid(value_json)),
    value_hash          TEXT NOT NULL,
    source              TEXT NOT NULL CHECK (
                            source IN (
                                'pdf_embedded', 'first_page', 'filename', 'body',
                                'crossref', 'openalex', 'ocr', 'manual', 'legacy'
                            )
                        ),
    source_record_id    TEXT NOT NULL DEFAULT '',
    confidence          REAL,
    is_current          INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0, 1)),
    created_at          TEXT NOT NULL,
    UNIQUE (document_id, field_name, value_hash, source),
    FOREIGN KEY (document_id) REFERENCES library_documents(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_document_field_values_current
    ON document_field_values(document_id, field_name, is_current);

CREATE UNIQUE INDEX IF NOT EXISTS ux_document_field_values_one_current
    ON document_field_values(document_id, field_name)
    WHERE is_current = 1;

-- ---------------------------------------------------------------------------
-- Project membership and organization
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS project_documents (
    id                          TEXT PRIMARY KEY,
    project_id                  TEXT NOT NULL,
    document_id                 TEXT NOT NULL,
    origin                      TEXT NOT NULL CHECK (
                                    origin IN (
                                        'upload', 'screenshot', 'doi', 'url',
                                        'library', 'citation_candidate', 'legacy'
                                    )
                                ),
    reading_state               TEXT NOT NULL DEFAULT 'not_planned' CHECK (
                                    reading_state IN (
                                        'not_planned', 'to_read', 'reading',
                                        'outline_draft', 'outline_confirmed',
                                        'synthesis_ready', 'archived'
                                    )
                                ),
    cite_candidate              INTEGER NOT NULL DEFAULT 0 CHECK (cite_candidate IN (0, 1)),
    project_note                TEXT NOT NULL DEFAULT '',
    added_by                    TEXT NOT NULL DEFAULT '',
    added_at                    TEXT NOT NULL,
    updated_at                  TEXT NOT NULL,
    last_read_page_index        INTEGER CHECK (last_read_page_index IS NULL OR last_read_page_index >= 0),
    last_read_outline_node_id   TEXT,
    legacy_paper_id             TEXT NOT NULL DEFAULT '',
    legacy_paper_ids_json       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(legacy_paper_ids_json)),
    legacy_payload_json         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(legacy_payload_json)),
    deleted_at                  TEXT,
    UNIQUE (project_id, document_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (document_id) REFERENCES library_documents(id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_project_documents_legacy_paper
    ON project_documents(legacy_paper_id)
    WHERE legacy_paper_id <> '';

CREATE INDEX IF NOT EXISTS ix_project_documents_project_state
    ON project_documents(project_id, reading_state, updated_at DESC);

CREATE INDEX IF NOT EXISTS ix_project_documents_document
    ON project_documents(document_id);

CREATE TABLE IF NOT EXISTS collections (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    parent_id           TEXT,
    name                TEXT NOT NULL,
    name_key            TEXT NOT NULL,
    kind                TEXT NOT NULL DEFAULT 'manual' CHECK (kind IN ('manual', 'smart')),
    rule_json           TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(rule_json)),
    order_index         INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES collections(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_collections_project_parent_name
    ON collections(project_id, COALESCE(parent_id, ''), name_key);

CREATE TABLE IF NOT EXISTS collection_documents (
    collection_id       TEXT NOT NULL,
    document_id         TEXT NOT NULL,
    added_at            TEXT NOT NULL,
    PRIMARY KEY (collection_id, document_id),
    FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE,
    FOREIGN KEY (document_id) REFERENCES library_documents(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_collection_documents_document
    ON collection_documents(document_id);

CREATE TABLE IF NOT EXISTS tags (
    id                  TEXT PRIMARY KEY,
    tag_key             TEXT NOT NULL UNIQUE,
    display_name        TEXT NOT NULL,
    color               TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_tags (
    document_id         TEXT NOT NULL,
    tag_id              TEXT NOT NULL,
    source              TEXT NOT NULL DEFAULT 'user' CHECK (source IN ('user', 'import', 'ai')),
    confidence          REAL,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (document_id, tag_id),
    FOREIGN KEY (document_id) REFERENCES library_documents(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_document_tags_tag
    ON document_tags(tag_id, document_id);

-- ---------------------------------------------------------------------------
-- Files
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS file_assets (
    id                  TEXT PRIMARY KEY,
    document_id         TEXT NOT NULL,
    role                TEXT NOT NULL CHECK (
                            role IN ('main_pdf', 'supplement', 'screenshot', 'ocr_text', 'web_snapshot')
                        ),
    file_name           TEXT NOT NULL,
    storage_key         TEXT NOT NULL,
    mime_type           TEXT NOT NULL DEFAULT '',
    sha256              TEXT NOT NULL,
    size_bytes          INTEGER NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
    page_count          INTEGER CHECK (page_count IS NULL OR page_count >= 0),
    text_layer          TEXT NOT NULL DEFAULT 'missing' CHECK (
                            text_layer IN ('available', 'missing', 'partial')
                        ),
    source_url          TEXT NOT NULL DEFAULT '',
    source_attachment_id TEXT NOT NULL DEFAULT '',
    legacy_payload_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(legacy_payload_json)),
    created_at          TEXT NOT NULL,
    deleted_at          TEXT,
    UNIQUE (id, document_id),
    UNIQUE (document_id, role, sha256),
    FOREIGN KEY (document_id) REFERENCES library_documents(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_file_assets_sha256
    ON file_assets(sha256);

CREATE INDEX IF NOT EXISTS ix_file_assets_document_role
    ON file_assets(document_id, role);

CREATE UNIQUE INDEX IF NOT EXISTS ux_file_assets_one_active_main_pdf
    ON file_assets(document_id)
    WHERE role = 'main_pdf' AND deleted_at IS NULL;

-- A migrated identity may have had one file at several legacy project paths.
-- All paths are recorded; storage_key points at the one canonical managed copy.
CREATE TABLE IF NOT EXISTS file_asset_locations (
    id                  TEXT PRIMARY KEY,
    file_asset_id       TEXT NOT NULL,
    location_type       TEXT NOT NULL CHECK (location_type IN ('managed', 'legacy')),
    path                TEXT NOT NULL,
    available_at_migration INTEGER NOT NULL DEFAULT 1 CHECK (available_at_migration IN (0, 1)),
    created_at          TEXT NOT NULL,
    FOREIGN KEY (file_asset_id) REFERENCES file_assets(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_file_asset_locations_asset
    ON file_asset_locations(file_asset_id, location_type);

-- ---------------------------------------------------------------------------
-- Annotations
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS annotations (
    id                  TEXT PRIMARY KEY,
    document_id         TEXT NOT NULL,
    file_id             TEXT NOT NULL,
    type                TEXT NOT NULL CHECK (
                            type IN ('highlight', 'underline', 'strikeout', 'note', 'region')
                        ),
    quote               TEXT NOT NULL DEFAULT '',
    comment_markdown    TEXT NOT NULL DEFAULT '',
    color               TEXT NOT NULL DEFAULT '' CHECK (
                            color = '' OR color GLOB '#[0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]'
                        ),
    page_index          INTEGER NOT NULL CHECK (page_index >= 0),
    page_label          TEXT NOT NULL DEFAULT '',
    text_source         TEXT NOT NULL DEFAULT 'pdf_text' CHECK (
                            text_source IN ('pdf_text', 'ocr')
                        ),
    text_hash           TEXT NOT NULL DEFAULT '',
    char_start          INTEGER CHECK (char_start IS NULL OR char_start >= 0),
    char_end            INTEGER CHECK (char_end IS NULL OR char_end >= 0),
    rects_json          TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(rects_json)),
    selector_json       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(selector_json)),
    sort_index          TEXT NOT NULL,
    anchor_status       TEXT NOT NULL DEFAULT 'exact' CHECK (
                            anchor_status IN ('exact', 'partial', 'none', 'no_text_layer', 'out_of_range')
                        ),
    author_id           TEXT NOT NULL DEFAULT '',
    updated_by          TEXT NOT NULL DEFAULT '',
    deleted_by          TEXT NOT NULL DEFAULT '',
    source              TEXT NOT NULL DEFAULT 'user' CHECK (
                            source IN ('user', 'external_pdf', 'ai_suggested', 'legacy')
                        ),
    read_only           INTEGER NOT NULL DEFAULT 0 CHECK (read_only IN (0, 1)),
    external_id         TEXT NOT NULL DEFAULT '',
    revision            INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    legacy_payload_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(legacy_payload_json)),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    deleted_at          TEXT,
    CHECK (char_start IS NULL OR char_end IS NULL OR char_end >= char_start),
    FOREIGN KEY (document_id) REFERENCES library_documents(id) ON DELETE CASCADE,
    FOREIGN KEY (file_id, document_id)
        REFERENCES file_assets(id, document_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_annotations_external
    ON annotations(file_id, source, external_id)
    WHERE external_id <> '';

CREATE INDEX IF NOT EXISTS ix_annotations_file_page_sort
    ON annotations(file_id, page_index, sort_index);

CREATE INDEX IF NOT EXISTS ix_annotations_document_updated
    ON annotations(document_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS annotation_tags (
    annotation_id       TEXT NOT NULL,
    tag_id              TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (annotation_id, tag_id),
    FOREIGN KEY (annotation_id) REFERENCES annotations(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Outline versions
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS outline_versions (
    id                  TEXT PRIMARY KEY,
    document_id         TEXT NOT NULL,
    kind                TEXT NOT NULL DEFAULT 'reading_outline' CHECK (
                            kind IN ('paper_structure', 'reading_outline')
                        ),
    version             INTEGER NOT NULL CHECK (version >= 1),
    status              TEXT NOT NULL DEFAULT 'ai_draft' CHECK (
                            status IN ('ai_draft', 'draft', 'confirmed', 'archived')
                        ),
    base_version_id     TEXT,
    is_current          INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0, 1)),
    markdown            TEXT NOT NULL DEFAULT '',
    source_run_id       TEXT NOT NULL DEFAULT '',
    created_by          TEXT NOT NULL DEFAULT 'ai' CHECK (created_by IN ('ai', 'user', 'mixed', 'legacy')),
    revision            INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    legacy_source_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(legacy_source_json)),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (document_id, kind, version),
    FOREIGN KEY (document_id) REFERENCES library_documents(id) ON DELETE CASCADE,
    FOREIGN KEY (base_version_id) REFERENCES outline_versions(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_outline_versions_current
    ON outline_versions(document_id, kind)
    WHERE is_current = 1;

CREATE INDEX IF NOT EXISTS ix_outline_versions_document_kind_updated
    ON outline_versions(document_id, kind, updated_at DESC);

CREATE TABLE IF NOT EXISTS outline_nodes (
    id                  TEXT PRIMARY KEY,
    outline_version_id  TEXT NOT NULL,
    parent_id           TEXT,
    order_index         INTEGER NOT NULL CHECK (order_index >= 0),
    heading_level       INTEGER NOT NULL CHECK (heading_level BETWEEN 1 AND 4),
    title               TEXT NOT NULL,
    body_markdown       TEXT NOT NULL DEFAULT '',
    origin              TEXT NOT NULL DEFAULT 'ai' CHECK (origin IN ('ai', 'user', 'mixed')),
    active              INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    legacy_source_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(legacy_source_json)),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (outline_version_id) REFERENCES outline_versions(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES outline_nodes(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_outline_nodes_position
    ON outline_nodes(outline_version_id, COALESCE(parent_id, ''), order_index);

CREATE INDEX IF NOT EXISTS ix_outline_nodes_parent
    ON outline_nodes(outline_version_id, parent_id, order_index);

CREATE TABLE IF NOT EXISTS outline_node_evidence (
    id                  TEXT PRIMARY KEY,
    outline_node_id     TEXT NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN ('annotation', 'page', 'outline_node', 'document')),
    document_id         TEXT NOT NULL,
    annotation_id       TEXT,
    target_outline_node_id TEXT,
    page_index          INTEGER,
    quote               TEXT NOT NULL DEFAULT '',
    match_status        TEXT NOT NULL DEFAULT 'exact' CHECK (
                            match_status IN ('exact', 'partial', 'none', 'no_text_layer', 'out_of_range')
                        ),
    created_at          TEXT NOT NULL,
    FOREIGN KEY (outline_node_id) REFERENCES outline_nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (document_id) REFERENCES library_documents(id) ON DELETE CASCADE,
    FOREIGN KEY (annotation_id) REFERENCES annotations(id) ON DELETE SET NULL,
    FOREIGN KEY (target_outline_node_id) REFERENCES outline_nodes(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_outline_node_evidence_node
    ON outline_node_evidence(outline_node_id);

-- ---------------------------------------------------------------------------
-- Summary versions
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS summary_versions (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    version             INTEGER NOT NULL CHECK (version >= 1),
    mode                TEXT NOT NULL CHECK (mode IN ('independent', 'incremental')),
    base_version_id     TEXT,
    status              TEXT NOT NULL CHECK (
                            status IN (
                                'draft', 'queued', 'running', 'partial', 'done',
                                'failed', 'cancelled', 'final'
                            )
                        ),
    title               TEXT NOT NULL DEFAULT '',
    markdown            TEXT NOT NULL DEFAULT '',
    run_id              TEXT NOT NULL DEFAULT '',
    model               TEXT NOT NULL DEFAULT '',
    prompt_version      TEXT NOT NULL DEFAULT '',
    stale               INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0, 1)),
    created_by          TEXT NOT NULL DEFAULT '',
    revision            INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    legacy_source_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(legacy_source_json)),
    legacy_source_hash  TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    finalized_at        TEXT,
    UNIQUE (project_id, version),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (base_version_id) REFERENCES summary_versions(id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_summary_versions_run
    ON summary_versions(run_id)
    WHERE run_id <> '';

CREATE UNIQUE INDEX IF NOT EXISTS ux_summary_versions_legacy_source
    ON summary_versions(project_id, legacy_source_hash)
    WHERE legacy_source_hash <> '';

CREATE INDEX IF NOT EXISTS ix_summary_versions_project_created
    ON summary_versions(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS summary_version_documents (
    summary_version_id  TEXT NOT NULL,
    document_id         TEXT NOT NULL,
    file_sha256         TEXT NOT NULL DEFAULT '',
    outline_version_id  TEXT,
    outline_revision    INTEGER,
    inclusion_status    TEXT NOT NULL CHECK (inclusion_status IN ('included', 'excluded')),
    exclusion_reason    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (summary_version_id, document_id),
    FOREIGN KEY (summary_version_id) REFERENCES summary_versions(id) ON DELETE CASCADE,
    FOREIGN KEY (document_id) REFERENCES library_documents(id) ON DELETE RESTRICT,
    FOREIGN KEY (outline_version_id) REFERENCES outline_versions(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_summary_version_documents_document
    ON summary_version_documents(document_id);

CREATE TABLE IF NOT EXISTS summary_sections (
    id                  TEXT PRIMARY KEY,
    summary_version_id  TEXT NOT NULL,
    parent_id           TEXT,
    stable_key          TEXT NOT NULL,
    order_index         INTEGER NOT NULL CHECK (order_index >= 0),
    title               TEXT NOT NULL,
    body_markdown       TEXT NOT NULL DEFAULT '',
    change_type         TEXT NOT NULL DEFAULT 'unchanged' CHECK (
                            change_type IN ('new', 'changed', 'unchanged', 'removed', 'unsupported')
                        ),
    base_section_id     TEXT,
    revision            INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (summary_version_id, stable_key),
    FOREIGN KEY (summary_version_id) REFERENCES summary_versions(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES summary_sections(id) ON DELETE CASCADE,
    FOREIGN KEY (base_section_id) REFERENCES summary_sections(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_summary_sections_version_order
    ON summary_sections(summary_version_id, parent_id, order_index);

CREATE TABLE IF NOT EXISTS summary_section_evidence (
    id                  TEXT PRIMARY KEY,
    summary_section_id  TEXT NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN ('annotation', 'page', 'outline_node', 'document')),
    document_id         TEXT NOT NULL,
    annotation_id       TEXT,
    outline_node_id     TEXT,
    page_index          INTEGER,
    quote               TEXT NOT NULL DEFAULT '',
    match_status        TEXT NOT NULL DEFAULT 'exact' CHECK (
                            match_status IN ('exact', 'partial', 'none', 'no_text_layer', 'out_of_range')
                        ),
    created_at          TEXT NOT NULL,
    FOREIGN KEY (summary_section_id) REFERENCES summary_sections(id) ON DELETE CASCADE,
    FOREIGN KEY (document_id) REFERENCES library_documents(id) ON DELETE RESTRICT,
    FOREIGN KEY (annotation_id) REFERENCES annotations(id) ON DELETE SET NULL,
    FOREIGN KEY (outline_node_id) REFERENCES outline_nodes(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_summary_section_evidence_section
    ON summary_section_evidence(summary_section_id);

-- ---------------------------------------------------------------------------
-- Directed document relations
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS document_relations (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    from_document_id    TEXT NOT NULL,
    to_document_id      TEXT NOT NULL,
    relation_type       TEXT NOT NULL CHECK (
                            relation_type IN ('cites', 'extends', 'compares', 'replaces', 'diverges')
                        ),
    source              TEXT NOT NULL CHECK (source IN ('openalex', 'user', 'llm')),
    provider            TEXT NOT NULL DEFAULT '',
    external_id         TEXT NOT NULL DEFAULT '',
    locator             TEXT NOT NULL DEFAULT '',
    source_record_id    TEXT NOT NULL DEFAULT '',
    certainty           TEXT NOT NULL DEFAULT 'inferred' CHECK (
                            certainty IN ('stated', 'implied', 'inferred')
                        ),
    verified            INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)),
    review_status       TEXT NOT NULL DEFAULT 'pending' CHECK (
                            review_status IN ('pending', 'confirmed', 'rejected')
                        ),
    review_note         TEXT NOT NULL DEFAULT '',
    fingerprint         TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    CHECK (from_document_id <> to_document_id),
    CHECK (verified = 0 OR locator <> '' OR external_id <> ''),
    UNIQUE (project_id, fingerprint),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (from_document_id) REFERENCES library_documents(id) ON DELETE RESTRICT,
    FOREIGN KEY (to_document_id) REFERENCES library_documents(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_document_relations_from
    ON document_relations(project_id, from_document_id, relation_type);

CREATE INDEX IF NOT EXISTS ix_document_relations_to
    ON document_relations(project_id, to_document_id, relation_type);

COMMIT;
