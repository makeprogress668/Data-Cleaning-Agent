import { useState } from 'react';

import { createCleaningJob, type CreateJobInput, type CreateJobResponse } from '../api/jobs';

export function useCreateJob() {
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function createJob(input: CreateJobInput): Promise<CreateJobResponse> {
    setIsLoading(true);
    setError(null);
    try {
      return await createCleaningJob(input);
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : '任务提交失败';
      setError(message);
      throw requestError;
    } finally {
      setIsLoading(false);
    }
  }

  return { createJob, isLoading, error };
}
