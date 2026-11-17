-- ResearchPilot v1.16 P0-04 annotation audit and search supplements.
-- Run after P0-01_schema_v1.16.sql.
--
-- NOTE: P0-01 has already been extended in this delivery with:
--   annotations.type += 'strikeout'
--   annotations.text_source / text_hash / char_start / char_end
--   annotations.updated_by / deleted_by
--   normalized page-relative rects

PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS annotation_operation_batches (
    id                  TEXT PRIMARY KEY,
    document_id         TEXT NOT NULL,
    file_asset_id       TEXT NOT NULL,
    action              TEXT NOT NULL CHECK (
                            action IN ('batch_delete', 'batch_set_color', 'batch_set_type')
                        ),
    target_ids_json     TEXT NOT NULL CHECK (json_valid(target_ids_json)),
    filter_json         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(filter_json)),
    actor_id            TEXT NOT NULL DEFAULT '',
    affected_count      INTEGER NOT NULL DEFAULT 0 CHECK (affected_count >= 0),
    created_at          TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES library_documents(id) ON DELETE CASCADE,
    FOREIGN KEY (file_asset_id, document_id)
        REFERENCES file_assets(id, document_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_annotation_operation_batches_document
    ON annotation_operation_batches(document_id, created_at DESC);

CREATE TABLE IF NOT EXISTS annotation_operation_logs (
    id                  TEXT PRIMARY KEY,
    batch_id            TEXT,
    annotation_id       TEXT NOT NULL,
    document_id         TEXT NOT NULL,
    action              TEXT NOT NULL CHECK (
                            action IN (
                                'created', 'comment_updated', 'style_updated',
                                'deleted', 'restored'
                            )
                        ),
    actor_id            TEXT NOT NULL DEFAULT '',
    before_json         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(before_json)),
    after_json          TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(after_json)),
    created_at          TEXT NOT NULL,
    FOREIGN KEY (batch_id) REFERENCES annotation_operation_batches(id) ON DELETE SET NULL,
    FOREIGN KEY (annotation_id) REFERENCES annotations(id) ON DELETE CASCADE,
    FOREIGN KEY (document_id) REFERENCES library_documents(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_annotation_operation_logs_annotation
    ON annotation_operation_logs(annotation_id, created_at);

CREATE INDEX IF NOT EXISTS ix_annotation_operation_logs_document
    ON annotation_operation_logs(document_id, created_at DESC);

-- Trigram tokenizer supports substring search in Chinese and Latin text.
CREATE VIRTUAL TABLE IF NOT EXISTS annotation_fts USING fts5(
    annotation_id UNINDEXED,
    document_id UNINDEXED,
    quote,
    comment_markdown,
    tokenize = 'trigram'
);

CREATE TRIGGER IF NOT EXISTS trg_annotation_fts_insert
AFTER INSERT ON annotations
WHEN NEW.deleted_at IS NULL
BEGIN
    INSERT INTO annotation_fts(
        rowid, annotation_id, document_id, quote, comment_markdown
    )
    VALUES (
        NEW.rowid, NEW.id, NEW.document_id, NEW.quote, NEW.comment_markdown
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_annotation_fts_update
AFTER UPDATE OF quote, comment_markdown, deleted_at ON annotations
BEGIN
    DELETE FROM annotation_fts WHERE rowid = OLD.rowid;
    INSERT INTO annotation_fts(
        rowid, annotation_id, document_id, quote, comment_markdown
    )
    SELECT NEW.rowid, NEW.id, NEW.document_id, NEW.quote, NEW.comment_markdown
    WHERE NEW.deleted_at IS NULL;
END;

CREATE TRIGGER IF NOT EXISTS trg_annotation_fts_delete
AFTER DELETE ON annotations
BEGIN
    DELETE FROM annotation_fts WHERE rowid = OLD.rowid;
END;

-- Backfill existing annotations after creating triggers.
INSERT INTO annotation_fts(rowid, annotation_id, document_id, quote, comment_markdown)
SELECT rowid, id, document_id, quote, comment_markdown
FROM annotations
WHERE deleted_at IS NULL
  AND rowid NOT IN (SELECT rowid FROM annotation_fts);

COMMIT;
