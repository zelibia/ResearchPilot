/**
 * ResearchPilot v1.16 P0-03 PDF.js reader contracts.
 *
 * Coordinate version: page-relative top-left boxes in [0,1].
 * Page indices are zero-based. UI labels must display `pageIndex + 1`.
 */

import type {
  Annotation,
  AnnotationId,
  DocumentId,
  EvidenceMatchStatus,
  FileAssetId,
  ISODateTime,
  JsonObject,
  PdfRect,
  ProjectId,
  TextLayerStatus,
} from "./P0-01_contracts_v1.16";

export const READER_PROTOCOL_VERSION = "rp.reader.v1";

export type ReaderProtocolVersion = typeof READER_PROTOCOL_VERSION;

export type ReaderMode = "split" | "fullscreen";

export type ReaderLoadState =
  | "idle"
  | "loading_document"
  | "detecting_text_layer"
  | "rendering_first_page"
  | "ready"
  | "ocr_pending"
  | "png_fallback"
  | "reloading_after_ocr"
  | "error";

export type ReaderTextSource = "pdf_text" | "ocr" | "none";

export type ZoomMode = "custom" | "fit_width" | "fit_page";

export interface ReaderMountData {
  protocolVersion: ReaderProtocolVersion;
  instanceId: string;
  documentId: DocumentId;
  projectId: ProjectId;
  mode: ReaderMode;
  file: {
    fileAssetId: FileAssetId;
    fileName: string;
    mimeType: string;
    sha256: string;
    pageCount: number | null;
    textLayer: TextLayerStatus;
    /** Same-origin signed URL. Must support HTTP Range and ETag. */
    contentUrl: string;
  };
  fallback?: {
    mode: "server_png";
    /** Template contains `{page}`; page is 1-based for the image endpoint. */
    pageImageUrlTemplate: string;
    thumbnailUrlTemplate?: string;
  };
  ocr?: {
    jobId: string;
    status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
    progress: number;
    hasPageText: boolean;
    /** Optional same-origin JSON endpoint for DocumentPageText `{page}` substitution. */
    pageTextUrlTemplate?: string;
  };
  initialView: ReaderViewState;
  annotations: Annotation[];
  outlineAnchors?: Array<{
    outlineNodeId: string;
    anchor: ReaderAnchor;
  }>;
  features: {
    thumbnails: boolean;
    fullTextSearch: boolean;
    nativeSelection: boolean;
    ocrFallback: boolean;
    externalAnnotations: boolean;
  };
  preferences: {
    sidebarOpen: boolean;
    sidebarTab: "thumbnails" | "outline" | "annotations";
    thumbnailWidth: number;
    showPageLabels: boolean;
  };
  command?: ReaderCommand;
  debugPerformance: boolean;
}

export interface ReaderViewState {
  pageIndex: number;
  /**
   * Position of the viewport top within the current page:
   * 0 = page top, 1 = page bottom.
   */
  pageScrollRatio: number;
  /**
   * Absolute document scroll ratio retained as a coarse fallback.
   */
  documentScrollRatio: number;
  zoomMode: ZoomMode;
  customZoom: number | null;
  sidebarOpen: boolean;
  sidebarTab: "thumbnails" | "outline" | "annotations";
  activeOutlineNodeId?: string | null;
}

// ---------------------------------------------------------------------------
// Text indexing and anchors
// ---------------------------------------------------------------------------

