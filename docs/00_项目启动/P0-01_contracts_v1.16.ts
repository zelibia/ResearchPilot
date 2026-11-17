/**
 * ResearchPilot v1.16 P0-01 data contracts.
 *
 * These types mirror P0-01_schema_v1.16.sql. The persistence layer stores
 * snake_case columns; the application/API layer exposes camelCase fields.
 * Convert at the repository boundary, not in UI components.
 */

declare const brand: unique symbol;
type Brand<T, B extends string> = T & { readonly [brand]: B };

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

export type ISODateTime = string;

export type DocumentId = Brand<string, "DocumentId">;
export type ProjectId = Brand<string, "ProjectId">;
export type ProjectDocumentId = Brand<string, "ProjectDocumentId">;
export type FileAssetId = Brand<string, "FileAssetId">;
export type AnnotationId = Brand<string, "AnnotationId">;
export type OutlineVersionId = Brand<string, "OutlineVersionId">;
export type OutlineNodeId = Brand<string, "OutlineNodeId">;
export type SummaryVersionId = Brand<string, "SummaryVersionId">;
export type SummarySectionId = Brand<string, "SummarySectionId">;
export type CollectionId = Brand<string, "CollectionId">;
export type TagId = Brand<string, "TagId">;
export type DocumentRelationId = Brand<string, "DocumentRelationId">;

export interface ActorContext {
  actorId: string;
  requestId: string;
}

// ---------------------------------------------------------------------------
// Canonical document
// ---------------------------------------------------------------------------

export type EntryType =
  | "article"
  | "book"
  | "book_chapter"
  | "conference"
  | "thesis"
  | "dataset"
  | "software"
  | "webpage"
  | "other";

export type MetadataSource =
  | "pdf_embedded"
  | "first_page"
  | "filename"
  | "body"
  | "crossref"
  | "openalex"
  | "ocr"
  | "manual"
  | "legacy";

export type VerificationStatus = "unverified" | "verified" | "partial" | "conflict";

export interface Author {
  given?: string;
  family?: string;
  literal?: string;
  sequence?: number;
  affiliation?: string;
}

export interface LibraryDocument {
  id: DocumentId;

  /** `doi:<normalized-doi>`, `title:<sha256>` or `legacy:<paper-id>`. */
  identityKey: string;
  doiKey: string | null;
  titleKey: string | null;

  entryType: EntryType;
  title: string;
  titleRaw: string;
  titleSource: string;
  authors: Author[];
  authorsRaw: string;
  year: string;
  venue: string;
  volume: string;
  issue: string;
  pages: string;
  publisher: string;
  abstract: string;
  language: string;

  sourceUrls: string[];
  externalIds: Record<string, string>;
  externalMetadata: JsonObject;
  metadataChecks: JsonValue[];
  guessSources: Record<string, string>;
  parseWarnings: string[];

  verificationStatus: VerificationStatus;
  verifiedAt: string;

  /** Original local time string retained exactly from the legacy Paper row. */
  legacyCreatedAt: string;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
  deletedAt: ISODateTime | null;
}

export interface DocumentFieldValue {
  id: string;
  documentId: DocumentId;
  fieldName: string;
  value: JsonValue;
  valueHash: string;
  source: MetadataSource;
  sourceRecordId: string;
  confidence: number | null;
  isCurrent: boolean;
  createdAt: ISODateTime;
}

// ---------------------------------------------------------------------------
// Project membership and organization
// ---------------------------------------------------------------------------

export type DocumentOrigin =
  | "upload"
  | "screenshot"
  | "doi"
  | "url"
  | "library"
  | "citation_candidate"
  | "legacy";

export type ReadingState =
  | "not_planned"
  | "to_read"
  | "reading"
  | "outline_draft"
  | "outline_confirmed"
  | "synthesis_ready"
  | "archived";

export interface ProjectDocument {
  id: ProjectDocumentId;
  projectId: ProjectId;
  documentId: DocumentId;
  origin: DocumentOrigin;
  readingState: ReadingState;
  citeCandidate: boolean;
  projectNote: string;
  addedBy: string;
  addedAt: ISODateTime;
  updatedAt: ISODateTime;
  lastReadPageIndex: number | null;
  lastReadOutlineNodeId: OutlineNodeId | null;

  /** First legacy Paper represented by this membership (stable display/audit id). */
  legacyPaperId: string;
  /** All legacy Paper rows that collapsed into this membership. */
  legacyPaperIds: string[];
  legacyPayload: JsonObject;

