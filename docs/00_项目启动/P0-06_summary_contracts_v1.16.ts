/**
 * ResearchPilot v1.16 P0-06 project summary contracts.
 *
 * AI output creates candidate revisions. Only user confirmation can move the
 * current official revision pointer.
 */

import type {
  ActorContext,
  Annotation,
  AnnotationId,
  DocumentId,
  ISODateTime,
  JsonObject,
  ProjectId,
} from "./P0-01_contracts_v1.16";

import type {
  OutlineNode,
  OutlineRevision,
} from "./P0-05_outline_contracts_v1.16";

import type { ReaderAnchor } from "./P0-03_reader_contracts_v1.16";

export const SUMMARY_PROTOCOL_VERSION = "rp.summary.v1";
export type SummaryProtocolVersion = typeof SUMMARY_PROTOCOL_VERSION;

export type SummaryMode = "independent" | "incremental";
export type SummaryRevisionStatus = "candidate" | "official" | "rejected";
export type SummaryRevisionSource = "ai" | "manual" | "rollback" | "legacy" | "migration";
export type SummaryScopeType = "single_document" | "multi_document" | "day" | "project";
export type SummaryChangeStatus =
  | "added"
  | "changed"
  | "unchanged"
  | "removed"
  | "insufficient_evidence";

export interface ProjectSummary {
  id: string;
  projectId: ProjectId;
  currentOfficialRevisionId: string | null;
  currentRevisionNo: number;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
}

export interface SummaryScope {
  type: SummaryScopeType;
  documentIds: DocumentId[];
  day?: string;
  excludedDocuments?: Array<{
    documentId: DocumentId;
    reason: string;
  }>;
}

export interface SummaryEvidence {
  id: string;
  evidenceToken: string;
  documentId: DocumentId;
  outlineNodeId: string | null;
  annotationId: AnnotationId | null;
  claimId: string | null;
  anchor: ReaderAnchor | null;
  quote: string;
  sourceLabel: string;
  evidenceStatus: "exact" | "partial" | "none" | "insufficient";
}

export interface SummarySection {
  id: string;
  stableKey: string;
  parentSectionId: string | null;
  orderIndex: number;
  title: string;
  bodyMarkdown: string;
  changeStatus: SummaryChangeStatus | null;
  contentHash: string;
  evidence: SummaryEvidence[];
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
}

export interface SummaryRevisionDocument {
  documentId: DocumentId;
  inclusionStatus: "included" | "excluded";
  exclusionReason: string;
  fileSha256: string;
  outlineRevisionId: string | null;
  outlineRevisionNo: number | null;
  annotationCount: number;
  sourceUpdatedAt: string;
}

export interface SummaryRevision {
  id: string;
  summaryId: string;
  revisionNo: number;
  mode: SummaryMode;
  baseRevisionId: string | null;
  baseRevisionNo: number | null;
  status: SummaryRevisionStatus;
  source: SummaryRevisionSource;
  scope: SummaryScope;
  title: string;
  markdown: string;
  structured: {
    sections: SummarySection[];
  };
  changeSummary: string;
  contentHash: string;
  createdBy: string;
  createdAt: ISODateTime;
  confirmedBy: string;
  confirmedAt: ISODateTime | null;
  aiRunId: string | null;
  legacySource: JsonObject;
  documents: SummaryRevisionDocument[];
}

export interface SummaryAIRun {
  id: string;
  summaryId: string;
  mode: SummaryMode;
  baseRevisionId: string | null;
  baseRevisionNo: number | null;
  scopeType: SummaryScopeType;
  scope: SummaryScope;
  status: "queued" | "running" | "partial" | "succeeded" | "failed" | "cancelled";
  provider: string;
  model: string;
  promptVersion: string;
  inputHash: string;
  inputScope: JsonObject;
  result: SummaryAIResult | null;
  errorCode: string;
  errorMessage: string;
  requestedBy: string;
  startedAt: ISODateTime | null;
  finishedAt: ISODateTime | null;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
}

export interface GenerateSummaryCommand {
  projectId: ProjectId;
  mode: SummaryMode;
  scope: SummaryScope;
  baseRevisionNo?: number;
  promptVersion: string;
  actor: ActorContext;
  clientMutationId: string;
}

