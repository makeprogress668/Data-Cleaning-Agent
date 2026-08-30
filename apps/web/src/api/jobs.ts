import { apiGet, apiPostForm, apiPostJson } from './client';
import type {
  BusinessAnswer,
  ClarificationQuestion,
  ClarificationState,
  DiscoveryView,
  JobEvents,
  JobMode,
  JobStatus,
  JobSummary,
  PlanConfirmationState,
  PlanView,
  ReviewApplied,
  ReviewRow,
  ReviewState,
  RecipeSummary,
  ResultVersionState,
  RuntimeConfig,
  SemanticModelSummary
} from './types';

export interface CreateJobInput {
  files: File[];
  goal: string;
  mode: JobMode;
  recipeId?: string;
  semanticModelId?: string;
}

export interface CreateJobResponse {
  job_id: string;
  status: JobStatus | string;
  files?: {
    download_url?: string;
  };
}

interface JobApiPayload {
  job_id?: string;
  status?: string;
  mode?: JobMode;
  goal?: string;
  created_at?: string;
  updated_at?: string;
  error_message?: string;
  error?: string;
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
    total?: number;
    valid?: number;
    abnormal?: number;
  };
  quality_score?: number | null;
  result_row_count?: number;
  input_files?: string[];
  has_discovery?: boolean;
  has_plan?: boolean;
  has_result?: boolean;
}

export async function createCleaningJob(input: CreateJobInput): Promise<CreateJobResponse> {
  const formData = new FormData();
  input.files.forEach((file) => formData.append('files', file));
  formData.append('goal', input.goal);
  formData.append('mode', input.mode);
  if (input.recipeId) formData.append('recipe_id', input.recipeId);
  if (input.semanticModelId) formData.append('semantic_model_id', input.semanticModelId);
  formData.append(
    'config',
    JSON.stringify({
      result_limit: 50,
      include_diagnostics: true,
      output_mode: input.mode === 'answer' ? 'business_answer' : 'cleaning_result'
    })
  );
  return apiPostForm<CreateJobResponse>('/api/v1/jobs', formData);
}

function normalizeJob(payload: JobApiPayload, fallbackJobId: string): JobSummary {
  return {
    job_id: payload.job_id ?? fallbackJobId,
    status: normalizeStatus(payload.status),
    mode: payload.mode ?? 'answer',
    goal: payload.goal,
    created_at: payload.created_at,
    updated_at: payload.updated_at,
    error_message: payload.error_message ?? payload.error,
    failure_stage: payload.failure_stage,
    clarification_questions: payload.clarification_questions ?? [],
    session_id: payload.session_id,
    clarification_round: payload.clarification_round,
    max_clarification_rounds: payload.max_clarification_rounds,
    task_spec_version: payload.task_spec_version,
    plan_version: payload.plan_version,
    review_count: payload.review_count,
    pending_review_count: payload.pending_review_count,
    counts: payload.counts
      ? {
          total: payload.counts.total ?? 0,
          valid: payload.counts.valid ?? 0,
          abnormal: payload.counts.abnormal ?? 0
        }
      : undefined,
    quality_score: payload.quality_score,
    result_row_count: payload.result_row_count,
    input_files: payload.input_files ?? [],
    has_discovery: payload.has_discovery,
    has_plan: payload.has_plan,
    has_result: payload.has_result
  };
}

function normalizeStatus(status: string | undefined): JobStatus {
  if (status === 'completed' || status === 'success') {
    return 'succeeded';
  }
  if (
    status === 'pending' ||
    status === 'running' ||
    status === 'needs_clarification' ||
    status === 'awaiting_confirmation' ||
    status === 'succeeded' ||
    status === 'failed' ||
    status === 'cancelled'
  ) {
    return status;
  }
  return 'failed';
}

export async function getJobStatus(jobId: string): Promise<JobSummary> {
  const payload = await apiGet<JobApiPayload>(`/api/v1/jobs/${jobId}/status`);
  return normalizeJob(payload, jobId);
}

export async function getRecentJobs(limit = 20): Promise<JobSummary[]> {
  const payload = await apiGet<{ jobs: JobApiPayload[] }>(
    `/api/v1/jobs?limit=${limit}`
  );
  return payload.jobs.map((job) => normalizeJob(job, job.job_id ?? ''));
}

