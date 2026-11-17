/**
 * ResearchPilot v1.16 P0-05 outline editing and revision contracts.
 *
 * Canonical content is shared by document. Project-specific completion state
 * is stored separately and never changes the shared revision.
 */

import type {
  ActorContext,
  AnnotationId,
  Annotation,
  DocumentId,
  ISODateTime,
  JsonObject,
  ProjectId,
} from "./P0-01_contracts_v1.16";

import type {
  ReaderAnchor,
  ReaderAnchorResolvedEvent,
  ReaderTextSelectedEvent,
  TextSelection,
} from "./P0-03_reader_contracts_v1.16";

export const OUTLINE_PROTOCOL_VERSION = "rp.outline.v1";
export type OutlineProtocolVersion = typeof OUTLINE_PROTOCOL_VERSION;

export type OutlineKind = "reading_outline" | "paper_structure";
export type OutlineRevisionStatus = "applied" | "ai_candidate" | "rejected";
export type OutlineRevisionSource =
  | "manual"
  | "ai"
  | "ai_merge"
  | "rollback"
  | "legacy"
  | "migration";

export interface ReadingOutline {
  id: string;
  documentId: DocumentId;
  kind: OutlineKind;
  currentRevisionId: string | null;
  currentRevisionNo: number;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
}

export interface OutlineNode {
  id: string;
  outlineId: string;
  parentId: string | null;
  orderIndex: number;
  headingLevel: 1 | 2 | 3 | 4 | 5 | 6;
  title: string;
  bodyMarkdown: string;
  anchor: ReaderAnchor | null;
  annotationIds: AnnotationId[];
  noteMarkdown: string;
  locked: boolean;
  userEdited: boolean;
  origin: "ai" | "user" | "mixed" | "legacy";
  contentHash: string;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
}

export interface OutlineTreeNode {
  nodeId: string;
  title: string;
  headingLevel: 1 | 2 | 3 | 4 | 5 | 6;
  orderIndex: number;
  bodyMarkdown: string;
  anchor: ReaderAnchor | null;
  annotationIds: AnnotationId[];
  noteMarkdown: string;
  locked: boolean;
  origin: "ai" | "user" | "mixed" | "legacy";
  children: OutlineTreeNode[];
}

export interface OutlineRevision {
  id: string;
  outlineId: string;
  revisionNo: number;
  baseRevisionId: string | null;
  baseRevisionNo: number | null;
  status: OutlineRevisionStatus;
  source: OutlineRevisionSource;
  tree: OutlineTreeNode[];
  markdown: string;
  changeSummary: string;
  createdBy: string;
  createdAt: ISODateTime;
  contentHash: string;
  aiRunId: string | null;
  legacySource: JsonObject;
}

export interface OutlineNodeView extends OutlineNode {
  completed: boolean;
  completedAt: string | null;
  lastViewedAt: string | null;
}

export interface OutlineEditorBootstrap {
  outline: ReadingOutline;
  currentRevision: OutlineRevision;
  nodes: OutlineNodeView[];
  revisionHistory: Array<
    Pick<
      OutlineRevision,
      "id" | "revisionNo" | "baseRevisionNo" | "status" | "source"
      | "changeSummary" | "createdBy" | "createdAt"
    >
  >;
  projectProgress: {
    completedCount: number;
    totalCount: number;
  };
  pendingConflicts: OutlineMergeConflict[];
  capabilities: OutlineCapabilities;
}

export interface OutlineCapabilities {
  canEdit: boolean;
  canRegenerate: boolean;
  canCompare: boolean;
  canRollback: boolean;
  canMarkComplete: boolean;
  canLock: boolean;
  canAttachAnnotation: boolean;
}

export interface ProjectOutlineNodeState {
  projectId: ProjectId;
  outlineId: string;
  nodeId: string;
  completed: boolean;
  completedAt: ISODateTime | null;
  lastViewedAt: ISODateTime | null;
  updatedAt: ISODateTime;
}

// ---------------------------------------------------------------------------
// Editing commands
// ---------------------------------------------------------------------------