  deletedAt: ISODateTime | null;
}

export type CollectionKind = "manual" | "smart";

export interface CollectionRule {
  all?: JsonValue[];
  any?: JsonValue[];
  none?: JsonValue[];
}

export interface Collection {
  id: CollectionId;
  projectId: ProjectId;
  parentId: CollectionId | null;
  name: string;
  nameKey: string;
  kind: CollectionKind;
  rule: CollectionRule;
  orderIndex: number;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
}

export interface CollectionDocument {
  collectionId: CollectionId;
  documentId: DocumentId;
  addedAt: ISODateTime;
}

export type TagSource = "user" | "import" | "ai";

export interface Tag {
  id: TagId;
  tagKey: string;
  displayName: string;
  color: string;
  createdAt: ISODateTime;
}

export interface DocumentTag {
  documentId: DocumentId;
  tagId: TagId;
  source: TagSource;
  confidence: number | null;
  createdAt: ISODateTime;
}

// ---------------------------------------------------------------------------
// Files
// ---------------------------------------------------------------------------

export type FileRole = "main_pdf" | "supplement" | "screenshot" | "ocr_text" | "web_snapshot";
export type TextLayerStatus = "available" | "missing" | "partial";

export interface FileAsset {
  id: FileAssetId;
  documentId: DocumentId;
  role: FileRole;
  fileName: string;
  /** Opaque storage key, never an absolute local path exposed to the UI. */
  storageKey: string;
  mimeType: string;
  sha256: string;
  sizeBytes: number;
  pageCount: number | null;
  textLayer: TextLayerStatus;
  sourceUrl: string;
  sourceAttachmentId: string;
  legacyPayload: JsonObject;
  createdAt: ISODateTime;
  deletedAt: ISODateTime | null;
}

export interface FileAssetLocation {
  id: string;
  fileAssetId: FileAssetId;
  locationType: "managed" | "legacy";
  path: string;
  availableAtMigration: boolean;
  createdAt: ISODateTime;
}

// ---------------------------------------------------------------------------
// Annotations
// ---------------------------------------------------------------------------

export type AnnotationType = "highlight" | "underline" | "strikeout" | "note" | "region";
export type AnnotationSource = "user" | "external_pdf" | "ai_suggested" | "legacy";
export type AnchorStatus = "exact" | "partial" | "none" | "no_text_layer" | "out_of_range";

/** Page-relative top-left rectangle. Values are fractions in [0, 1]. */
export interface PageNormalizedRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** Legacy PDF-point rectangle kept for import/export adapters. */
export type PdfRect = [number, number, number, number];

export interface Annotation {
  id: AnnotationId;
  documentId: DocumentId;
  fileId: FileAssetId;
  type: AnnotationType;
  quote: string;
  commentMarkdown: string;
  color: string;

  /** 0-based internal page index. UI displays `pageIndex + 1`. */
  pageIndex: number;
  pageLabel: string;
  textSource: "pdf_text" | "ocr";
  textHash: string;
  charStart: number | null;
  charEnd: number | null;
  rects: PageNormalizedRect[];
  selector: JsonObject;
  sortIndex: string;
  anchorStatus: AnchorStatus;

  authorId: string;
  updatedBy: string;
  deletedBy: string;
  source: AnnotationSource;
  readOnly: boolean;
  externalId: string;
  revision: number;
  legacyPayload: JsonObject;

  createdAt: ISODateTime;
  updatedAt: ISODateTime;
  deletedAt: ISODateTime | null;
}

export interface AnnotationTag {
  annotationId: AnnotationId;
  tagId: TagId;
  createdAt: ISODateTime;
}

// ---------------------------------------------------------------------------
// Evidence and outlines
// ---------------------------------------------------------------------------

export type EvidenceKind = "annotation" | "page" | "outline_node" | "document";
export type EvidenceMatchStatus = "exact" | "partial" | "none" | "no_text_layer" | "out_of_range";

export interface EvidenceRef {
  kind: EvidenceKind;
  documentId: DocumentId;
  annotationId?: AnnotationId;
  outlineNodeId?: OutlineNodeId;
  pageIndex?: number;
  quote?: string;
  matchStatus: EvidenceMatchStatus;
}

export type OutlineKind = "paper_structure" | "reading_outline";
export type OutlineStatus = "ai_draft" | "draft" | "confirmed" | "archived";
export type OutlineOrigin = "ai" | "user" | "mixed";

