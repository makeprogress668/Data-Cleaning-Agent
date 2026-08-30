import { useEffect, useState } from 'react';

import type { JobMode } from '../api/types';

const RECENT_JOB_KEY = 'data-cleaning-agent.recent-job';

export interface RecentJob {
  job_id: string;
  goal: string;
  mode: JobMode;
  created_at: string;
  review_count?: number;
  pending_review_count?: number;
}

export function readRecentJob(): RecentJob | null {
  try {
    const raw = window.localStorage.getItem(RECENT_JOB_KEY);
    return raw ? (JSON.parse(raw) as RecentJob) : null;
  } catch {
    return null;
  }
}

export function saveRecentJob(job: RecentJob) {
  window.localStorage.setItem(RECENT_JOB_KEY, JSON.stringify(job));
  window.dispatchEvent(new CustomEvent('recent-job-changed', { detail: job }));
}

export function clearRecentJob(jobId?: string) {
  const current = readRecentJob();
  if (jobId && current?.job_id !== jobId) {
    return;
  }
  window.localStorage.removeItem(RECENT_JOB_KEY);
  window.dispatchEvent(new CustomEvent('recent-job-changed', { detail: null }));
}

export function useRecentJob() {
  const [recentJob, setRecentJob] = useState<RecentJob | null>(() => readRecentJob());

  useEffect(() => {
    function handleStorage(event: StorageEvent) {
      if (event.key === RECENT_JOB_KEY) {
        setRecentJob(readRecentJob());
      }
    }

    function handleLocalChange(event: Event) {
      const customEvent = event as CustomEvent<RecentJob | null>;
      setRecentJob(customEvent.detail ?? readRecentJob());
    }

    window.addEventListener('storage', handleStorage);
    window.addEventListener('recent-job-changed', handleLocalChange);
    return () => {
      window.removeEventListener('storage', handleStorage);
      window.removeEventListener('recent-job-changed', handleLocalChange);
    };
  }, []);

  return recentJob;
}
