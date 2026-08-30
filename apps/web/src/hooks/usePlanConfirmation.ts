import { useCallback, useEffect, useState } from 'react';

import { confirmPlan, getPlanConfirmation } from '../api/jobs';
import type { ExecutionPlan, GoalUnderstanding, OutputSpec } from '../api/types';

/**
 * Loads the pre-execution goal understanding a paused (awaiting_confirmation) job is
 * waiting on and exposes a confirm() that approves the plan and resumes execution.
 * Kept close to the useClarification shape so the page reads consistently.
 */
export function usePlanConfirmation(jobId?: string) {
  const [understanding, setUnderstanding] = useState<GoalUnderstanding | null>(null);
  const [goal, setGoal] = useState('');
  const [planId, setPlanId] = useState('');
  const [planHash, setPlanHash] = useState('');
  const [executionPlan, setExecutionPlan] = useState<ExecutionPlan | null>(null);
  const [outputSpec, setOutputSpec] = useState<OutputSpec | null>(null);
  const [isLoading, setIsLoading] = useState(Boolean(jobId));
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadedJobId, setLoadedJobId] = useState<string | null>(null);

  useEffect(() => {
    if (!jobId) {
      setUnderstanding(null);
      setGoal('');
      setPlanId('');
      setPlanHash('');
      setLoadedJobId(null);
      setExecutionPlan(null);
      setOutputSpec(null);
      setIsLoading(false);
      return undefined;
    }

    const currentJobId = jobId;
    let cancelled = false;
    setUnderstanding(null);
    setGoal('');
    setPlanId('');
    setPlanHash('');
    setExecutionPlan(null);
    setOutputSpec(null);
    setLoadedJobId(null);
    setError(null);
    setIsLoading(true);

    async function load() {
      setIsLoading(true);
      try {
        const state = await getPlanConfirmation(currentJobId);
        if (!cancelled) {
          setUnderstanding(state.goal_understanding ?? null);
          setLoadedJobId(currentJobId);
          setGoal(state.goal ?? '');
          setPlanId(state.plan_id ?? '');
          setPlanHash(state.plan_hash ?? '');
          setExecutionPlan(state.execution_plan ?? null);
          setOutputSpec(state.output_spec ?? null);
          setError(null);
        }
      } catch (requestError) {
        if (!cancelled) {
          setLoadedJobId(currentJobId);
          setError(requestError instanceof Error ? requestError.message : '计划信息加载失败');
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

  const confirm = useCallback(async () => {
    if (!jobId || !planId || !planHash) {
      setError('处理计划尚未完整加载，请刷新后重试');
      return false;
    }
    setIsSubmitting(true);
    try {
      await confirmPlan(jobId, planId, planHash);
      setError(null);
      return true;
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '确认执行失败');
      return false;
    } finally {
      setIsSubmitting(false);
    }
  }, [jobId, planHash, planId]);

  const matchesRoute = loadedJobId === jobId;
  return {
    understanding: matchesRoute ? understanding : null,
    goal: matchesRoute ? goal : '',
    executionPlan: matchesRoute ? executionPlan : null,
    outputSpec: matchesRoute ? outputSpec : null,
    confirm,
    isLoading: Boolean(jobId) && !matchesRoute ? true : isLoading,
    isSubmitting,
    error
  };
}
