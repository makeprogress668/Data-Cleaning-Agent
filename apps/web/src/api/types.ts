export type JobStatus =
  | 'pending'
  | 'running'
  | 'needs_clarification'
  | 'awaiting_confirmation'
  | 'succeeded'
  | 'failed'
  | 'cancelled';

export type JobMode = 'discover' | 'plan' | 'answer';

export interface ClarificationQuestion {
  id: string;
  priority: string;
  question: string;
  impact: string;
  // When present, the question is a single-select over these choices (P1); the
  // console renders one-click option cards instead of a free-text box.
  kind?: string;
  options?: string[];
}

export interface MissingSlot {
  slot_id: string;
  kind:
    | 'primary_table'
    | 'target_field'
    | 'business_rule'
    | 'retention_rule'
    | 'output_requirement'
    | 'confirmation';
  question: string;
  impact: string;
  priority: 'high' | 'medium';
  options: string[];
}

export type ActionAuthorization = 'stated' | 'implied' | 'inferred';
export type TaskRuleSource = 'goal_text' | 'llm';
export type TaskFilterOperator =
  | 'gt'
  | 'gte'
  | 'lt'
  | 'lte'
  | 'equals'
  | 'not_equals'
  | 'in'
  | 'not_in';
export type TaskAggregationName =
  | 'count'
  | 'sum'
  | 'mean'
  | 'min'
  | 'max'
  | 'median'
  | 'nunique'
  | 'first'
  | 'last';

export interface RetentionRule {
  source?: 'goal_text' | 'llm';
  authorization?: ActionAuthorization;
  evidence?: string;
  field?: string;
  op?: TaskFilterOperator;
  mode?: 'keep' | 'exclude';
  value?: unknown;
  values?: unknown[];
  fields?: string[];
  strategy?: 'first' | 'last' | 'latest' | 'earliest';
  order_by?: string;
}

export interface TaskFilter extends RetentionRule {
  field: string;
  op: TaskFilterOperator;
  mode: 'keep' | 'exclude';
}

export interface TaskDeduplication extends RetentionRule {
  fields: string[];
  strategy: 'first' | 'last' | 'latest' | 'earliest';
}

export interface TaskDerivation {
  kind?: 'split';
  output?: string;
  branches?: Array<{
    when: {
      field: string;
      op: TaskFilterOperator;
      value?: unknown;
      values?: unknown[];
    };
    then: string;
  }>;
  otherwise?: string;
  source?: string;
  sep?: string;
  outputs?: string[];
}

export interface TaskAggregation {
  group_by: string[];
  metrics: Array<{
    column: string | null;
    agg: TaskAggregationName;
    output_name: string;
  }>;
  source?: TaskRuleSource;
  column_field?: string;
  time_bucket?: {
    field: string;
    granularity: 'week' | 'month' | 'quarter' | 'year';
    output_name: string;
  };
  top_n?: number;
  rank_within?: string[];
}

export interface TaskSpec {
  version: 1;
  objective: string;
  primary_entity?: string | null;
  target_tables: string[];
  target_fields: Array<{ table: string; field: string }>;
  actions: Array<
    | 'clean'
    | 'filter'
    | 'deduplicate'
    | 'lookup'
    | 'derive'
    | 'validate'
    | 'analyze'
    | 'chart'
    | 'review'
    | 'annotate'
    | 'export'
  >;
  /** `source` identifies where a row rule came from. Quote authorization and
   * impact-based confirmation are evaluated separately by the backend. */
  filters: TaskFilter[];
  deduplication: TaskDeduplication[];
  derivations: TaskDerivation[];
  union: Array<{
    tables: string[];
    source?: TaskRuleSource;
    authorization?: ActionAuthorization;
  }>;
  rollups: Array<{
    source_table: string;
    left_key: string;
    right_key: string;
    measure: string;
    agg: TaskAggregationName;
    output: string;
  }>;
  melt: {
    table: string;
    value_columns: string[];
    variable_name: string;
    value_name: string;
  } | Record<never, never>;
  target_schema: string[];
  aggregation: TaskAggregation | Record<never, never>;
  join_requirements: Array<{
    required: boolean;
    candidate_tables: string[];
  }>;
  business_rules: string[];
  acceptance_criteria: string[];
  output_intent: {
    requested_outputs?: string[];
    business_views?: string[];
    needs_charts?: boolean;
    needs_review?: boolean;
    needs_change_manifest?: boolean;
  };
  assumptions: string[];
  unmet_actions: TaskSpec['actions'];
  confidence: 'high' | 'medium' | 'low';
  missing_slots: MissingSlot[];
}

export interface ExecutionStep {
  step_id: string;
  capability_id: string;
  capability_version: string;
  depends_on: string[];
  risk_level: 'low' | 'medium' | 'high';
  requires_confirmation: boolean;
  confirmation_reasons: ConfirmationReason[];
  impact_estimate?: {
    estimate_available: boolean;
    input_rows: number;
    estimated_removed_rows: number | null;
    removal_ratio: number | null;
    high_removal_ratio_threshold: number;
  } | null;
}

