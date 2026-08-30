import { useEffect, useState } from 'react';

import { getJobEvents } from '../api/jobs';
import type { ExecutionEvent } from '../api/types';

/**
 * 执行记录是次要信息：拿不到就当没有，绝不能因此让结果页报错或者转圈。
 * 它的作用只是给已经交付的结果补一句解释。
 */
export function useJobEvents(jobId?: string, refreshKey?: string) {
  const [events, setEvents] = useState<ExecutionEvent[]>([]);

  useEffect(() => {
    if (!jobId) {
      setEvents([]);
      return undefined;
    }
    let cancelled = false;
    getJobEvents(jobId)
      .then((payload) => {
        if (!cancelled) setEvents(payload.execution_events ?? []);
      })
      .catch(() => {
        if (!cancelled) setEvents([]);
      });
    return () => {
      cancelled = true;
    };
  }, [jobId, refreshKey]);

  return events;
}
