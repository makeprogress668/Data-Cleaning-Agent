import { NavLink } from 'react-router-dom';

import type { JobSummary } from '../../api/types';
import { AppIcon } from '../common/AppIcon';

interface TaskNavigationProps {
  job: JobSummary;
}

export function TaskNavigation({ job }: TaskNavigationProps) {
  const items = [
    {
      label: '任务总览',
      to: `/jobs/${job.job_id}`,
      icon: 'home' as const,
      end: true,
      enabled: true
    },
    {
      label: '数据概览',
      to: `/jobs/${job.job_id}/discovery`,
      icon: 'data' as const,
      end: false,
      enabled: Boolean(job.has_discovery)
    },
    {
      label: '处理方案',
      to: `/jobs/${job.job_id}/plan`,
      icon: 'plan' as const,
      end: false,
      enabled: Boolean(job.has_plan)
    },
    {
      label: '处理结果',
      to: `/jobs/${job.job_id}/result`,
      icon: 'result' as const,
      end: false,
      enabled: job.mode === 'answer' && job.status === 'succeeded' && Boolean(job.has_result)
    },
    {
      label: '异常复核',
      to: `/jobs/${job.job_id}/review`,
      icon: 'review' as const,
      end: false,
      enabled: job.mode === 'answer' && job.status === 'succeeded',
      badge: job.pending_review_count
    }
  ];

  return (
    <nav aria-label="当前任务导航" className="task-nav">
      {items.map((item) =>
        item.enabled ? (
          <NavLink
            className={({ isActive }) => `task-nav-link${isActive ? ' active' : ''}`}
            end={item.end}
            key={item.label}
            to={item.to}
          >
            <AppIcon name={item.icon} />
            <span>{item.label}</span>
            {item.badge ? <small>{item.badge}</small> : null}
          </NavLink>
        ) : (
          <span className="task-nav-link disabled" key={item.label}>
            <AppIcon name={item.icon} />
            <span>{item.label}</span>
          </span>
        )
      )}
    </nav>
  );
}