export interface OutlineVersion {
  id: OutlineVersionId;
  documentId: DocumentId;
  kind: OutlineKind;
  version: number;
  status: OutlineStatus;
  baseVersionId: OutlineVersionId | null;
  isCurrent: boolean;
  markdown: string;
  sourceRunId: string;
  createdBy: "ai" | "user" | "mixed" | "legacy";
  revision: number;
  legacySource: JsonObject;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
}

export interface OutlineNode {
  id: OutlineNodeId;
  outlineVersionId: OutlineVersionId;
  parentId: OutlineNodeId | null;
  orderIndex: number;
  headingLevel: 1 | 2 | 3 | 4;
  title: string;
  bodyMarkdown: string;
  origin: OutlineOrigin;
  active: boolean;
  legacySource: JsonObject;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
}

export interface OutlineNodeEvidence extends EvidenceRef {
  id: string;
  ownerOutlineNodeId: OutlineNodeId;
  createdAt: ISODateTime;
}

export interface OutlineBootstrap {
  current: OutlineVersion;
  nodes: OutlineNode[];
  evidence: OutlineNodeEvidence[];
  versionHistory: Array<Pick<
    OutlineVersion,
    "id" | "kind" | "version" | "status" | "createdBy" | "createdAt" | "updatedAt"
  >>;
}

// ---------------------------------------------------------------------------
// Summary versions
// ---------------------------------------------------------------------------

export type SummaryMode = "independent" | "incremental";
export type SummaryStatus =
  | "draft"
  | "queued"
  | "running"
  | "partial"
  | "done"
  | "failed"
  | "cancelled"
  | "final";
export type SummaryChangeType = "new" | "changed" | "unchanged" | "removed" | "unsupported";

export interface SummaryVersion {
  id: SummaryVersionId;
  projectId: ProjectId;
  version: number;
  mode: SummaryMode;
  baseVersionId: SummaryVersionId | null;
  status: SummaryStatus;
  title: string;
  markdown: string;
  runId: string;
  model: string;
  promptVersion: string;
  stale: boolean;
  createdBy: string;
  revision: number;
  legacySource: JsonObject;
  legacySourceHash: string;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
  finalizedAt: ISODateTime | null;
}

export interface SummaryVersionDocument {
  summaryVersionId: SummaryVersionId;
  documentId: DocumentId;
  fileSha256: string;
  outlineVersionId: OutlineVersionId | null;
  outlineRevision: number | null;
  inclusionStatus: "included" | "excluded";
  exclusionReason: string;
}

export interface SummarySection {
  id: SummarySectionId;
  summaryVersionId: SummaryVersionId;
  parentId: SummarySectionId | null;
  stableKey: string;
  orderIndex: number;
  title: string;
  bodyMarkdown: string;
  changeType: SummaryChangeType;
  baseSectionId: SummarySectionId | null;
  revision: number;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
}

export interface SummarySectionEvidence extends EvidenceRef {
  id: string;
  summarySectionId: SummarySectionId;
  createdAt: ISODateTime;
}

export interface SummaryBootstrap {
  version: SummaryVersion;
  documents: SummaryVersionDocument[];
  sections: SummarySection[];
  evidence: SummarySectionEvidence[];
}

// ---------------------------------------------------------------------------
// Directed document relations
// ---------------------------------------------------------------------------

export type RelationType = "cites" | "extends" | "compares" | "replaces" | "diverges";
export type RelationSource = "openalex" | "user" | "llm";
export type Certainty = "stated" | "implied" | "inferred";
export type ReviewStatus = "pending" | "confirmed" | "rejected";

export interface DocumentRelation {
  id: DocumentRelationId;
  projectId: ProjectId;
  fromDocumentId: DocumentId;
  toDocumentId: DocumentId;
  relationType: RelationType;
  source: RelationSource;
  provider: string;
  externalId: string;
  locator: string;
  sourceRecordId: string;
  certainty: Certainty;
  verified: boolean;
  reviewStatus: ReviewStatus;
  reviewNote: string;
  fingerprint: string;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
}

// ---------------------------------------------------------------------------
// Migration records
// ---------------------------------------------------------------------------

export type MigrationStatus =
  | "pending"
  | "preflight"
  | "backed_up"
  | "schema_created"
  | "documents_built"
  | "memberships_built"
  | "files_built"
  | "derived_backfilled"
  | "validated"
  | "cutover_ready"
  | "committed"
  | "failed";