export function getBusinessAnswer(jobId: string): Promise<BusinessAnswer> {
  return apiGet<BusinessAnswer>(`/api/v1/jobs/${jobId}/business-answer`);
}

export function getJobDiscovery(jobId: string): Promise<DiscoveryView> {
  return apiGet<DiscoveryView>(`/api/v1/jobs/${jobId}/discovery`);
}

export function getJobEvents(jobId: string): Promise<JobEvents> {
  return apiGet<JobEvents>(`/api/v1/jobs/${jobId}/events`);
}

export function getJobPlan(jobId: string): Promise<PlanView> {
  return apiGet<PlanView>(`/api/v1/jobs/${jobId}/plan`);
}

export function getRuntimeConfig(): Promise<RuntimeConfig> {
  return apiGet<RuntimeConfig>('/api/v1/config');
}

export function getReviewState(
  jobId: string,
  offset = 0,
  limit = 200
): Promise<ReviewState> {
  return apiGet<ReviewState>(
    `/api/v1/jobs/${jobId}/review?offset=${offset}&limit=${limit}`
  );
}

export function submitReviewDecisions(
  jobId: string,
  decisions: Array<{ row_id: string; status: ReviewRow['review_status'] }>
): Promise<ReviewState> {
  return apiPostJson<ReviewState>(`/api/v1/jobs/${jobId}/review`, { decisions });
}

/**
 * Apply the saved review decisions and produce a second, review-aware workbook.
 * Server-side this is a pure projection over the run's persisted result snapshot,
 * so it never re-plans or re-executes the job.
 */
export function rematerializeReviewedResult(
  jobId: string
): Promise<ReviewApplied & { job_id: string; url: string }> {
  return apiPostJson<ReviewApplied & { job_id: string; url: string }>(
    `/api/v1/jobs/${jobId}/rematerialize`,
    {}
  );
}

export function getClarification(jobId: string): Promise<ClarificationState> {
  return apiGet<ClarificationState>(`/api/v1/jobs/${jobId}/clarification`);
}

export function submitClarificationAnswers(
  jobId: string,
  answers: Array<{ question_id: string; answer: string }>
): Promise<{ job_id: string; status: string }> {
  return apiPostJson<{ job_id: string; status: string }>(
    `/api/v1/jobs/${jobId}/clarification`,
    { answers }
  );
}

export function getPlanConfirmation(jobId: string): Promise<PlanConfirmationState> {
  return apiGet<PlanConfirmationState>(`/api/v1/jobs/${jobId}/plan-confirmation`);
}

export function confirmPlan(
  jobId: string,
  planId: string,
  planHash: string
): Promise<{ job_id: string; status: string }> {
  return apiPostJson<{ job_id: string; status: string }>(
    `/api/v1/jobs/${jobId}/plan-confirmation`,
    { plan_id: planId, plan_hash: planHash }
  );
}

export async function getRecipes(): Promise<RecipeSummary[]> {
  const payload = await apiGet<{ recipes: RecipeSummary[] }>('/api/v1/recipes');
  return payload.recipes;
}

export function saveRecipe(
  sourceJobId: string,
  name: string,
  description = ''
): Promise<RecipeSummary> {
  return apiPostJson<RecipeSummary>('/api/v1/recipes', {
    source_job_id: sourceJobId,
    name,
    description
  });
}

export async function getSemanticModels(): Promise<SemanticModelSummary[]> {
  const payload = await apiGet<{ semantic_models: SemanticModelSummary[] }>(
    '/api/v1/semantic-models'
  );
  return payload.semantic_models;
}

export function getResultVersions(jobId: string): Promise<ResultVersionState> {
  return apiGet<ResultVersionState>(`/api/v1/jobs/${jobId}/versions`);
}

export function selectResultVersion(
  jobId: string,
  versionId: string
): Promise<{ job_id: string; selected_version_id: string }> {
  return apiPostJson(`/api/v1/jobs/${jobId}/versions/${versionId}/select`, {});
}

export function undoResultVersion(
  jobId: string
): Promise<{ job_id: string; selected_version_id: string }> {
  return apiPostJson(`/api/v1/jobs/${jobId}/versions/undo`, {});
}

export function branchResultVersion(
  jobId: string,
  versionId: string
): Promise<ReviewApplied & { version_id: string; parent_version_id: string; url: string }> {
  return apiPostJson(`/api/v1/jobs/${jobId}/versions/${versionId}/branch`, {
    decisions: []
  });
}
