/**
 * ResearchPilot v1.16 P0-02 screening-inbox contracts.
 *
 * Depends on P0-01_contracts_v1.16.ts. Persistence uses snake_case; API/UI
 * contracts use camelCase. No API key, absolute file path, or raw credential
 * may appear in these objects.
 */

import type {
  ActorContext,
  Annotation,
  CandidateDocumentDraft,
  CandidateFileDraft,
  DocumentId,
  DocumentOrigin,
  FileAssetId,
  FileRole,
  ISODateTime,
  JsonObject,
  JsonValue,
  MetadataSource,
  ProjectDocumentId,
  ProjectId,
  ReadingState,
} from "./P0-01_contracts_v1.16";

// ---------------------------------------------------------------------------
// Candidate state
// ---------------------------------------------------------------------------

export type CandidateStatus =
  | "uploading"
  | "uploaded"
  | "extracting"
  | "ready"
  | "screening_queued"
  | "screening"
  | "screened"
  | "screening_failed"
  | "extraction_failed"
  | "confirming"
  | "confirmed"
  | "skipped"
  | "deleted"
  | "expired";

export type CandidateSourceKind = "pdf" | "image" | "doi" | "url" | "manual";

export type DuplicateStatus =
  | "none"
  | "exact_sha256"
  | "exact_doi"
  | "possible_title"
  | "conflict";

export type ReadRecommendation =
  | "must_read"
  | "recommended"
  | "optional"
  | "not_recommended"
  | "insufficient_evidence"
  | "pending"
  | "failed";

export type CitationRecommendation =
  | "strong_candidate"
  | "candidate"
  | "optional"
  | "not_candidate"
  | "insufficient_evidence"
  | "pending"
  | "failed";

export type CandidateDecisionType =
  | "confirmed_store"
  | "confirmed_deep_read"
  | "skipped"
  | "deleted";

export interface ImportBatch {
  id: string;
  projectId: ProjectId;
  sourceSurface: "upload" | "drop" | "clipboard" | "api";
  status: "open" | "processing" | "completed" | "failed";
  createdBy: string;
  note: string;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
}

export interface CandidateMetadata {
  title: string;
  titleRaw?: string;
  authors: string[];
  authorsRaw?: string;
  year: string;
  venue: string;
  doi: string;
  abstract: string;
  language: string;
  externalIds: Record<string, string>;
  sourceUrls: string[];
  entryType?: string;
}

export interface CandidateMetadataProvenance {
  [fieldName: string]: {
    source: MetadataSource;
    sourceRecordId?: string;
    confidence?: number;
  };
}

export interface ImportCandidate {
  id: string;
  batchId: string;
  projectId: ProjectId;
  status: CandidateStatus;
  sourceKind: CandidateSourceKind;

  displayTitle: string;
  originalFilename: string;
  mimeType: string;
  pageCount: number | null;
  language: string;
  textLayerStatus: "available" | "missing" | "partial";

  metadata: CandidateMetadata;
  metadataProvenance: CandidateMetadataProvenance;
  metadataWarnings: string[];
  fulltextCharCount: number;

  duplicateStatus: DuplicateStatus;
  duplicateDocumentId: DocumentId | null;
  duplicateBasis: string;

  readRecommendation: ReadRecommendation;
  citationRecommendation: CitationRecommendation;
  readingPriorityScore: number | null;
  confidenceAdjusted: number | null;
  currentEvaluationId: string | null;

  confirmedDocumentId: DocumentId | null;
  confirmedProjectDocumentId: ProjectDocumentId | null;

  errorCode: CandidateErrorCode | "";
  errorMessage: string;
  revision: number;

  skippedUntil: ISODateTime | null;
  purgeAfter: ISODateTime | null;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
  deletedAt: ISODateTime | null;
}

export interface CandidateFile {
  id: string;
  candidateId: string;
  role: "source_file" | "ocr_text" | "thumbnail";
  fileName: string;
  storageKey: string;
  mimeType: string;
  sha256: string;
  sizeBytes: number;
  pageCount: number | null;
  textLayerStatus: "available" | "missing" | "partial";
  extractionStatus: "pending" | "running" | "done" | "partial" | "failed" | "not_applicable";
  extractionError: string;
  promotedFileAssetId: FileAssetId | null;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
  deletedAt: ISODateTime | null;
}

