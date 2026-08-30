import type { JobStatus } from './types';

/** Base URL for the backend API. Empty string means same-origin (dev proxy / prod). */
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '';

/** Interval for polling job status while a job is still running. */
export const JOB_POLL_INTERVAL_MS = 3000;

/** Default request timeout for GET/JSON calls (uploads are intentionally exempt). */
export const REQUEST_TIMEOUT_MS = 30000;

/** Job statuses that will never change again, so polling can stop. */
const TERMINAL_STATUSES: ReadonlySet<JobStatus> = new Set<JobStatus>([
  'succeeded',
  'failed',
  'cancelled'
]);

export function isTerminalStatus(status: JobStatus | undefined): boolean {
  return status !== undefined && TERMINAL_STATUSES.has(status);
}

/** Prefix a relative API path with the configured base URL. */
export function withApiBase(path: string): string {
  if (/^https?:\/\//i.test(path)) {
    return path;
  }
  return `${API_BASE_URL}${path}`;
}
