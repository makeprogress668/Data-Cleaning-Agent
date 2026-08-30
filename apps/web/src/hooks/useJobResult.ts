import { useEffect, useState } from 'react';

import { ApiError } from '../api/client';
import { getBusinessAnswer } from '../api/jobs';
import type { BusinessAnswer } from '../api/types';

interface UseJobResultOptions {
  /** Re-fetch whenever this key changes (e.g. job status flips to succeeded). */
  refreshKey?: string;
}

export function useJobResult(jobId?: string, options: UseJobResultOptions = {}) {
  const { refreshKey } = options;
  const [result, setResult] = useState<BusinessAnswer | null>(null);
  const [isLoading, setIsLoading] = useState(Boolean(jobId));
  const [error, setError] = useState<string | null>(null);
  // 404 while the job is still running just means the result isn't ready yet.
  const [notReady, setNotReady] = useState(false);
  const [loadedJobId, setLoadedJobId] = useState<string | null>(null);

  useEffect(() => {
    if (!jobId) {
      setResult(null);
      setLoadedJobId(null);
      setIsLoading(false);
      setNotReady(false);
      return undefined;
    }

    const currentJobId = jobId;
    let cancelled = false;
    setResult(null);
    setLoadedJobId(null);
    setError(null);
    setNotReady(false);
    setIsLoading(true);

    async function loadResult() {
      setIsLoading(true);
      try {
        const payload = await getBusinessAnswer(currentJobId);
        if (!cancelled) {
          setResult(payload);
          setLoadedJobId(currentJobId);
          setError(null);
          setNotReady(false);
        }
      } catch (requestError) {
        if (cancelled) {
          return;
        }
        setLoadedJobId(currentJobId);
        if (requestError instanceof ApiError && requestError.status === 404) {
          setNotReady(true);
          setError(null);
        } else {
          setError(requestError instanceof Error ? requestError.message : '业务结果加载失败');
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    }

    void loadResult();

    return () => {
      cancelled = true;
    };
  }, [jobId, refreshKey]);

  const matchesRoute = loadedJobId === jobId;
  return {
    result: matchesRoute ? result : null,
    isLoading: Boolean(jobId) && !matchesRoute ? true : isLoading,
    error,
    notReady: matchesRoute ? notReady : false
  };
}
