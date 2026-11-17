/**
 * ResearchPilot v1.16 P0-04 annotation contracts.
 *
 * Depends on P0-01 (Annotation persistence) and P0-03 (Reader events/anchors).
 */

import type {
  ActorContext,
  Annotation,
  AnnotationId,
  AnnotationType,
  DocumentId,
  FileAssetId,
  ISODateTime,
  JsonObject,
  PageNormalizedRect,
  TagId,
} from "./P0-01_contracts_v1.16";

import type {
  AnchorResolveResult,
  ReaderAnchor,
  ReaderAnchorResolvedEvent,
  ReaderTextSelectedEvent,
  TextSelection,
} from "./P0-03_reader_contracts_v1.16";

export const ANNOTATION_PROTOCOL_VERSION = "rp.annotation.v1";
export type AnnotationProtocolVersion = typeof ANNOTATION_PROTOCOL_VERSION;

export type TextAnnotationType = Extract<
  AnnotationType,
  "highlight" | "underline" | "strikeout"
>;

export const ANNOTATION_PALETTE = {
  yellow: "#FFD400",
  red: "#FF6666",
  green: "#5FB236",
  blue: "#2EA8E5",
  purple: "#A28AE5",
  orange: "#F19837",
} as const;

export type AnnotationColor = (typeof ANNOTATION_PALETTE)[keyof typeof ANNOTATION_PALETTE] | string;

export type AnnotationTextSource = "pdf_text" | "ocr";

export interface AnnotationAnchorSelector {
  coordinateVersion: "page-normalized-v1";
  pageViewBox: {
    width: number;
    height: number;
    rotation: number;
  };
  charStart: number;
  charEnd: number;
  textHash: string;
  textSource: AnnotationTextSource;
  segments: ReaderAnchor["segments"];
}

export interface AnnotationDraft {
  documentId: DocumentId;
  fileAssetId: FileAssetId;
  type: TextAnnotationType;
  color: AnnotationColor;
  quote: string;
  commentMarkdown: string;
  pageIndex: number;
  pageLabel: string;
  rects: PageNormalizedRect[];
  selector: AnnotationAnchorSelector;
  sortIndex: string;
  anchorStatus: "exact" | "partial" | "none" | "no_text_layer" | "out_of_range";
}

export interface CreateAnnotationCommand {
  draft: AnnotationDraft;
  actor: ActorContext;
  clientMutationId: string;
}

export interface UpdateAnnotationCommentCommand {
  annotationId: AnnotationId;
  baseRevision: number;
  commentMarkdown: string;
  actor: ActorContext;
  clientMutationId: string;
}

export interface UpdateAnnotationStyleCommand {
  annotationId: AnnotationId;
  baseRevision: number;
  patch: {
    type?: TextAnnotationType;
    color?: AnnotationColor;
  };
  actor: ActorContext;
  clientMutationId: string;
}

export interface DeleteAnnotationCommand {
  annotationId: AnnotationId;
  baseRevision: number;
  actor: ActorContext;
  clientMutationId: string;
}

export interface RestoreAnnotationCommand {
  annotationId: AnnotationId;
  baseRevision: number;
  actor: ActorContext;
  clientMutationId: string;
}

export interface AnnotationMutationResult {
  annotation: Annotation;
  revision: number;
  undoToken: string | null;
}

// ---------------------------------------------------------------------------
// Sidebar queries and batch operations
// ---------------------------------------------------------------------------

export type AnnotationSidebarSort = "page_asc" | "updated_desc" | "created_desc";

export interface AnnotationSidebarFilter {
  types?: TextAnnotationType[];
  colors?: string[];
  textSources?: AnnotationTextSource[];
  editedOnly?: boolean;
  commentOnly?: boolean;
  searchQuery?: string;
}

export interface AnnotationSidebarQuery {
  documentId: DocumentId;
  fileAssetId: FileAssetId;
  filter: AnnotationSidebarFilter;
  sort: AnnotationSidebarSort;
  limit: number;
  cursor?: string;
}

export interface AnnotationListItem {
  annotation: Annotation;
  pageLabel: string;
  quotePreview: string;
  commentPreview: string;
  textSource: AnnotationTextSource;
  anchorStatus: Annotation["anchorStatus"];
  editable: boolean;
  selected: boolean;
}

export interface AnnotationSidebarPage {
  items: AnnotationListItem[];
  nextCursor: string | null;
  resultCount: number;
  filterCounts: {
    byType: Partial<Record<TextAnnotationType, number>>;
    byColor: Record<string, number>;
    ocr: number;
  };
}

export interface BatchDeleteAnnotationsCommand {
  documentId: DocumentId;
  annotationIds: AnnotationId[];
  actor: ActorContext;
  clientMutationId: string;
}

export interface BatchSetAnnotationColorCommand {
  documentId: DocumentId;
  annotationIds: AnnotationId[];
  color: AnnotationColor;
  actor: ActorContext;
  clientMutationId: string;
}

export interface BatchAnnotationMutationResult {
  batchId: string;
  affected: AnnotationMutationResult[];
  failed: Array<{
    annotationId: AnnotationId;
    code: AnnotationErrorCode;
    message: string;
  }>;
}

// ---------------------------------------------------------------------------
// Sidebar/Reader standard events
// ---------------------------------------------------------------------------