interface OutlineEditBase {
  outlineId: string;
  baseRevisionNo: number;
  actor: ActorContext;
  clientMutationId: string;
  changeSummary?: string;
}

export interface AddOutlineNodeCommand extends OutlineEditBase {
  type: "add_node";
  parentNodeId: string | null;
  insertAfterNodeId?: string | null;
  title: string;
  headingLevel: 1 | 2 | 3 | 4 | 5 | 6;
  bodyMarkdown?: string;
  anchor?: ReaderAnchor | null;
  noteMarkdown?: string;
}

export interface DeleteOutlineNodesCommand extends OutlineEditBase {
  type: "delete_nodes";
  nodeIds: string[];
}

export interface RenameOutlineNodeCommand extends OutlineEditBase {
  type: "rename_node";
  nodeId: string;
  title: string;
}

export interface MoveOutlineNodesCommand extends OutlineEditBase {
  type: "move_nodes";
  moves: Array<{
    nodeId: string;
    parentNodeId: string | null;
    orderIndex: number;
  }>;
}

export interface IndentOutlineNodesCommand extends OutlineEditBase {
  type: "indent_nodes" | "outdent_nodes";
  nodeIds: string[];
}

export interface UpdateOutlineNodeContentCommand extends OutlineEditBase {
  type: "update_node_content";
  nodeId: string;
  bodyMarkdown?: string;
  noteMarkdown?: string;
}

export interface SetOutlineNodeLockCommand extends OutlineEditBase {
  type: "set_lock";
  nodeId: string;
  locked: boolean;
}

export interface AttachOutlineAnnotationsCommand extends OutlineEditBase {
  type: "attach_annotations";
  nodeId: string;
  annotationIds: AnnotationId[];
  relationType?: "evidence" | "related" | "follow_up";
}

export interface SaveOutlineMarkdownCommand extends OutlineEditBase {
  type: "save_markdown";
  markdown: string;
  parserMode: "replace_tree" | "preserve_node_ids";
}

export type OutlineEditCommand =
  | AddOutlineNodeCommand
  | DeleteOutlineNodesCommand
  | RenameOutlineNodeCommand
  | MoveOutlineNodesCommand
  | IndentOutlineNodesCommand
  | UpdateOutlineNodeContentCommand
  | SetOutlineNodeLockCommand
  | AttachOutlineAnnotationsCommand
  | SaveOutlineMarkdownCommand;

export interface OutlineMutationResult {
  outline: ReadingOutline;
  revision: OutlineRevision;
  nodes: OutlineNodeView[];
  events: OutlineEvent[];
}

export interface MarkdownParsePreview {
  markdown: string;
  tree: OutlineTreeNode[];
  warnings: string[];
  matchedNodeIds: string[];
  newNodeCount: number;
  removedNodeCount: number;
}

// ---------------------------------------------------------------------------
// AI regeneration and deterministic merge
// ---------------------------------------------------------------------------

export interface OutlineAIInput {
  documentId: DocumentId;
  documentTitle: string;
  metadata: {
    authors: string[];
    year: string;
    venue: string;
    abstract: string;
  };
  baseRevisionNo: number;
  currentTree: OutlineTreeNode[];
  fulltextSections: Array<{
    title: string;
    pageStart: number;
    pageEnd: number;
    text: string;
  }>;
  existingAnnotations: Array<Pick<
    Annotation,
    "id" | "type" | "quote" | "commentMarkdown" | "pageIndex" | "textSource"
  >>;
  promptVersion: string;
}

export interface OutlineAIGeneratedNode {
  tempId: string;
  parentTempId: string | null;
  orderIndex: number;
  headingLevel: 1 | 2 | 3 | 4 | 5 | 6;
  title: string;
  bodyMarkdown: string;
  anchorHint?: {
    pageIndex: number;
    quote: string;
    charStart?: number | null;
    charEnd?: number | null;
  } | null;
}

export interface OutlineAIResult {
  schemaVersion: "outline.v1";
  summary: string;
  nodes: OutlineAIGeneratedNode[];
  markdown: string;
  warnings: string[];
}