export type ConfirmationReason =
  | 'missing_authorization'
  | 'impact_unknown'
  | 'high_removal_ratio'
  | 'fuzzy_matching';

export interface ExecutionPlan {
  version: 1 | 2;
  steps: ExecutionStep[];
}

export interface PlannerMetadata {
  goal_prompt_version: string;
  planner_prompt_version: string;
  model: string;
  capability_registry_hash: string;
}

export interface OutputSpec {
  version: 1;
  primary_artifact: {
    type: 'table';
    name: string;
    fields: string[];
    description: string;
  };
  secondary_artifacts: Array<{
    type: 'table';
    name: string;
    fields: string[];
    description: string;
  }>;
  charts: Array<{
    chart_id: string;
    business_question: string;
    dimension: string;
    metric: string;
    chart_type: string;
    file_format: string;
  }>;
  report: {
    enabled: boolean;
    detail_level: string;
    formats: string[];
  };
  include_review_view: boolean;
  include_audit: boolean;
}

export interface JobSummary {
  job_id: string;
  status: JobStatus;
  mode: JobMode;
  goal?: string;
  created_at?: string;
  updated_at?: string;
  queued_at?: string;
  started_at?: string;
  completed_at?: string;
  queue_wait_ms?: number | null;
  dispatch_attempts?: number;
  sla_seconds?: number | null;
  sla_breached?: boolean;
  error_message?: string;
  failure_stage?: 'planning' | 'execution' | 'delivery';
  clarification_questions?: ClarificationQuestion[];
  session_id?: string;
  clarification_round?: number;
  max_clarification_rounds?: number;
  task_spec_version?: number;
  plan_version?: number;
  review_count?: number;
  pending_review_count?: number;
  counts?: {
    total: number;
    valid: number;
    abnormal: number;
  };
  quality_score?: number | null;
  result_row_count?: number;
  input_files?: string[];
  has_discovery?: boolean;
  has_plan?: boolean;
  has_result?: boolean;
}

export interface OutputFile {
  name: string;
  url: string;
  type: string;
}

export interface ChartItem {
  title: string;
  url: string;
  type?: string;
}

export interface KeyMetric {
  label: string;
  value: string | number;
  description?: string;
}

export interface ReviewItem {
  id: string;
  issue_type: string;
  severity: string;
  source_row: string;
  affected_field: string;
  current_value: string;
  recommended_action: string;
  /** Position of this row in the persisted result snapshot; absent on legacy jobs. */
  result_index?: number;
}

/** Outcome of applying saved review decisions back into a downloadable result. */
export interface ReviewApplied {
  accepted_rows: number;
  excluded_rows: number;
  pending_rows: number;
  result_row_count: number;
  file_name: string;
  updated_at?: string;
}

/**
 * 一次执行里每个步骤的实测记录。这里只取解释「汇总和明细为什么对不上」需要的那几个
 * 数：合计行没算进去、有几个数读不出来。后端一直在记，但没有任何界面读它，于是这些
 * 解释谁也看不到 —— 而它们正是用户看到一个奇怪数字时会问的问题。
 */
export interface ExecutionEvent {
  step_id?: string;
  capability_id?: string;
  status?: string;
  metrics?: Record<string, unknown>;
}

export interface JobEvents {
  job_id: string;
  status?: string;
  execution_events?: ExecutionEvent[];
}

export interface GoalUnderstanding {
  // 'deterministic' 是按规则理解（没配模型，符合预期）；'deterministic_fallback' 是
  // 配了模型但没连上 —— 两者的结果差别很大，必须让用户分得清。
  understanding_source?:
    | 'deterministic'
    | 'deterministic_fallback'
    | 'llm'
    | 'llm_cached';
  understanding_fallback_reason?: string;
  understanding_confidence?: 'high' | 'medium' | 'low';
  is_generic_fallback?: boolean;
  assumptions?: string[];
  focus?: string[];
  business_views?: string[];
  requested_outputs?: string[];
  matched_fields?: Array<{ table: string; field: string }>;
  field_matches?: Array<{
    table: string;
    field: string;
    score?: number;
    confidence?: string;
    match_type?: string;
    evidence?: string[];
    matched_terms?: string[];
    sample_values?: string[];
  }>;
  table_matches?: Array<{
    table: string;
    score?: number;
    confidence?: string;
    row_count?: number;
    column_count?: number;
    matched_fields?: string[];
    matched_terms?: string[];
    reason?: string;
  }>;
  data_location?: {
    summary?: string;
    target_tables?: string[];
    primary_table?: string;
    high_confidence_fields?: string[];
    coverage?: Record<string, number>;
  };
  execution_steps?: Array<{
    step: number;
    name: string;
    objective: string;
    inputs?: string[];
    outputs?: string[];
    evidence?: string[];
  }>;
  suggested_base_table?: string;
  data_access?: {
    mode: 'metadata_only' | 'masked_samples' | 'trusted_samples';
    sample_limit_per_field: number;
    raw_samples_allowed: boolean;
    masked_fields: string[];
    column_count: number;
  };
  task_spec?: TaskSpec;
  impact_preview?: TaskImpactPreview;
}