export interface CandidateDuplicateMatch {
  id: string;
  candidateId: string;
  documentId: DocumentId;
  matchBasis: "sha256" | "doi" | "title" | "manual";
  matchStatus: "exact" | "possible" | "conflict";
  titleSimilarity: number | null;
  detail: JsonObject;
  createdAt: ISODateTime;
}

// ---------------------------------------------------------------------------
// AI screening runs
// ---------------------------------------------------------------------------

export type CandidateAIRunStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "timed_out"
  | "cancelled"
  | "superseded";

export type CandidateErrorCode =
  | "UPLOAD_INVALID_TYPE"
  | "UPLOAD_TOO_LARGE"
  | "UPLOAD_STORAGE_FAILED"
  | "FILE_HASH_FAILED"
  | "PDF_PARSE_FAILED"
  | "PDF_NO_TEXT_LAYER"
  | "OCR_UNAVAILABLE"
  | "OCR_FAILED"
  | "METADATA_TITLE_MISSING"
  | "AI_NOT_CONNECTED"
  | "AI_TIMEOUT"
  | "AI_RATE_LIMIT"
  | "AI_AUTH_FAILED"
  | "AI_BAD_JSON"
  | "AI_SCHEMA_INVALID"
  | "AI_EMPTY_RESPONSE"
  | "DUPLICATE_CONFLICT"
  | "COMMIT_FAILED"
  | "CANDIDATE_NOT_READY"
  | "REVISION_CONFLICT"
  | "INTERNAL_ERROR";

export interface CandidateAIRun {
  id: string;
  candidateId: string;
  attemptNo: number;
  status: CandidateAIRunStatus;
  provider: string;
  model: string;
  promptVersion: string;
  inputHash: string;
  inputScope: {
    usedFields: string[];
    selectedPages: number[];
    fulltextChars: number;
    truncated: boolean;
    inputCoverage: "fulltext" | "abstract" | "metadata_only";
  };
  projectContext: ScreeningProjectContext;
  inputPreviewMarkdown: string;
  startedAt: ISODateTime | null;
  finishedAt: ISODateTime | null;
  elapsedMs: number | null;
  leaseOwner: string;
  leaseExpiresAt: ISODateTime | null;
  errorCode: CandidateErrorCode | "";
  errorMessage: string;
  usage: JsonObject;
  rawResponse: JsonObject;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
}

export type ScoreDimension =
  | "topic_match"
  | "academic_value"
  | "citation_potential"
  | "reading_priority";

export interface CandidateEvaluationReason {
  id: string;
  evaluationId: string;
  dimension: ScoreDimension;
  polarity: "positive" | "negative" | "risk" | "neutral";
  reasonText: string;
  sourceKind: "metadata" | "abstract" | "fulltext";
  evidencePageIndex: number | null;
  evidenceQuote: string;
  matchStatus: "exact" | "partial" | "none" | "no_text_layer";
  createdAt: ISODateTime;
}

export interface CandidateEvaluation {
  id: string;
  candidateId: string;
  runId: string;
  schemaVersion: string;
  topicMatchScore: number;
  academicValueScore: number;
  citationPotentialScore: number;
  readingPriorityScore: number;
  readRecommendation: Exclude<ReadRecommendation, "pending" | "failed">;
  citationRecommendation: Exclude<CitationRecommendation, "pending" | "failed">;
  confidenceRaw: number;
  confidenceAdjusted: number;
  confidenceLabel: "high" | "medium" | "low";
  inputCoverage: "fulltext" | "abstract" | "metadata_only";
  evidenceStatus: "verified" | "partial" | "none";
  conclusionSummary: string;
  result: ScreeningAIResult;
  reasons: CandidateEvaluationReason[];
  createdAt: ISODateTime;
}

export interface ScreeningProjectContext {
  projectId: ProjectId;
  title: string;
  domain: string;
  description: string;
  activeGoal?: string;
  successCriteria?: string[];
  targetMethodNames?: string[];
  existingDocumentTitles?: string[];
}

export interface ScreeningCandidateInput {
  candidateId: string;
  projectContext: ScreeningProjectContext;
  metadata: CandidateMetadata;
  abstract: string;
  fulltextSections: Array<{
    title: string;
    pageStart: number;
    pageEnd: number;
    text: string;
  }>;
  inputCoverage: "fulltext" | "abstract" | "metadata_only";
}