export interface OutlineAIRun {
  id: string;
  outlineId: string;
  baseRevisionId: string | null;
  baseRevisionNo: number;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  provider: string;
  model: string;
  promptVersion: string;
  inputHash: string;
  inputScope: JsonObject;
  result: OutlineAIResult | null;
  errorCode: string;
  errorMessage: string;
  requestedBy: string;
  startedAt: ISODateTime | null;
  finishedAt: ISODateTime | null;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
}

export type OutlineMergeNodeStatus =
  | "kept_locked"
  | "kept_user_edited"
  | "updated_from_ai"
  | "added_from_ai"
  | "user_removed_kept"
  | "ai_removed_suggested"
  | "conflict";

export interface OutlineMergeNodeResult {
  nodeId: string;
  status: OutlineMergeNodeStatus;
  current: OutlineTreeNode | null;
  candidate: OutlineTreeNode | null;
  base: OutlineTreeNode | null;
  reason: string;
}

export interface OutlineMergePreview {
  aiRun: OutlineAIRun;
  candidateRevision: OutlineRevision;
  baseRevisionNo: number;
  currentRevisionNo: number;
  mergeNodes: OutlineMergeNodeResult[];
  conflicts: OutlineMergeConflict[];
  autoAddedCount: number;
  autoUpdatedCount: number;
  preservedCount: number;
  suggestedRemovalCount: number;
}

export type OutlineMergeConflictType =
  | "both_modified"
  | "user_deleted_ai_modified"
  | "ai_deleted_user_modified"
  | "move_vs_content"
  | "anchor_conflict"
  | "parent_conflict";

export type OutlineConflictResolution =
  | "pending"
  | "keep_current"
  | "accept_ai"
  | "manual_merge"
  | "dismissed";

export interface OutlineMergeConflict {
  id: string;
  outlineId: string;
  aiRunId: string;
  candidateRevisionId: string;
  baseRevisionNo: number;
  currentRevisionNo: number;
  nodeId: string | null;
  conflictType: OutlineMergeConflictType;
  baseNode: OutlineTreeNode | null;
  currentNode: OutlineTreeNode | null;
  aiNode: OutlineTreeNode | null;
  resolution: OutlineConflictResolution;
  resolvedBy: string;
  resolvedAt: ISODateTime | null;
  createdAt: ISODateTime;
}

export interface ResolveOutlineConflictCommand {
  conflictId: string;
  baseRevisionNo: number;
  resolution: Exclude<OutlineConflictResolution, "pending">;
  mergedNode?: OutlineTreeNode;
  actor: ActorContext;
  clientMutationId: string;
}

// ---------------------------------------------------------------------------
// Version comparison / restore
// ---------------------------------------------------------------------------

export type OutlineDiffStatus =
  | "added"
  | "removed"
  | "renamed"
  | "body_changed"
  | "moved"
  | "anchor_changed"
  | "annotation_links_changed"
  | "note_changed"
  | "locked_changed"
  | "unchanged";

export interface OutlineNodeDiff {
  nodeId: string;
  status: OutlineDiffStatus;
  before: OutlineTreeNode | null;
  after: OutlineTreeNode | null;
}

export interface OutlineRevisionDiff {
  outlineId: string;
  leftRevisionNo: number;
  rightRevisionNo: number;
  nodeDiffs: OutlineNodeDiff[];
  markdownDiff: string;
  counts: Record<OutlineDiffStatus, number>;
}

export interface RestoreOutlineRevisionCommand {
  outlineId: string;
  sourceRevisionNo: number;
  baseRevisionNo: number;
  changeSummary: string;
  actor: ActorContext;
  clientMutationId: string;
}

// ---------------------------------------------------------------------------
// Project progress (does not create a shared revision)
// ---------------------------------------------------------------------------

export interface SetOutlineNodeCompletedCommand {
  projectId: ProjectId;
  outlineId: string;
  nodeId: string;
  completed: boolean;
  actor: ActorContext;
}

// ---------------------------------------------------------------------------
// Standard events
// ---------------------------------------------------------------------------

interface OutlineEventBase<TType extends string, TPayload> {
  protocolVersion: OutlineProtocolVersion;
  eventId: string;
  sequence: number;
  type: TType;
  documentId: DocumentId;
  outlineId: string;
  occurredAt: ISODateTime;
  payload: TPayload;
}