export interface PageCharacterBox {
  charIndex: number;
  /** Page-relative top-left coordinates in [0,1]. */
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface PageTextItemRange {
  itemIndex: number;
  charStart: number;
  charEnd: number;
  hasEol: boolean;
}

export interface PageTextIndex {
  pageIndex: number;
  source: Exclude<ReaderTextSource, "none">;
  text: string;
  textHash: string;
  items: PageTextItemRange[];
  charBoxes: PageCharacterBox[];
  pageViewBox: {
    width: number;
    height: number;
    rotation: number;
  };
}

export interface TextCharPosition {
  pageIndex: number;
  charOffset: number;
  /** Page-relative top-left coordinate around the character. */
  x: number;
  y: number;
}

export interface ReaderAnchorSegment {
  pageIndex: number;
  quote: string;
  charStart: number;
  charEnd: number;
  rects: NormalizedRect[];
  pageViewBox: PageTextIndex["pageViewBox"];
  textSource: Exclude<ReaderTextSource, "none">;
  textHash: string;
}

export interface NormalizedRect {
  /** All values are fractions of the current page view box, origin top-left. */
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface ReaderAnchor {
  protocolVersion: ReaderProtocolVersion;
  anchorId?: string;
  documentId: DocumentId;
  fileAssetId: FileAssetId;
  start: TextCharPosition;
  end: TextCharPosition;
  quote: string;
  segments: ReaderAnchorSegment[];
  matchStatus: EvidenceMatchStatus;
  coordinateVersion: "page-normalized-v1";
  createdAt: ISODateTime;
}

export interface TextSelection {
  protocolVersion: ReaderProtocolVersion;
  selectionId: string;
  documentId: DocumentId;
  fileAssetId: FileAssetId;
  text: string;
  granularity: "character" | "word" | "sentence" | "paragraph" | "custom";
  direction: "forward" | "backward";
  start: TextCharPosition;
  end: TextCharPosition;
  segments: ReaderAnchorSegment[];
  pageViewBox: PageTextIndex["pageViewBox"];
  createdAt: ISODateTime;
}

export interface AnchorResolveResult {
  requestedAnchor: ReaderAnchor;
  resolvedPageIndex: number;
  strategy: "char_offset_and_quote" | "quote_search" | "normalized_rect" | "page_only" | "none";
  matchStatus: EvidenceMatchStatus;
  rects: NormalizedRect[];
  reason: string;
  highlightRendered: boolean;
}

// ---------------------------------------------------------------------------
// Search / zoom / load
// ---------------------------------------------------------------------------

export interface SearchResultMatch {
  matchIndex: number;
  pageIndex: number;
  charStart: number;
  charEnd: number;
  quote: string;
  rects: NormalizedRect[];
  matchStatus: EvidenceMatchStatus;
}

export interface ReaderSearchState {
  query: string;
  status: "idle" | "indexing" | "searching" | "done" | "failed";
  total: number;
  currentIndex: number;
  matches: SearchResultMatch[];
  errorMessage: string;
}

export interface ReaderZoomState {
  mode: ZoomMode;
  scale: number;
  minScale: number;
  maxScale: number;
  containerWidth: number;
  containerHeight: number;
}

export interface ReaderViewportChanged {
  pageIndex: number;
  pageLabel: string;
  pageScrollRatio: number;
  documentScrollRatio: number;
  visiblePageIndices: number[];
  activeOutlineNodeId: string | null;
}

export interface ReaderCapabilities {
  textLayer: TextLayerStatus;
  textSource: ReaderTextSource;
  canSelectText: boolean;
  canSearchText: boolean;
  canResolveQuoteAnchor: boolean;
  canResolveRectAnchor: boolean;
  canRenderAnnotations: boolean;
  fallbackMode: "none" | "server_png";
}

// ---------------------------------------------------------------------------
// Commands: Python -> JS
// ---------------------------------------------------------------------------

export type ReaderCommand =
  | {
      seq: number;
      type: "goto_page";
      documentId: DocumentId;
      pageIndex: number;
      highlight?: boolean;
    }
  | {
      seq: number;
      type: "goto_anchor";
      documentId: DocumentId;
      anchor: ReaderAnchor;
      behavior?: "auto" | "smooth";
    }
  | {
      seq: number;
      type: "search";
      documentId: DocumentId;
      query: string;
      caseSensitive?: boolean;
    }
  | {
      seq: number;
      type: "search_next";
      documentId: DocumentId;
      direction: "next" | "previous";
    }
  | {
      seq: number;
      type: "set_zoom";
      documentId: DocumentId;
      mode: ZoomMode;
      value?: number;
    }
  | {
      seq: number;
      type: "set_sidebar";
      documentId: DocumentId;
      open: boolean;
      tab?: "thumbnails" | "outline" | "annotations";
    }
  | {
      seq: number;
      type: "clear_highlight";
      documentId: DocumentId;
    }
  | {
      seq: number;
      type: "reload_document";
      documentId: DocumentId;
    };

// ---------------------------------------------------------------------------
// Events: JS -> Python / downstream modules
// ---------------------------------------------------------------------------

interface ReaderEventBase<TType extends string, TPayload> {
  protocolVersion: ReaderProtocolVersion;
  eventId: string;
  sequence: number;
  type: TType;
  documentId: DocumentId;
  fileAssetId: FileAssetId;
  occurredAt: ISODateTime;
  payload: TPayload;
}

export type ReaderLoadStateChangedEvent = ReaderEventBase<
  "load_state_changed",
  {
    state: ReaderLoadState;
    progress: number | null;
    message: string;
    capabilities: ReaderCapabilities;
  }
>;

export type ReaderTextSelectedEvent = ReaderEventBase<
  "text_selected",
  {
    selection: TextSelection;
  }
>;

export type ReaderViewportChangedEvent = ReaderEventBase<
  "viewport_changed",
  ReaderViewportChanged
>;

export type ReaderZoomChangedEvent = ReaderEventBase<
  "zoom_changed",
  ReaderZoomState
>;

export type ReaderAnchorResolvedEvent = ReaderEventBase<
  "anchor_resolved",
  AnchorResolveResult
>;

export type ReaderSearchStateChangedEvent = ReaderEventBase<
  "search_state_changed",
  ReaderSearchState
>;

export type ReaderThumbnailSelectedEvent = ReaderEventBase<
  "thumbnail_selected",
  {
    pageIndex: number;
  }
>;

export type ReaderAnnotationClickedEvent = ReaderEventBase<
  "annotation_clicked",
  {
    annotationId: AnnotationId;
  }
>;

export type ReaderOcrRequiredEvent = ReaderEventBase<
  "ocr_required",
  {
    reason: "no_text_layer" | "low_text_coverage";
    sampledPages: number;
    detectedCharacters: number;
  }
>;

export type ReaderErrorEvent = ReaderEventBase<
  "reader_error",
  {
    code:
      | "PDF_LOAD_FAILED"
      | "PDF_PASSWORD_REQUIRED"
      | "PDF_INVALID"
      | "PDF_RANGE_REQUEST_FAILED"
      | "WORKER_LOAD_FAILED"
      | "TEXT_LAYER_FAILED"
      | "SEARCH_FAILED"
      | "ANCHOR_FAILED"
      | "OCR_REQUIRED"
      | "RENDER_FAILED";
    message: string;
    recoverable: boolean;
  }
>;

export type ReaderEvent =
  | ReaderLoadStateChangedEvent
  | ReaderTextSelectedEvent
  | ReaderViewportChangedEvent
  | ReaderZoomChangedEvent
  | ReaderAnchorResolvedEvent
  | ReaderSearchStateChangedEvent
  | ReaderThumbnailSelectedEvent
  | ReaderAnnotationClickedEvent
  | ReaderOcrRequiredEvent
  | ReaderErrorEvent;

export type ReaderEventType = ReaderEvent["type"];

/** The single object passed through a Streamlit trigger callback. */
export interface ReaderEventEnvelope {
  event: ReaderEvent;
  bridge: "trigger" | "state";
}

// ---------------------------------------------------------------------------
// OCR sidecar data
// ---------------------------------------------------------------------------

export interface DocumentPageText {
  id: string;
  documentId: DocumentId;
  fileAssetId: FileAssetId;
  pageIndex: number;
  source: "pdf_text" | "ocr";
  text: string;
  textHash: string;
  charMap: PageCharacterBox[];
  words: Array<{
    text: string;
    charStart: number;
    charEnd: number;
    x: number;
    y: number;
    width: number;
    height: number;
    confidence: number | null;
  }>;
  ocrJobId: string | null;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
}

export interface ReaderOcrJob {
  id: string;
  documentId: DocumentId;
  fileAssetId: FileAssetId;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  engine: string;
  language: string;
  progress: number;
  outputFileAssetId: FileAssetId | null;
  errorCode: string;
  errorMessage: string;
  leaseOwner: string;
  leaseExpiresAt: ISODateTime | null;
  requestedBy: string;
  createdAt: ISODateTime;
  startedAt: ISODateTime | null;
  finishedAt: ISODateTime | null;
  updatedAt: ISODateTime;
}

// ---------------------------------------------------------------------------
// Python wrapper API
// ---------------------------------------------------------------------------

export interface ReaderMountResult {
  viewState: Partial<ReaderViewState>;
  lastEvent: ReaderEvent | null;
}

export interface ReaderService {
  getMountData(input: {
    projectId: ProjectId;
    documentId: DocumentId;
    fileAssetId: FileAssetId;
    mode: ReaderMode;
    initialAnchor?: ReaderAnchor;
    initialPageIndex?: number;
    command?: ReaderCommand;
  }): Promise<ReaderMountData>;

  handleEvent(event: ReaderEvent): Promise<void>;

  requestOcr(input: {
    documentId: DocumentId;
    fileAssetId: FileAssetId;
    requestedBy: string;
    reason: ReaderOcrRequiredEvent["payload"]["reason"];
  }): Promise<ReaderOcrJob>;

  getOcrStatus(input: {
    documentId: DocumentId;
    fileAssetId: FileAssetId;
  }): Promise<ReaderOcrJob | null>;

  getPageText(input: {
    documentId: DocumentId;
    fileAssetId: FileAssetId;
    pageIndex: number;
    source?: "pdf_text" | "ocr";
  }): Promise<DocumentPageText | null>;
}

// Type anchors for downstream modules.
export type ReaderP0Types = Annotation | PdfRect | JsonObject;
