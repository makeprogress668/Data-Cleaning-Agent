import { Link } from 'react-router-dom';
import { AppIcon } from './AppIcon';

interface EmptyStateProps {
  title: string;
  description?: string;
  actionLabel?: string;
  actionTo?: string;
}

export function EmptyState({ title, description, actionLabel, actionTo }: EmptyStateProps) {
  return (
    <div className="empty-state">
      <div className="empty-state-icon" aria-hidden="true">
        <AppIcon name="file" />
      </div>
      <strong>{title}</strong>
      {description ? <p>{description}</p> : null}
      {actionLabel && actionTo ? (
        <Link className="primary-button" to={actionTo}>
          {actionLabel}
        </Link>
      ) : null}
    </div>
  );
}
