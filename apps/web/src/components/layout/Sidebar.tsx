import { useEffect, useMemo, useState } from 'react';
import { NavLink, useLocation, useNavigate } from 'react-router-dom';

import { jobStatusLabel } from '../../api/labels';
import type { JobSummary } from '../../api/types';
import { AppIcon } from '../common/AppIcon';
import { TaskNavigation } from '../job/TaskNavigation';
import { useJobStatus } from '../../hooks/useJobStatus';
import { clearRecentJob, saveRecentJob, useRecentJob } from '../../hooks/useRecentJob';
import { useRecentJobs } from '../../hooks/useRecentJobs';

export function Sidebar() {
  const location = useLocation();
  const navigate = useNavigate();
  const cachedJob = useRecentJob();
  const { jobs, isLoading: jobsLoading } = useRecentJobs();
  const routeJobId = location.pathname.match(/^\/jobs\/([a-f0-9]{32})(?:\/|$)/)?.[1];
  const { job: routeJob, isMissing } = useJobStatus(routeJobId);
  const [switcherOpen, setSwitcherOpen] = useState(false);

  const activeJob = useMemo(
    () =>
      routeJob ??
      jobs.find((job) => job.job_id === routeJobId) ??
      jobs.find((job) => job.job_id === cachedJob?.job_id) ??
      jobs[0] ??
      null,
    [cachedJob?.job_id, jobs, routeJob, routeJobId]
  );

  useEffect(() => {
    if (routeJob) {
      saveRecentJob(toRecentJob(routeJob));
    }
  }, [routeJob]);

  useEffect(() => {
    if (isMissing && routeJobId) {
      clearRecentJob(routeJobId);
    }
  }, [isMissing, routeJobId]);

  function selectJob(job: JobSummary) {
    saveRecentJob(toRecentJob(job));
    setSwitcherOpen(false);
    navigate(`/jobs/${job.job_id}`);
  }

  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-mark">炼</span>
        <div>
          <strong>数据炼金师</strong>
          <small>数据处理与结果交付</small>
        </div>
      </div>

      <NavLink className="new-task-button" to="/upload">
        <AppIcon name="add" />
        <span>新建任务</span>
      </NavLink>

      <section className="sidebar-section">
        <div className="sidebar-section-label">
          <span>当前任务</span>
          {activeJob ? (
            <button
              aria-expanded={switcherOpen}
              aria-label="切换任务"
              className="icon-button"
              onClick={() => setSwitcherOpen((current) => !current)}
              type="button"
            >
              <AppIcon name="chevron" />
            </button>
          ) : null}
        </div>

        {activeJob ? (
          <>
            <button
              className="active-job-card"
              onClick={() => setSwitcherOpen((current) => !current)}
              type="button"
            >
              <span className={`job-state-dot status-${activeJob.status}`} />
              <span>
                <strong>{activeJob.goal || '未命名任务'}</strong>
                <small>
                  {jobStatusLabel(activeJob.status)} · {formatTaskTime(activeJob.created_at)}
                </small>
              </span>
              <AppIcon name="chevron" />
            </button>

            {switcherOpen ? (
              <div className="job-switcher">
                {jobs.map((job) => (
                  <button
                    className={job.job_id === activeJob.job_id ? 'selected' : ''}
                    key={job.job_id}
                    onClick={() => selectJob(job)}
                    type="button"
                  >
                    <span className={`job-state-dot status-${job.status}`} />
                    <span>
                      <strong>{job.goal || '未命名任务'}</strong>
                      <small>{jobStatusLabel(job.status)} · {formatTaskTime(job.created_at)}</small>
                    </span>
                  </button>
                ))}
              </div>
            ) : null}

            <TaskNavigation job={activeJob} />

            {activeJob.status === 'needs_clarification' ? (
              <NavLink
                className="sidebar-action"
                to={`/jobs/${activeJob.job_id}/clarification`}
              >
                <AppIcon name="warning" />
                <span>需要回答一个问题</span>
                <AppIcon name="arrow" />
              </NavLink>
            ) : null}
            {activeJob.status === 'awaiting_confirmation' ? (
              <NavLink
                className="sidebar-action"
                to={`/jobs/${activeJob.job_id}/plan-confirmation`}
              >
                <AppIcon name="warning" />
                <span>处理方案待确认</span>
                <AppIcon name="arrow" />
              </NavLink>
            ) : null}
          </>
        ) : (
          <div className="sidebar-empty">
            <AppIcon name="spark" />
            <strong>{jobsLoading ? '正在读取任务' : '还没有任务'}</strong>
            <p>上传数据并说明目标，任务进度和结果会显示在这里。</p>
          </div>
        )}
      </section>

      <nav className="sidebar-footer-nav" aria-label="工作台导航">
        <NavLink className={({ isActive }) => `system-nav-link${isActive ? ' active' : ''}`} to="/settings">
          <AppIcon name="settings" />
          <span>使用说明</span>
        </NavLink>
      </nav>
    </aside>
  );
}

function toRecentJob(job: JobSummary) {
  return {
    job_id: job.job_id,
    goal: job.goal ?? '',
    mode: job.mode,
    created_at: job.created_at ?? new Date().toISOString(),
    review_count: job.review_count,
    pending_review_count: job.pending_review_count
  };
}

function formatTaskTime(value?: string) {
  if (!value) return '最近任务';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '最近任务';
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit'
  }).format(date);
}
