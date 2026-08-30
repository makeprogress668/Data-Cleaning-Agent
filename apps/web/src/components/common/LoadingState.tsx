interface LoadingStateProps {
  label?: string;
}

export function LoadingState({ label = '正在加载' }: LoadingStateProps) {
  return (
    <div className="loading-state" role="status">
      <span className="spinner" />
      {label}
    </div>
  );
}