export interface MigrationRun {
  id: string;
  schemaFrom: string;
  schemaTo: string;
  status: MigrationStatus;
  startedAt: ISODateTime;
  finishedAt: ISODateTime | null;
  backupPath: string;
  backupSha256: string;
  sourceCounts: Record<string, number>;
  resultCounts: Record<string, number>;
  error: JsonObject;
  note: string;
}

export type MatchBasis =
  | "sha256"
  | "doi"
  | "title"
  | "legacy_id"
  | "embedded"
  | "manual_override";

export interface MigrationIdMap {
  runId: string;
  entityType: string;
  legacyTable: string;
  legacyId: string;
  newId: string;
  matchBasis: MatchBasis;
  createdAt: ISODateTime;
}

export type MigrationConflictKind =
  | "doi_title_mismatch"
  | "same_file_different_doi"
  | "title_collision"
  | "ambiguous_title_match"
  | "metadata_conflict"
  | "missing_file"
  | "invalid_outline"
  | "invalid_annotation"
  | "unknown_legacy_shape";

export interface MigrationConflict {
  id: string;
  runId: string;
  kind: MigrationConflictKind;
  legacyIds: string[];
  documentIds: DocumentId[];
  detail: JsonObject;
  resolution: "pending" | "auto_resolved" | "manual_resolved" | "kept_separate";
  resolvedBy: string;
  resolvedAt: string;
  createdAt: ISODateTime;
}

// ---------------------------------------------------------------------------
// P0-01 outward API: screening module -> document model
// ---------------------------------------------------------------------------

export interface DuplicateLookup {
  doi?: string;
  title?: string;
  authors?: Author[];
  year?: string;
  sha256?: string;
}

export interface DuplicateCandidate {
  documentId: DocumentId;
  matchBasis: "doi" | "title" | "sha256";
  matchStatus: "exact" | "ambiguous";
  conflictingFields: string[];
}

export interface DuplicateResolution {
  matches: DuplicateCandidate[];
  recommendedAction: "link_existing" | "add_attachment" | "review" | "create_new";
}

export interface CandidateDocumentDraft {
  entryType: EntryType;
  title: string;
  titleRaw?: string;
  titleSource?: string;
  authors?: Author[];
  authorsRaw?: string;
  year?: string;
  venue?: string;
  volume?: string;
  issue?: string;
  pages?: string;
  publisher?: string;
  abstract?: string;
  language?: string;
  sourceUrls?: string[];
  externalIds?: Record<string, string>;
  metadataValues?: Array<{
    fieldName: string;
    value: JsonValue;
    source: MetadataSource;
    sourceRecordId?: string;
    confidence?: number;
  }>;
}

export interface CandidateFileDraft {
  role: FileRole;
  fileName: string;
  storageKey: string;
  mimeType: string;
  sha256: string;
  sizeBytes: number;
  pageCount?: number;
  textLayer: TextLayerStatus;
  sourceUrl?: string;
}

export interface CommitScreenedCandidateCommand {
  candidateId: string;
  projectId: ProjectId;
  /** ID owned by the screening module. Must reference a committed user decision. */
  intakeDecisionId: string;
  decision: "store_and_read" | "store_only" | "cite_only";
  duplicateDocumentId?: DocumentId;
  document: CandidateDocumentDraft;
  files: CandidateFileDraft[];
  addedBy: string;
  clientMutationId: string;
}

export interface CommitScreenedCandidateResult {
  documentId: DocumentId;
  projectDocumentId: ProjectDocumentId;
  fileAssetIds: FileAssetId[];
  createdDocument: boolean;
  createdMembership: boolean;
  duplicateMerged: boolean;
}

// ---------------------------------------------------------------------------
// P0-01 outward API: PDF.js reader
// ---------------------------------------------------------------------------

export interface ReaderBootstrap {
  document: LibraryDocument;
  projectDocument: ProjectDocument;
  file: FileAsset;
  fileUrl: string;
  fileSha256: string;
  initialPageIndex: number;
  zoom: number;
  annotations: Annotation[];
  outline: OutlineBootstrap;
  permissions: {
    annotate: boolean;
    editOutline: boolean;
  };
}

