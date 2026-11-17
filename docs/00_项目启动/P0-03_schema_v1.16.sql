-- ResearchPilot v1.16 P0-03 derived tables for PDF.js reader/OCR.
-- Depends on P0-01_schema_v1.16.sql.

PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS document_page_texts (
    id                  TEXT PRIMARY KEY,
    document_id         TEXT NOT NULL,
    file_asset_id       TEXT NOT NULL,
    page_index          INTEGER NOT NULL CHECK (page_index >= 0),
    source              TEXT NOT NULL CHECK (source IN ('pdf_text', 'ocr')),
    text                TEXT NOT NULL DEFAULT '',
    text_hash           TEXT NOT NULL,
    char_map_json       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(char_map_json)),
    words_json          TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(words_json)),
    ocr_job_id          TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (file_asset_id, page_index, source),
    FOREIGN KEY (document_id) REFERENCES library_documents(id) ON DELETE CASCADE,
    FOREIGN KEY (file_asset_id, document_id)
        REFERENCES file_assets(id, document_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_document_page_texts_document_page
    ON document_page_texts(document_id, page_index, source);

CREATE INDEX IF NOT EXISTS ix_document_page_texts_text_hash
    ON document_page_texts(file_asset_id, text_hash);

CREATE TABLE IF NOT EXISTS reader_ocr_jobs (
    id                  TEXT PRIMARY KEY,
    document_id         TEXT NOT NULL,
    file_asset_id       TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'queued' CHECK (
                            status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')
                        ),
    engine              TEXT NOT NULL DEFAULT '',
    language            TEXT NOT NULL DEFAULT 'chi_sim+eng',
    progress            REAL NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),
    output_file_asset_id TEXT,
    error_code          TEXT NOT NULL DEFAULT '',
    error_message       TEXT NOT NULL DEFAULT '',
    lease_owner         TEXT NOT NULL DEFAULT '',
    lease_expires_at    TEXT,
    requested_by        TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    started_at          TEXT,
    finished_at         TEXT,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES library_documents(id) ON DELETE CASCADE,
    FOREIGN KEY (file_asset_id, document_id)
        REFERENCES file_assets(id, document_id) ON DELETE CASCADE,
    FOREIGN KEY (output_file_asset_id) REFERENCES file_assets(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_reader_ocr_jobs_active
    ON reader_ocr_jobs(file_asset_id, language)
    WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS ix_reader_ocr_jobs_queue
    ON reader_ocr_jobs(status, created_at);

CREATE INDEX IF NOT EXISTS ix_reader_ocr_jobs_file
    ON reader_ocr_jobs(file_asset_id, created_at DESC);

COMMIT;