export type OutlineNodeClickedEvent = OutlineEventBase<
  "outline_node_clicked",
  {
    nodeId: string;
    revisionNo: number;
    anchor: ReaderAnchor | null;
    requestedReaderCommandSeq: number | null;
  }
>;

export type OutlineUpdatedEvent = OutlineEventBase<
  "outline_updated",
  {
    revisionId: string;
    revisionNo: number;
    baseRevisionNo: number | null;
    source: OutlineRevisionSource;
    changedNodeIds: string[];
    changeSummary: string;
  }
>;

export type RevisionSwitchEvent = OutlineEventBase<
  "revision_switch",
  {
    fromRevisionNo: number | null;
    toRevisionNo: number;
    mode: "compare" | "view";
    becameCurrent: boolean;
  }
>;

export type AIRegenerateRequestEvent = OutlineEventBase<
  "ai_regenerate_request",
  {
    baseRevisionNo: number;
    promptVersion: string;
    includeAnnotations: boolean;
  }
>;

export type OutlineGenerationStateEvent = OutlineEventBase<
  "outline_generation_state",
  {
    aiRunId: string;
    state: "queued" | "running" | "succeeded" | "failed" | "cancelled";
    message: string;
  }
>;

export type OutlineMergeReadyEvent = OutlineEventBase<
  "outline_merge_ready",
  {
    aiRunId: string;
    candidateRevisionId: string;
    conflictCount: number;
  }
>;

export type OutlineProgressUpdatedEvent = OutlineEventBase<
  "outline_progress_updated",
  {
    projectId: ProjectId;
    nodeId: string;
    completed: boolean;
  }
>;

export type OutlineEvent =
  | OutlineNodeClickedEvent
  | OutlineUpdatedEvent
  | RevisionSwitchEvent
  | AIRegenerateRequestEvent
  | OutlineGenerationStateEvent
  | OutlineMergeReadyEvent
  | OutlineProgressUpdatedEvent;

export type OutlineEventType = OutlineEvent["type"];

export interface OutlineEventEnvelope {
  event: OutlineEvent;
  bridge: "trigger" | "state";
}

// ---------------------------------------------------------------------------
// Service and Reader bridge
// ---------------------------------------------------------------------------

export interface OutlineService {
  getBootstrap(documentId: DocumentId, projectId: ProjectId): Promise<OutlineEditorBootstrap>;

  applyEdit(
    command: OutlineEditCommand,
    actor: ActorContext
  ): Promise<OutlineMutationResult>;

  parseMarkdownPreview(input: {
    outlineId: string;
    revisionNo: number;
    markdown: string;
  }): Promise<MarkdownParsePreview>;

  compareRevisions(
    outlineId: string,
    leftRevisionNo: number,
    rightRevisionNo: number
  ): Promise<OutlineRevisionDiff>;

  restoreRevision(command: RestoreOutlineRevisionCommand): Promise<OutlineMutationResult>;

  requestAIRegeneration(input: {
    outlineId: string;
    baseRevisionNo: number;
    includeAnnotations: boolean;
    actor: ActorContext;
    clientMutationId: string;
  }): Promise<OutlineAIRun>;

  resolveConflict(command: ResolveOutlineConflictCommand): Promise<OutlineMergePreview>;

  applyMergeCandidate(input: {
    outlineId: string;
    candidateRevisionId: string;
    baseRevisionNo: number;
    actor: ActorContext;
    clientMutationId: string;
  }): Promise<OutlineMutationResult>;

  setNodeCompleted(command: SetOutlineNodeCompletedCommand): Promise<ProjectOutlineNodeState>;
}

export interface OutlineReaderBridge {
  onTextSelectedForOutline(event: ReaderTextSelectedEvent): Promise<{
    action: "open_new_node_draft" | "ignored";
    selection?: TextSelection;
    reason?: string;
  }>;

  onAnchorResolved(event: ReaderAnchorResolvedEvent): Promise<void>;

  requestReaderLocate(node: OutlineNodeView): Promise<void>;
}