export interface CreateAnnotationCommand {
  documentId: DocumentId;
  fileId: FileAssetId;
  type: AnnotationType;
  quote: string;
  commentMarkdown?: string;
  color: string;
  pageIndex: number;
  pageLabel?: string;
  rects: PageNormalizedRect[];
  selector?: JsonObject;
  sortIndex: string;
  anchorStatus: AnchorStatus;
  authorId: string;
  source?: "user" | "ai_suggested";
  clientMutationId: string;
}

export interface AnnotationPatch {
  commentMarkdown?: string;
  color?: string;
  type?: AnnotationType;
  tagIds?: TagId[];
  quote?: string;
  rects?: PageNormalizedRect[];
  sortIndex?: string;
}

export interface UpdateAnnotationCommand {
  annotationId: AnnotationId;
  baseRevision: number;
  patch: AnnotationPatch;
  clientMutationId: string;
}

export interface DeleteAnnotationCommand {
  annotationId: AnnotationId;
  baseRevision: number;
  clientMutationId: string;
}

export interface AnnotationMutationResult {
  annotation: Annotation;
  revision: number;
  undoToken?: string;
}

export interface SaveOutlineTreeCommand {
  outlineVersionId: OutlineVersionId;
  baseRevision: number;
  nodes: Array<{
    id: OutlineNodeId;
    parentId: OutlineNodeId | null;
    orderIndex: number;
    headingLevel: 1 | 2 | 3 | 4;
    title: string;
    bodyMarkdown: string;
  }>;
  clientMutationId: string;
}

export interface SaveOutlineMarkdownCommand {
  outlineVersionId: OutlineVersionId;
  baseRevision: number;
  markdown: string;
  clientMutationId: string;
}

export interface OutlineMutationResult {
  outline: OutlineVersion;
  nodes: OutlineNode[];
}

// ---------------------------------------------------------------------------
// P0-01 outward API: summary generation module
// ---------------------------------------------------------------------------

export interface CreateSummaryVersionCommand {
  projectId: ProjectId;
  mode: SummaryMode;
  baseVersionId?: SummaryVersionId;
  documentIds: DocumentId[];
  excludedDocuments?: Array<{ documentId: DocumentId; reason: string }>;
  runId: string;
  model: string;
  promptVersion: string;
  createdBy: string;
  clientMutationId: string;
}

export interface SaveSummarySectionsCommand {
  summaryVersionId: SummaryVersionId;
  baseRevision: number;
  sections: Array<{
    id: SummarySectionId;
    parentId: SummarySectionId | null;
    stableKey: string;
    orderIndex: number;
    title: string;
    bodyMarkdown: string;
    changeType: SummaryChangeType;
    evidence: Omit<SummarySectionEvidence, "id" | "summarySectionId" | "createdAt">[];
  }>;
}

export interface DataModelPort {
  resolveDuplicate(query: DuplicateLookup): Promise<DuplicateResolution>;
  commitScreenedCandidate(
    command: CommitScreenedCandidateCommand,
    actor: ActorContext
  ): Promise<CommitScreenedCandidateResult>;

  getReaderBootstrap(
    projectId: ProjectId,
    documentId: DocumentId,
    actor: ActorContext
  ): Promise<ReaderBootstrap>;
  createAnnotation(
    command: CreateAnnotationCommand,
    actor: ActorContext
  ): Promise<AnnotationMutationResult>;
  updateAnnotation(
    command: UpdateAnnotationCommand,
    actor: ActorContext
  ): Promise<AnnotationMutationResult>;
  deleteAnnotation(
    command: DeleteAnnotationCommand,
    actor: ActorContext
  ): Promise<AnnotationMutationResult>;

  getCurrentOutline(documentId: DocumentId, kind: OutlineKind): Promise<OutlineBootstrap>;
  saveOutlineTree(
    command: SaveOutlineTreeCommand,
    actor: ActorContext
  ): Promise<OutlineMutationResult>;
  saveOutlineMarkdown(
    command: SaveOutlineMarkdownCommand,
    actor: ActorContext
  ): Promise<OutlineMutationResult>;

  createSummaryVersion(
    command: CreateSummaryVersionCommand,
    actor: ActorContext
  ): Promise<SummaryVersion>;
  saveSummarySections(
    command: SaveSummarySectionsCommand,
    actor: ActorContext
  ): Promise<SummaryBootstrap>;
  finalizeSummaryVersion(
    summaryVersionId: SummaryVersionId,
    baseRevision: number,
    actor: ActorContext
  ): Promise<SummaryVersion>;
}