export interface TaskImpactPreview {
  base_table: string;
  input_rows: number;
  estimated_output_rows: number | null;
  estimated_removed_rows: number | null;
  estimated_removal_ratio: number | null;
  estimate_available: boolean;
  affected_fields: string[];
  added_fields: string[];
}

export interface BusinessAnswer {
  job_id: string;
  goal: string;
  goal_understanding?: GoalUnderstanding;
  output_spec?: OutputSpec;
  final_conclusion: string;
  key_metrics: KeyMetric[];
  key_findings: string[];
  data_quality_score: number;
  result_preview: Array<Record<string, unknown>>;
  exception_impact: Array<Record<string, unknown>>;
  review_items: ReviewItem[];
  review_item_count: number;
  review_items_truncated: boolean;
  review_applied?: ReviewApplied | null;
  can_rematerialize?: boolean;
  recommended_actions: string[];
  charts: ChartItem[];
  output_files: OutputFile[];
  selected_result_version_id?: string;
  result_versions?: ResultVersion[];
}

export interface ResultVersion {
  version_id: string;
  version_number: number;
  parent_version_id: string | null;
  kind: 'original' | 'review' | string;
  file_name: string;
  url: string;
  summary: Record<string, unknown>;
  created_at?: string;
}

export interface ResultVersionState {
  job_id: string;
  selected_version_id: string;
  versions: ResultVersion[];
}

export interface RecipeSummary {
  recipe_id: string;
  name: string;
  description: string;
  goal: string;
  created_at: string;
  updated_at: string;
}

export interface SemanticModelSummary {
  semantic_model_id: string;
  name: string;
  description: string;
  created_at: string;
  updated_at: string;
}

export interface ReviewRow extends ReviewItem {
  review_status: 'pending' | 'accepted' | 'excluded';
}

// These row shapes are rendered by the generic DataPreviewTable, which expects
// Record<string, unknown>. Declared as type aliases (not interfaces) so they carry
// an implicit string index signature and stay assignable to that prop type.
export type DiscoveryTable = {
  name: string;
  rows: number;
  columns: number;
  role: string;
};

export type DiscoveryKey = {
  table: string;
  field: string;
  confidence: string;
  uniqueness: string;
};

export type DiscoveryRelationship = {
  from: string;
  to: string;
  match_rate: string;
  recommendation: string;
};

export interface DiscoveryView {
  job_id: string;
  goal: string;
  files: string[];
  tables: DiscoveryTable[];
  keys: DiscoveryKey[];
  relationships: DiscoveryRelationship[];
  issues: string[];
}

export interface PlanView {
  job_id: string;
  goal: string;
  main_table: string;
  lookups: string[];
  formulas: string[];
  anomaly_rules: string[];
  document_rules: string[];
  risks: string[];
  execution_steps: ExecutionStep[];
  output_spec: OutputSpec;
  planner_metadata: PlannerMetadata;
  impact_preview: TaskImpactPreview;
}

export interface RuntimeConfig {
  service: {
    available: boolean;
  };
  /** Whether goal understanding is running on the model or the rule engine. */
  understanding?: {
    mode: 'llm' | 'deterministic';
    state: 'configured' | 'disabled';
    label: string;
    model?: string;
    description: string;
  };
  data_protection: {
    level: 'strict' | 'standard' | 'enhanced';
    label: string;
    description: string;
  };
  upload: {
    max_upload_bytes: number;
    max_upload_mb: number;
    allowed_extensions: string[];
  };
}

export interface ReviewState {
  job_id: string;
  decisions: Record<string, ReviewRow['review_status']>;
  items: ReviewItem[];
  total: number;
  truncated: boolean;
  offset: number;
  limit: number;
  has_more: boolean;
  pending: number;
  updated_at: string | null;
}

export interface ClarificationState {
  job_id: string;
  goal: string;
  status: JobStatus | string;
  questions: ClarificationQuestion[];
  clarification_round: number;
  max_clarification_rounds: number;
}

export interface PlanConfirmationState {
  job_id: string;
  goal: string;
  status: JobStatus | string;
  plan_id: string;
  plan_hash: string;
  goal_understanding: GoalUnderstanding;
  execution_plan: ExecutionPlan;
  output_spec: OutputSpec;
  planner_metadata: PlannerMetadata;
}

export interface ConversationTurn {
  turn_id: string;
  role: 'user' | 'assistant' | 'system';
  kind: string;
  content: string;
  structured_payload: Record<string, unknown>;
  created_at: string;
}

export interface TaskSession {
  session_id: string;
  job_id: string;
  status: string;
  original_instruction: string;
  current_task_spec_version: number;
  current_plan_version: number;
  current_plan_hash: string;
  clarification_round: number;
  max_clarification_rounds: number;
  task_spec: TaskSpec | Record<string, never>;
  turns: ConversationTurn[];
  created_at: string;
  updated_at: string;
}
