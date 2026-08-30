import { useEffect, useState } from 'react';

import { ApiError } from '../api/client';
import { getJobDiscovery } from '../api/jobs';
import type { DiscoveryView } from '../api/types';

export function useDiscovery(jobId?: string, refreshKey?: string) {
  const [discovery, setDiscovery] = useState<DiscoveryView | null>(null);
  const [isLoading, setIsLoading] = useState(Boolean(jobId));
  const [error, setError] = useState<string | null>(null);
  const [notReady, setNotReady] = useState(false);
  const [loadedJobId, setLoadedJobId] = useState<string | null>(null);

  useEffect(() => {
    if (!jobId) {
      setDiscovery(null);
      setLoadedJobId(null);
      setIsLoading(false);
      setNotReady(false);
      return undefined;
    }

    const currentJobId = jobId;
    let cancelled = false;
    setDiscovery(null);
    setLoadedJobId(null);
    setError(null);
    setNotReady(false);
    setIsLoading(true);

    async function load() {
      setIsLoading(true);
      try {
        const payload = await getJobDiscovery(currentJobId);
        if (!cancelled) {
          setDiscovery(payload);
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
          setError(requestError instanceof Error ? requestError.message : '数据发现结果加载失败');
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, [jobId, refreshKey]);

  const matchesRoute = loadedJobId === jobId;
  return {
    discovery: matchesRoute ? discovery : null,
    isLoading: Boolean(jobId) && !matchesRoute ? true : isLoading,
    error,
    notReady: matchesRoute ? notReady : false
  };
}
