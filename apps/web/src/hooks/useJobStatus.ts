import { useEffect, useState } from 'react';

import { ApiError } from '../api/client';
import { getJobStatus } from '../api/jobs';
import { isTerminalStatus, JOB_POLL_INTERVAL_MS } from '../api/config';
import type { JobSummary } from '../api/types';

export function useJobStatus(jobId?: string) {
  const [job, setJob] = useState<JobSummary | null>(null);
  const [isLoading, setIsLoading] = useState(Boolean(jobId));
  const [error, setError] = useState<string | null>(null);
  const [isMissing, setIsMissing] = useState(false);
  const [loadedJobId, setLoadedJobId] = useState<string | null>(null);

  useEffect(() => {
    if (!jobId) {
      setJob(null);
      setLoadedJobId(null);
      setIsLoading(false);
      setIsMissing(false);
      return undefined;
    }

    const currentJobId = jobId;
    let cancelled = false;
    let timer: number | undefined;
    setJob(null);
    setLoadedJobId(null);
    setError(null);
    setIsMissing(false);
    setIsLoading(true);

    async function loadJob() {
      let shouldContinue = true;
      try {
        const payload = await getJobStatus(currentJobId);
        if (cancelled) {
          return;
        }
        setJob(payload);
        setLoadedJobId(currentJobId);
        setError(null);
        setIsMissing(false);
        shouldContinue = !isTerminalStatus(payload.status);
      } catch (requestError) {
        if (!cancelled) {
          setLoadedJobId(currentJobId);
          if (requestError instanceof ApiError && requestError.status === 404) {
            setJob(null);
            setError(null);
            setIsMissing(true);
            shouldContinue = false;
          } else {
            setError(requestError instanceof Error ? requestError.message : '任务状态加载失败');
          }
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false);
          if (shouldContinue) {
            timer = window.setTimeout(() => {
              void loadJob();
            }, JOB_POLL_INTERVAL_MS);
          }
        }
      }
    }

    void loadJob();
    function handleStatusChange(event: Event) {
      const changedJobId = (event as CustomEvent<{ jobId?: string }>).detail?.jobId;
      if (!changedJobId || changedJobId === currentJobId) {
        void loadJob();
      }
    }
    window.addEventListener('job-status-changed', handleStatusChange);

    return () => {
      cancelled = true;
      window.removeEventListener('job-status-changed', handleStatusChange);
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
  }, [jobId]);

  const matchesRoute = loadedJobId === jobId;
  return {
    job: matchesRoute ? job : null,
    isLoading: Boolean(jobId) && !matchesRoute ? true : isLoading,
    error,
    isMissing: matchesRoute ? isMissing : false
  };
}
