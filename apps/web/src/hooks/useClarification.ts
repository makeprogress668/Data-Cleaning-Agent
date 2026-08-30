import { useCallback, useEffect, useState } from 'react';

import { getClarification, submitClarificationAnswers } from '../api/jobs';
import type { ClarificationQuestion } from '../api/types';

/**
 * Loads the clarification questions a paused job is waiting on and exposes a
 * submit() that posts the user's answers and resumes execution. Kept close to the
 * useReview shape so the page reads like the exception-review page.
 */
export function useClarification(jobId?: string) {
  const [questions, setQuestions] = useState<ClarificationQuestion[]>([]);
  const [goal, setGoal] = useState('');
  const [round, setRound] = useState(0);
  const [maxRounds, setMaxRounds] = useState(3);
  const [isLoading, setIsLoading] = useState(Boolean(jobId));
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadedJobId, setLoadedJobId] = useState<string | null>(null);

  useEffect(() => {
    if (!jobId) {
      setQuestions([]);
      setGoal('');
      setLoadedJobId(null);
      setRound(0);
      setIsLoading(false);
      return undefined;
    }

    const currentJobId = jobId;
    let cancelled = false;
    setQuestions([]);
    setGoal('');
    setLoadedJobId(null);
    setError(null);
    setIsLoading(true);

    async function load() {
      setIsLoading(true);
      try {
        const state = await getClarification(currentJobId);
        if (!cancelled) {
          setQuestions(state.questions ?? []);
          setLoadedJobId(currentJobId);
          setGoal(state.goal ?? '');
          setRound(state.clarification_round ?? 0);
          setMaxRounds(state.max_clarification_rounds ?? 3);
          setError(null);
        }
      } catch (requestError) {
        if (!cancelled) {
          setLoadedJobId(currentJobId);
          setError(requestError instanceof Error ? requestError.message : '澄清问题加载失败');
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
  }, [jobId]);

  const submit = useCallback(
    async (answers: Array<{ question_id: string; answer: string }>) => {
      if (!jobId) {
        return false;
      }
      setIsSubmitting(true);
      try {
        await submitClarificationAnswers(jobId, answers);
        setError(null);
        return true;
      } catch (requestError) {
        setError(requestError instanceof Error ? requestError.message : '提交澄清答案失败');
        return false;
      } finally {
        setIsSubmitting(false);
      }
    },
    [jobId]
  );

  const matchesRoute = loadedJobId === jobId;
  return {
    questions: matchesRoute ? questions : [],
    goal: matchesRoute ? goal : '',
    round,
    maxRounds,
    submit,
    isLoading: Boolean(jobId) && !matchesRoute ? true : isLoading,
    isSubmitting,
    error
  };
}
