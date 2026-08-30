interface ErrorStateProps {
  message: string;
}

export function ErrorState({ message }: ErrorStateProps) {
  return (
    <div className="error-state" role="alert">
      {message}
    </div>
  );
}
