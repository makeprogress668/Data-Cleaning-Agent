import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError } from '../api/client';
import { getReviewState, submitReviewDecisions } from '../api/jobs';
import type { ReviewItem, ReviewRow } from '../api/types';
import { saveRecentJob, useRecentJob } from './useRecentJob';

type ReviewStatus = ReviewRow['review_status'];

/**
 * Loads persisted review decisions for a job and exposes a persist() that writes
 * a decision back to the API. When no jobId is given (demo mode) it keeps state
 * only in memory so the demo page stays interactive without a backend.
 */
export function useReview(jobId?: string, refreshKey?: string) {
  const recentJob = useRecentJob();
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [decisions, setDecisions] = useState<Record<string, ReviewStatus>>({});
  const [isLoading, setIsLoading] = useState(Boolean(jobId));
  const [error, setError] = useState<string | null>(null);
  const [savingCount, setSavingCount] = useState(0);
  const [notReady, setNotReady] = useState(false);
  const [total, setTotal] = useState(0);
  const [truncated, setTruncated] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [loadedJobId, setLoadedJobId] = useState<string | null>(null);
  const decisionsRef = useRef<Record<string, ReviewStatus>>({});
  const rowQueues = useRef<Map<string, Promise<void>>>(new Map());

  useEffect(() => {
    if (!jobId) {
      setItems([]);
      setDecisions({});
      decisionsRef.current = {};
      rowQueues.current.clear();
      setLoadedJobId(null);
      setIsLoading(false);
      setNotReady(false);
      setTotal(0);
      setTruncated(false);
      setHasMore(false);
      setIsLoadingMore(false);
      return undefined;
    }

    const currentJobId = jobId;
    let cancelled = false;
    setItems([]);
    setDecisions({});
    decisionsRef.current = {};
    rowQueues.current.clear();
    setLoadedJobId(null);
    setError(null);
    setNotReady(false);
    setTotal(0);
    setTruncated(false);
    setHasMore(false);
    setIsLoadingMore(false);
    setIsLoading(true);

    async function load() {
      setIsLoading(true);
      try {
        const state = await getReviewState(currentJobId);
        if (!cancelled) {
          setItems(state.items ?? []);
          const nextDecisions = state.decisions ?? {};
          decisionsRef.current = nextDecisions;
          setDecisions(nextDecisions);
          setLoadedJobId(currentJobId);
          setError(null);
          setNotReady(false);
          setTotal(state.total ?? state.items?.length ?? 0);
          setTruncated(Boolean(state.truncated));
          setHasMore(Boolean(state.has_more));
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
          setError(requestError instanceof Error ? requestError.message : '复核状态加载失败');
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

  const loadMore = useCallback(async () => {
    if (!jobId || !hasMore || isLoadingMore) return;
    setIsLoadingMore(true);
    try {
      const state = await getReviewState(jobId, items.length);
      setItems((current) => {
        const existing = new Set(current.map((item) => item.id));
        return [
          ...current,
          ...(state.items ?? []).filter((item) => !existing.has(item.id))
        ];
      });
      const nextDecisions = { ...decisionsRef.current, ...(state.decisions ?? {}) };
      decisionsRef.current = nextDecisions;
      setDecisions(nextDecisions);
      setHasMore(Boolean(state.has_more));
      setTruncated(Boolean(state.truncated));
      setError(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '更多复核记录加载失败');
    } finally {
      setIsLoadingMore(false);
    }
  }, [hasMore, isLoadingMore, items.length, jobId]);

  const persist = useCallback(
    async (rowId: string, status: ReviewStatus) => {
      const previousStatus = decisionsRef.current[rowId];
      decisionsRef.current = { ...decisionsRef.current, [rowId]: status };
      setDecisions(decisionsRef.current);
      if (!jobId) {
        return;
      }
      const previousRequest = rowQueues.current.get(rowId) ?? Promise.resolve();
      const request = previousRequest.catch(() => undefined).then(async () => {
        setSavingCount((current) => current + 1);
        try {
          const state = await submitReviewDecisions(jobId, [{ row_id: rowId, status }]);
          if (decisionsRef.current[rowId] === status) {
            decisionsRef.current = {
              ...decisionsRef.current,
              [rowId]: state.decisions?.[rowId] ?? status
            };
            setDecisions(decisionsRef.current);
          }
          setTotal(state.total ?? state.items?.length ?? 0);
          if (recentJob?.job_id === jobId) {
            saveRecentJob({
              ...recentJob,
              review_count: state.total ?? state.items?.length ?? 0,
              pending_review_count: state.pending ?? 0
            });
          }
          window.dispatchEvent(
            new CustomEvent('job-status-changed', { detail: { jobId } })
          );
          setError(null);
        } catch (requestError) {
          if (decisionsRef.current[rowId] === status) {
            const restored = { ...decisionsRef.current };
            if (previousStatus) {
              restored[rowId] = previousStatus;
            } else {
              delete restored[rowId];
            }
            decisionsRef.current = restored;
            setDecisions(restored);
          }
          setError(requestError instanceof Error ? requestError.message : '复核结果保存失败');
        } finally {
          setSavingCount((current) => Math.max(0, current - 1));
        }
      });
      rowQueues.current.set(rowId, request);
      await request;
      if (rowQueues.current.get(rowId) === request) {
        rowQueues.current.delete(rowId);
      }
    },
    [jobId, recentJob]
  );

  const matchesRoute = loadedJobId === jobId;
  return {
    items: matchesRoute ? items : [],
    total: matchesRoute ? total : 0,
    truncated: matchesRoute ? truncated : false,
    hasMore: matchesRoute ? hasMore : false,
    decisions: matchesRoute ? decisions : {},
    persist,
    loadMore,
    isLoading: Boolean(jobId) && !matchesRoute ? true : isLoading,
    isLoadingMore,
    isSaving: savingCount > 0,
    error,
    notReady
  };
}