/** Raw model response shape. Final recommendations are derived by the server. */
export interface ScreeningAIResult {
  schemaVersion: "screening.v1";
  conclusionSummary: string;
  scores: {
    topic_match: { score: number; rationale: string };
    academic_value: { score: number; rationale: string };
    citation_potential: { score: number; rationale: string };
    reading_priority: { score: number; rationale: string };
  };
  reasons: Array<{
    dimension: ScoreDimension;
    polarity: "positive" | "negative" | "risk" | "neutral";
    text: string;
    sourceKind: "metadata" | "abstract" | "fulltext";
    pageIndex?: number | null;
    quote?: string;
  }>;
  modelConfidence: number;
  confidenceRationale: string;
}

export interface ScreeningResult {
  run: CandidateAIRun;
  evaluation: CandidateEvaluation;
  reusedExistingEvaluation: boolean;
}

// ---------------------------------------------------------------------------
// Decisions and operation log
// ---------------------------------------------------------------------------

export interface CandidateDecision {
  id: string;
  candidateId: string;
  evaluationId: string | null;
  decision: CandidateDecisionType;
  readIntent: boolean;
  citeIntent: boolean;
  note: string;
  actorId: string;
  idempotencyKey: string;
  documentId: DocumentId | null;
  projectDocumentId: ProjectDocumentId | null;
  createdAt: ISODateTime;
}

export interface CandidateOperationLog {
  id: string;
  candidateId: string;
  action: string;
  fromStatus: CandidateStatus | null;
  toStatus: CandidateStatus;
  actorId: string;
  detail: JsonObject;
  createdAt: ISODateTime;
}

// ---------------------------------------------------------------------------
// UI / application service contracts
// ---------------------------------------------------------------------------

export interface CreateCandidateCommand {
  projectId: ProjectId;
  batchId: string;
  sourceKind: CandidateSourceKind;
  fileName: string;
  mimeType: string;
  upload: {
    storageKey: string;
    sha256: string;
    sizeBytes: number;
  };
  createdBy: string;
  clientMutationId: string;
}

export interface CreateCandidateResult {
  candidate: ImportCandidate;
  file: CandidateFile;
  duplicateMatches: CandidateDuplicateMatch[];
  autoScreeningQueued: boolean;
}

export interface CandidateListQuery {
  projectId: ProjectId;
  statuses?: CandidateStatus[];
  search?: string;
  duplicateOnly?: boolean;
  limit: number;
  cursor?: string;
}

export interface CandidateListPage {
  items: ImportCandidate[];
  nextCursor: string | null;
  countsByStatus: Partial<Record<CandidateStatus, number>>;
}

export interface CandidateDetail {
  candidate: ImportCandidate;
  files: CandidateFile[];
  duplicateMatches: CandidateDuplicateMatch[];
  latestRun: CandidateAIRun | null;
  latestEvaluation: CandidateEvaluation | null;
  latestDecision: CandidateDecision | null;
  operationLogs: CandidateOperationLog[];
}

export interface RequestScreeningCommand {
  candidateId: string;
  force: boolean;
  actorId: string;
  clientMutationId: string;
  /** Same concurrency token as candidate.revision; optional for legacy callers. */
  baseRevisionNo?: number;
}

export interface SubmitCandidateDecisionCommand {
  candidateId: string;
  expectedRevision: number;
  decision: CandidateDecisionType;
  evaluationId?: string;
  citeIntent: boolean;
  note?: string;
  idempotencyKey: string;
  clientMutationId?: string;
  baseRevisionNo?: number;
  duplicateAction?: "merge" | "add_attachment" | "keep_independent";
  duplicateDocumentId?: DocumentId;
  actorId?: string;
}

export interface CandidateDecisionResult {
  candidate: ImportCandidate;
  decision: CandidateDecision;
  documentId: DocumentId | null;
  projectDocumentId: ProjectDocumentId | null;
  readingState: ReadingState | null;
  nextRoute: string | null;
}

export interface BulkCandidateDecisionCommand {
  candidateIds: string[];
  decision: Extract<CandidateDecisionType, "confirmed_store" | "confirmed_deep_read" | "skipped" | "deleted">;
  citeIntent: boolean;
  idempotencyPrefix: string;
  expectedRevisions?: Record<string, number>;
  duplicateAction?: "merge" | "add_attachment" | "keep_independent";
  actorId?: string;
}

export interface BulkCandidateDecisionResult {
  succeeded: CandidateDecisionResult[];
  failed: Array<{
    candidateId: string;
    errorCode: CandidateErrorCode;
    message: string;
  }>;
}