export interface SummaryGenerationState {
  runId: string;
  summaryId: string;
  status: "queued" | "running" | "partial" | "succeeded" | "failed" | "cancelled";
  stage:
    | "scope_resolved"
    | "sources_loaded"
    | "evidence_packed"
    | "model_running"
    | "evidence_verified"
    | "diffing_base"
    | "candidate_saved";
  completedUnits: number | null;
  totalUnits: number | null;
  message: string;
}

// ---------------------------------------------------------------------------
// AI candidate schema
// ---------------------------------------------------------------------------

export interface SummaryEvidenceSource {
  token: string;
  kind: "annotation" | "outline_node" | "claim" | "page";
  documentId: DocumentId;
  outlineNodeId: string | null;
  annotationId: AnnotationId | null;
  claimId: string | null;
  pageIndex: number | null;
  quote: string;
  label: string;
  anchor: ReaderAnchor | null;
}

export interface SummaryAIInput {
  project: {
    id: ProjectId;
    title: string;
    domain: string;
    description: string;
    activeGoal?: string;
  };
  mode: SummaryMode;
  scope: SummaryScope;
  baseSummary?: {
    revisionNo: number;
    sections: SummarySection[];
  };
  documents: Array<{
    documentId: DocumentId;
    title: string;
    authors: string[];
    year: string;
    outlineRevision: OutlineRevision | null;
    outlineNodes: OutlineNode[];
    annotations: Array<Pick<
      Annotation,
      "id" | "type" | "quote" | "commentMarkdown" | "pageIndex"
      | "textSource" | "rects" | "selector"
    >>;
    claims: Array<{
      id: string;
      kind: string;
      text: string;
      quote: string;
      pageIndex: number | null;
      verified: boolean;
    }>;
  }>;
  evidenceSources: SummaryEvidenceSource[];
  promptVersion: string;
}

export interface SummaryAIResult {
  schemaVersion: "summary.v1";
  title: string;
  overviewMarkdown: string;
  sections: Array<{
    stableKey: string;
    parentStableKey: string | null;
    orderIndex: number;
    title: string;
    bodyMarkdown: string;
    evidenceTokens: string[];
    changeStatus?: SummaryChangeStatus | null;
  }>;
  unresolvedQuestions: string[];
  warnings: string[];
}

// ---------------------------------------------------------------------------
// Editing / confirmation / compare / rollback
// ---------------------------------------------------------------------------

export interface UpdateSummarySectionCommand {
  summaryId: string;
  revisionId: string;
  baseRevisionNo: number;
  sectionId: string;
  title?: string;
  bodyMarkdown?: string;
  evidenceIds?: string[];
  changeSummary?: string;
  actor: ActorContext;
  clientMutationId: string;
}

export interface SaveSummaryMarkdownCommand {
  summaryId: string;
  revisionId: string;
  baseRevisionNo: number;
  markdown: string;
  parserMode: "replace_sections" | "preserve_section_ids";
  changeSummary?: string;
  actor: ActorContext;
  clientMutationId: string;
}

export interface SummaryMutationResult {
  revision: SummaryRevision;
  candidate: boolean;
  events: SummaryEvent[];
}

export interface ConfirmSummaryRevisionCommand {
  summaryId: string;
  revisionId: string;
  baseRevisionNo: number;
  changeSummary?: string;
  actor: ActorContext;
  clientMutationId: string;
}

export interface RestoreSummaryRevisionCommand {
  summaryId: string;
  sourceRevisionNo: number;
  baseRevisionNo: number;
  changeSummary: string;
  actor: ActorContext;
  clientMutationId: string;
}

export type SummaryDiffStatus =
  | "added"
  | "changed"
  | "unchanged"
  | "removed"
  | "insufficient_evidence";

export interface SummarySectionDiff {
  stableKey: string;
  title: string;
  status: SummaryDiffStatus;
  before: SummarySection | null;
  after: SummarySection | null;
  markdownDiff: string;
  evidenceAdded: SummaryEvidence[];
  evidenceRemoved: SummaryEvidence[];
}

export interface SummaryRevisionDiff {
  summaryId: string;
  leftRevisionNo: number;
  rightRevisionNo: number;
  sectionDiffs: SummarySectionDiff[];
  counts: Record<SummaryDiffStatus, number>;
}

export interface SummaryFilter {
  documentIds?: DocumentId[];
  sectionStableKeys?: string[];
  changeStatuses?: SummaryDiffStatus[];
  searchQuery?: string;
}

// ---------------------------------------------------------------------------
// Evidence navigation
// ---------------------------------------------------------------------------