interface AnnotationEventBase<TType extends string, TPayload> {
  protocolVersion: AnnotationProtocolVersion;
  eventId: string;
  sequence: number;
  type: TType;
  documentId: DocumentId;
  fileAssetId: FileAssetId;
  occurredAt: ISODateTime;
  payload: TPayload;
}

export type AnnotationCreatedEvent = AnnotationEventBase<
  "annotation_created",
  {
    annotation: Annotation;
    selectionId: string;
  }
>;

export type AnnotationUpdatedEvent = AnnotationEventBase<
  "annotation_updated",
  {
    annotation: Annotation;
    changedFields: Array<"comment" | "type" | "color">;
  }
>;

export type AnnotationDeletedEvent = AnnotationEventBase<
  "annotation_deleted",
  {
    annotationId: AnnotationId;
    deletedAt: ISODateTime;
  }
>;

export type AnnotationRestoredEvent = AnnotationEventBase<
  "annotation_restored",
  {
    annotation: Annotation;
  }
>;

export type AnnotationSidebarLocatedEvent = AnnotationEventBase<
  "annotation_sidebar_located",
  {
    annotationId: AnnotationId;
    anchorResolveResult: AnchorResolveResult;
  }
>;

export type AnnotationBatchChangedEvent = AnnotationEventBase<
  "annotation_batch_changed",
  {
    batchId: string;
    action: "delete" | "set_color";
    annotationIds: AnnotationId[];
  }
>;

export type AnnotationEvent =
  | AnnotationCreatedEvent
  | AnnotationUpdatedEvent
  | AnnotationDeletedEvent
  | AnnotationRestoredEvent
  | AnnotationSidebarLocatedEvent
  | AnnotationBatchChangedEvent;

export type AnnotationEventType = AnnotationEvent["type"];

export interface AnnotationEventEnvelope {
  event: AnnotationEvent;
  bridge: "trigger" | "state";
}

// ---------------------------------------------------------------------------
// Errors / capabilities
// ---------------------------------------------------------------------------

export type AnnotationErrorCode =
  | "DOCUMENT_MISMATCH"
  | "FILE_MISMATCH"
  | "INVALID_TYPE"
  | "INVALID_COLOR"
  | "EMPTY_SELECTION"
  | "ANCHOR_INVALID"
  | "ANCHOR_NOT_RESOLVABLE"
  | "REVISION_CONFLICT"
  | "ANNOTATION_NOT_FOUND"
  | "READ_ONLY_ANNOTATION"
  | "TEXT_LAYER_UNAVAILABLE"
  | "OCR_TEXT_UNAVAILABLE"
  | "BATCH_PARTIAL_FAILURE"
  | "SEARCH_INDEX_UNAVAILABLE"
  | "INTERNAL_ERROR";

export interface AnnotationModeCapabilities {
  mode: "pdf_text" | "ocr_text" | "png_fallback";
  canCreateTextAnnotation: boolean;
  canEditAnnotation: boolean;
  canDeleteAnnotation: boolean;
  canComment: boolean;
  canSearchSidebar: boolean;
  canLocateFromSidebar: boolean;
  showsTextSourceBadge: boolean;
}

// ---------------------------------------------------------------------------
// Application service
// ---------------------------------------------------------------------------

export interface AnnotationService {
  list(query: AnnotationSidebarQuery, actor: ActorContext): Promise<AnnotationSidebarPage>;

  create(
    command: CreateAnnotationCommand,
    actor: ActorContext
  ): Promise<AnnotationMutationResult>;

  updateComment(
    command: UpdateAnnotationCommentCommand,
    actor: ActorContext
  ): Promise<AnnotationMutationResult>;

  updateStyle(
    command: UpdateAnnotationStyleCommand,
    actor: ActorContext
  ): Promise<AnnotationMutationResult>;

  delete(
    command: DeleteAnnotationCommand,
    actor: ActorContext
  ): Promise<AnnotationMutationResult>;

  restore(
    command: RestoreAnnotationCommand,
    actor: ActorContext
  ): Promise<AnnotationMutationResult>;

  batchDelete(
    command: BatchDeleteAnnotationsCommand,
    actor: ActorContext
  ): Promise<BatchAnnotationMutationResult>;

  batchSetColor(
    command: BatchSetAnnotationColorCommand,
    actor: ActorContext
  ): Promise<BatchAnnotationMutationResult>;

  locateFromSidebar(input: {
    annotationId: AnnotationId;
    documentId: DocumentId;
    fileAssetId: FileAssetId;
  }): Promise<AnnotationSidebarLocatedEvent>;
}

// ---------------------------------------------------------------------------
// Reader integration adapters
// ---------------------------------------------------------------------------

export interface ReaderAnnotationBridge {
  onTextSelected(event: ReaderTextSelectedEvent): Promise<
    | { status: "toolbar_ready"; selection: TextSelection }
    | { status: "ignored"; reason: string }
  >;

  onAnchorResolved(event: ReaderAnchorResolvedEvent): Promise<void>;

  requestLocate(annotation: Annotation): Promise<void>;
}

// This alias documents that sidebar DTOs consume P0-03's text selection type;
// it prevents an accidental duplicate selection model.
export type P0ReaderSelection = TextSelection;