export interface RestoreCandidateCommand {
  candidateId: string;
  expectedRevision: number;
  actorId: string;
  clientMutationId: string;
}

export interface CancelScreeningCommand {
  candidateId: string;
  actorId: string;
  clientMutationId: string;
  baseRevisionNo?: number;
}

export interface UpdateCandidateMetadataCommand {
  candidateId: string;
  metadata: CandidateMetadata;
  expectedRevision: number;
  actorId: string;
  clientMutationId: string;
}

export interface ScreeningApiError {
  error: {
    code: CandidateErrorCode | "NOT_FOUND" | "VALIDATION_ERROR";
    message: string;
    details: JsonObject;
    requestId: string;
  };
}

export interface DeepReadHandoff {
  projectId: ProjectId;
  documentId: DocumentId;
  projectDocumentId: ProjectDocumentId;
  readingState: Extract<ReadingState, "to_read" | "reading">;
}

// ---------------------------------------------------------------------------
// Cross-module ports
// ---------------------------------------------------------------------------

/**
 * Upload transport only validates and stores bytes in quarantine. It does not
 * create library_documents or inspect project science state.
 */
export interface UploadQuarantinePort {
  put(input: {
    projectId: ProjectId;
    candidateId: string;
    fileName: string;
    mimeType: string;
    source: Blob | ReadableStream<Uint8Array>;
  }): Promise<{
    storageKey: string;
    sha256: string;
    sizeBytes: number;
  }>;
  remove(storageKey: string): Promise<void>;
}

/**
 * P0-01 exposes a connection-scoped method so screening decision + document
 * commit can share one SQLite transaction. The public convenience method may
 * open its own transaction for callers that do not need a wider unit of work.
 */
export interface TransactionalDocumentCommitPort {
  resolveDuplicate(query: {
    doi?: string;
    title?: string;
    authors?: Array<{ given?: string; family?: string; literal?: string }>;
    year?: string;
    sha256?: string;
  }): Promise<{
    matches: Array<{
      documentId: DocumentId;
      matchBasis: "doi" | "title" | "sha256";
      matchStatus: "exact" | "ambiguous";
      conflictingFields: string[];
    }>;
    recommendedAction: "link_existing" | "add_attachment" | "review" | "create_new";
  }>;
  commitScreenedCandidateInTransaction(connection: unknown, input: {
    candidateId: string;
    projectId: ProjectId;
    intakeDecisionId: string;
    decision: "store_and_read" | "store_only" | "cite_only";
    duplicateDocumentId?: DocumentId;
    document: CandidateDocumentDraft;
    files: CandidateFileDraft[];
    addedBy: string;
    clientMutationId: string;
  }): Promise<{
    documentId: DocumentId;
    projectDocumentId: ProjectDocumentId;
    fileAssetIds: FileAssetId[];
    createdDocument: boolean;
    createdMembership: boolean;
    duplicateMerged: boolean;
  }>;
}

export interface CandidateScreeningAI {
  screen(
    input: ScreeningCandidateInput,
    actor: ActorContext
  ): Promise<{
    result: ScreeningAIResult;
    model: string;
    provider: string;
    promptVersion: string;
    elapsedMs: number;
    usage: JsonObject;
    rawResponse: JsonObject;
  }>;
}

export interface CandidateService {
  create(command: CreateCandidateCommand, actor: ActorContext): Promise<CreateCandidateResult>;
  list(query: CandidateListQuery, actor: ActorContext): Promise<CandidateListPage>;
  detail(candidateId: string, actor: ActorContext): Promise<CandidateDetail>;
  updateMetadata(
    command: UpdateCandidateMetadataCommand,
    actor: ActorContext
  ): Promise<ImportCandidate>;
  requestScreening(command: RequestScreeningCommand, actor: ActorContext): Promise<CandidateAIRun>;
  cancelScreening(command: CancelScreeningCommand, actor: ActorContext): Promise<CandidateAIRun>;
  submitDecision(
    command: SubmitCandidateDecisionCommand,
    actor: ActorContext
  ): Promise<CandidateDecisionResult>;
  submitBulkDecision(
    command: BulkCandidateDecisionCommand,
    actor: ActorContext
  ): Promise<BulkCandidateDecisionResult>;
  restore(
    command: RestoreCandidateCommand,
    actor: ActorContext
  ): Promise<ImportCandidate>;
}

// Unused type anchors keep downstream imports explicit.
export type P0RetainedTypes = Annotation | FileRole | DocumentOrigin | JsonValue;