export interface SummaryEvidenceClickedEventData {
  summaryId: string;
  revisionNo: number;
  sectionId: string;
  evidence: SummaryEvidence;
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

interface SummaryEventBase<TType extends string, TPayload> {
  protocolVersion: SummaryProtocolVersion;
  eventId: string;
  sequence: number;
  type: TType;
  projectId: ProjectId;
  summaryId: string;
  occurredAt: ISODateTime;
  payload: TPayload;
}

export type SummaryGeneratedEvent = SummaryEventBase<
  "summary_generated",
  {
    runId: string;
    revisionId: string;
    revisionNo: number;
    mode: SummaryMode;
    status: "candidate" | "official";
  }
>;

export type RevisionSwitchEvent = SummaryEventBase<
  "revision_switch",
  {
    fromRevisionNo: number | null;
    toRevisionNo: number;
    mode: "view" | "compare";
    becameCurrent: boolean;
  }
>;

export type SummaryUpdatedEvent = SummaryEventBase<
  "summary_updated",
  {
    revisionId: string;
    revisionNo: number;
    baseRevisionNo: number | null;
    source: SummaryRevisionSource;
    changedSectionIds: string[];
  }
>;

export type GenerateProgressEvent = SummaryEventBase<
  "generate_progress",
  SummaryGenerationState
>;

export type GenerateFailedEvent = SummaryEventBase<
  "generate_failed",
  {
    runId: string;
    code: SummaryErrorCode;
    message: string;
    partialCandidateId: string | null;
  }
>;

export type SummaryConfirmedEvent = SummaryEventBase<
  "summary_confirmed",
  {
    revisionId: string;
    revisionNo: number;
    confirmedBy: string;
  }
>;

export type SummaryEvidenceClickedEvent = SummaryEventBase<
  "summary_evidence_clicked",
  SummaryEvidenceClickedEventData
>;

export type SummaryEvent =
  | SummaryGeneratedEvent
  | RevisionSwitchEvent
  | SummaryUpdatedEvent
  | GenerateProgressEvent
  | GenerateFailedEvent
  | SummaryConfirmedEvent
  | SummaryEvidenceClickedEvent;

export type SummaryEventType = SummaryEvent["type"];

export interface SummaryEventEnvelope {
  event: SummaryEvent;
  bridge: "trigger" | "state";
}

// ---------------------------------------------------------------------------
// Errors / service
// ---------------------------------------------------------------------------

export type SummaryErrorCode =
  | "SCOPE_EMPTY"
  | "SCOPE_INVALID_DOCUMENT"
  | "INCREMENTAL_BASE_REQUIRED"
  | "BASE_REVISION_NOT_OFFICIAL"
  | "BASE_REVISION_MISMATCH"
  | "AI_NOT_CONNECTED"
  | "AI_TIMEOUT"
  | "AI_RATE_LIMIT"
  | "AI_AUTH_FAILED"
  | "AI_BAD_JSON"
  | "AI_SCHEMA_INVALID"
  | "EVIDENCE_VALIDATION_FAILED"
  | "REVISION_CONFLICT"
  | "SUMMARY_NOT_FOUND"
  | "REVISION_NOT_FOUND"
  | "CANDIDATE_REQUIRED"
  | "INTERNAL_ERROR";

export interface SummaryService {
  getProjectSummary(projectId: ProjectId): Promise<{
    summary: ProjectSummary;
    revisions: SummaryRevisionSummary[];
  }>;

  getRevision(summaryId: string, revisionNo: number): Promise<SummaryRevision>;

  generate(command: GenerateSummaryCommand): Promise<SummaryAIRun>;

  getGenerationState(runId: string): Promise<SummaryGenerationState>;

  updateSection(command: UpdateSummarySectionCommand): Promise<SummaryMutationResult>;

  saveMarkdown(command: SaveSummaryMarkdownCommand): Promise<SummaryMutationResult>;

  confirmRevision(command: ConfirmSummaryRevisionCommand): Promise<SummaryMutationResult>;

  restoreRevision(command: RestoreSummaryRevisionCommand): Promise<SummaryMutationResult>;

  compareRevisions(
    summaryId: string,
    leftRevisionNo: number,
    rightRevisionNo: number
  ): Promise<SummaryRevisionDiff>;
}

export interface SummaryRevisionSummary {
  revisionNo: number;
  status: SummaryRevisionStatus;
  mode: SummaryMode;
  baseRevisionNo: number | null;
  createdAt: ISODateTime;
  createdBy: string;
  changeSummary: string;
}
