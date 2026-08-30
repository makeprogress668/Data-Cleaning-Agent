import { useCallback, useEffect, useRef, useState } from 'react';

import { getRecentJobs } from '../api/jobs';
import type { JobSummary } from '../api/types';
import { clearRecentJob, readRecentJob, saveRecentJob } from './useRecentJob';

export function useRecentJobs(limit = 12) {
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const requestSequence = useRef(0);

  const refresh = useCallback(async () => {
    const requestId = ++requestSequence.current;
    setIsLoading(true);
    try {
      const payload = await getRecentJobs(limit);
      if (requestId !== requestSequence.current) return;
      const cachedJob = readRecentJob();
      setJobs(payload);
      setError(null);

      const cachedStillExists = cachedJob
        ? payload.some((job) => job.job_id === cachedJob.job_id)
        : false;
      if (cachedJob && !cachedStillExists) {
        clearRecentJob(cachedJob.job_id);
      }

      const preferred =
        payload.find((job) => job.job_id === cachedJob?.job_id) ?? payload[0];
      if (preferred && (!cachedJob || preferred.job_id !== cachedJob.job_id)) {
        saveRecentJob({
          job_id: preferred.job_id,
          goal: preferred.goal ?? '',
          mode: preferred.mode,
          created_at: preferred.created_at ?? new Date().toISOString(),
          review_count: preferred.review_count,
          pending_review_count: preferred.pending_review_count
        });
      }
    } catch (requestError) {
      if (requestId !== requestSequence.current) return;
      setError(requestError instanceof Error ? requestError.message : '任务列表加载失败');
    } finally {
      if (requestId === requestSequence.current) {
        setIsLoading(false);
      }
    }
  }, [limit]);

  useEffect(() => {
    void refresh();
    return () => {
      requestSequence.current += 1;
    };
  }, [refresh]);

  useEffect(() => {
    function handleChange() {
      void refresh();
    }
    window.addEventListener('job-list-changed', handleChange);
    window.addEventListener('job-status-changed', handleChange);
    return () => {
      window.removeEventListener('job-list-changed', handleChange);
      window.removeEventListener('job-status-changed', handleChange);
    };
  }, [refresh]);

  return { jobs, isLoading, error, refresh };
}
