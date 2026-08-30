interface ProcessingStateProps {
  label?: string;
  hint?: string;
}

/**
 * Shown while a job is still running and a downstream artifact (result / discovery
 * / plan) has not been generated yet. Distinct from ErrorState so a 404-not-ready
 * never looks like a failure to the user.
 */
export function ProcessingState({
  label = '正在处理',
  hint = '结果生成后本页会自动刷新，请稍候。'
}: ProcessingStateProps) {
  return (
    <div className="processing-state" role="status">
      <span className="spinner" />
      <div>
        <strong>{label}</strong>
        <p>{hint}</p>
      </div>
    </div>
  );
}
