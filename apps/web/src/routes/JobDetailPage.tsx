import { Link, useParams } from 'react-router-dom';

import { jobStatusLabel } from '../api/labels';
import { AppIcon } from '../components/common/AppIcon';
import { EmptyState } from '../components/common/EmptyState';
import { ErrorState } from '../components/common/ErrorState';
import { LoadingState } from '../components/common/LoadingState';
import { JobTimeline } from '../components/job/JobTimeline';
import { OutputFiles } from '../components/report/OutputFiles';
import { useJobResult } from '../hooks/useJobResult';
import { useJobStatus } from '../hooks/useJobStatus';

const MODE_LABELS = {
  discover: '仅查看数据概览',
  plan: '仅生成处理方案',
  answer: '直接处理并交付结果'
};

export function JobDetailPage() {
  const { jobId } = useParams();
  const { job, isLoading, error, isMissing } = useJobStatus(jobId);
  const { result, notReady } = useJobResult(
    job?.mode === 'answer' ? jobId : undefined,
    { refreshKey: job?.status }
  );

  if (!jobId) {
    return <NoTask />;
  }

  if (isLoading && !job) {
    return (
      <div className="page-stack">
        <LoadingState label="正在读取任务" />
      </div>
    );
  }

  if (isMissing) {
    return (
      <div className="page-stack">
        <EmptyState
          title="这个任务已不存在"
          description="任务可能已被删除或超过保存期限。请选择其他任务，或重新上传数据开始处理。"
          actionLabel="新建任务"
          actionTo="/upload"
        />
      </div>
    );
  }

  if (!job) {
    return (
      <div className="page-stack">
        <ErrorState message={`任务加载失败：${error ?? '请稍后重试'}`} />
      </div>
    );
  }

  const isSucceeded = job.status === 'succeeded';
  const total = job.counts?.total ?? 0;
  const valid = job.counts?.valid ?? 0;
  const abnormal = job.counts?.abnormal ?? 0;
  const pendingReview = job.pending_review_count ?? abnormal;
  const excluded = Math.max(0, total - valid - abnormal);
  const completion = completionAction(job);

  return (
    <div className="page-stack">
      {error ? <ErrorState message={`状态刷新失败：${error}`} /> : null}

      <section className="task-overview-hero">
        <div className="task-overview-main">
          <div className="task-heading-row">
            <span className={`status-pill status-${job.status}`}>
              {jobStatusLabel(job.status)}
            </span>
          </div>
          <p className="eyebrow">本次处理目标</p>
          <h2>{job.goal || '未填写处理目标'}</h2>
          <div className="task-meta-row">
            <span>{MODE_LABELS[job.mode]}</span>
            {job.input_files?.length ? (
              <span>{job.input_files.length} 个输入文件</span>
            ) : null}
            {job.created_at ? <span>{formatTime(job.created_at)}</span> : null}
          </div>
        </div>
        {completion ? (
          <Link className="primary-button" to={completion.to}>
            {completion.label}
            <AppIcon name="arrow" />
          </Link>
        ) : null}
      </section>

      {job.status === 'needs_clarification' ? (
        <ActionRequired
          description="还有一个会影响处理结果的问题需要确认，回答后任务将继续。"
          label="回答问题"
          to={`/jobs/${job.job_id}/clarification`}
        />
      ) : null}
      {job.status === 'awaiting_confirmation' ? (
        <ActionRequired
          description={`第 ${job.plan_version ?? 1} 版处理方案已生成。请确认筛选、去重和输出内容后再执行。`}
          label="检查并确认方案"
          to={`/jobs/${job.job_id}/plan-confirmation`}
        />
      ) : null}
      {isSucceeded && (job.pending_review_count ?? 0) > 0 ? (
        <ActionRequired
          description={`还有 ${job.pending_review_count} 条记录需要业务判断；复核结论单独保存，下载文件保持当前版本。`}
          label="去复核"
          to={`/jobs/${job.job_id}/review`}
        />
      ) : null}

      {isSucceeded && job.mode === 'answer' ? (
        <section className="overview-metrics" aria-label="任务结果摘要">
          <OverviewMetric label="输入记录" value={total} />
          <OverviewMetric label="可直接使用" tone="success" value={valid} />
          <OverviewMetric
            label="待业务确认"
            tone={pendingReview ? 'warning' : 'neutral'}
            value={pendingReview}
          />
          <OverviewMetric label="按目标排除" value={excluded} />
          <OverviewMetric
            label="质量评分"
            tone={(job.quality_score ?? 0) >= 85 ? 'success' : 'warning'}
            value={job.quality_score == null ? '—' : `${Math.round(job.quality_score)}`}
            suffix={job.quality_score == null ? undefined : '/100'}
          />
        </section>
      ) : null}

      <div className="overview-layout">
        <JobTimeline
          status={job.status}
          mode={job.mode}
          errorMessage={job.error_message}
          failureStage={job.failure_stage}
        />
        <section className="card next-step-card">
          <div className="section-heading">
            <p className="eyebrow">当前建议</p>
            <h2>{nextStepTitle(job)}</h2>
            <p>{nextStepDescription(job)}</p>
          </div>
          <div className="quick-links">
            {job.has_discovery ? (
              <Link to={`/jobs/${job.job_id}/discovery`}>
                <AppIcon name="data" />
                <span>
                  <strong>数据概览</strong>
                  <small>查看输入表、字段和初步质量问题</small>
                </span>
                <AppIcon name="arrow" />
              </Link>
            ) : null}
            {job.has_plan ? (
              <Link to={`/jobs/${job.job_id}/plan`}>
                <AppIcon name="plan" />
                <span>
                  <strong>处理方案</strong>
                  <small>核对处理步骤、业务规则和影响范围</small>
                </span>
                <AppIcon name="arrow" />
              </Link>
            ) : null}
            {isSucceeded && job.mode === 'answer' ? (
              <Link to={`/jobs/${job.job_id}/result`}>
                <AppIcon name="result" />
                <span>
                  <strong>处理结果</strong>
                  <small>查看业务结论、数据预览和下载</small>
                </span>
                <AppIcon name="arrow" />
              </Link>
            ) : null}
          </div>
        </section>
      </div>

      {isSucceeded && job.mode === 'answer' && result?.output_files.length ? (
        <OutputFiles
          files={result.output_files}
          outputSpec={result.output_spec}
          reviewCount={result.review_item_count}
          pendingReviewCount={job.pending_review_count}
          resultRowCount={job.result_row_count}
        />
      ) : null}
      {isSucceeded && job.mode === 'answer' && notReady ? (
        <section className="card">
          <p>结果文件正在生成，请稍候。</p>
        </section>
      ) : null}
    </div>
  );
}

function NoTask() {
  return (
    <div className="page-stack">
      <EmptyState
        title="还没有选择任务"
        description="新建任务后，这里会持续展示处理进度、需要确认的事项和最终结果。"
        actionLabel="新建任务"
        actionTo="/upload"
      />
    </div>
  );
}

function ActionRequired({
  description,
  label,
  to
}: {
  description: string;
  label: string;
  to: string;
}) {
  return (
    <section className="attention-banner">
      <span className="attention-icon"><AppIcon name="warning" /></span>
      <div>
        <strong>需要你处理</strong>
        <p>{description}</p>
      </div>
      <Link className="attention-action" to={to}>
        {label}
        <AppIcon name="arrow" />
      </Link>
    </section>
  );
}

function OverviewMetric({
  label,
  value,
  suffix,
  tone = 'neutral'
}: {
  label: string;
  value: string | number;
  suffix?: string;
  tone?: 'neutral' | 'success' | 'warning';
}) {
  return (
    <article className={`overview-metric tone-${tone}`}>
      <span>{label}</span>
      <strong>{value}<small>{suffix}</small></strong>
    </article>
  );
}

function completionAction(job: NonNullable<ReturnType<typeof useJobStatus>['job']>) {
  if (job.status === 'needs_clarification') {
    return { label: '回答问题', to: `/jobs/${job.job_id}/clarification` };
  }
  if (job.status === 'awaiting_confirmation') {
    return { label: '确认方案', to: `/jobs/${job.job_id}/plan-confirmation` };
  }
  if (job.status !== 'succeeded') return null;
  if (job.mode === 'discover') {
    return { label: '查看数据概览', to: `/jobs/${job.job_id}/discovery` };
  }
  if (job.mode === 'plan') {
    return { label: '查看处理方案', to: `/jobs/${job.job_id}/plan` };
  }
  return { label: '查看处理结果', to: `/jobs/${job.job_id}/result` };
}

function nextStepTitle(job: NonNullable<ReturnType<typeof useJobStatus>['job']>) {
  if (job.status === 'failed') return '查看失败原因后重新提交';
  if (job.status === 'cancelled') return '任务已取消';
  if (job.status === 'needs_clarification') return '补充信息后继续处理';
  if (job.status === 'awaiting_confirmation') return '核对方案后开始执行';
  if (job.status !== 'succeeded') return '任务正在自动处理';
  if ((job.pending_review_count ?? 0) > 0) return '先完成异常复核';
  if ((job.review_count ?? 0) > 0) return '复核结论已保存';
  if (job.mode === 'answer') return '结果已可以下载使用';
  return job.mode === 'plan' ? '方案已可以审阅' : '数据概览已生成';
}

function nextStepDescription(job: NonNullable<ReturnType<typeof useJobStatus>['job']>) {
  if (job.error_message) return job.error_message;
  if (job.status === 'running' || job.status === 'pending') {
    return '页面会自动更新，无需重复提交。你可以先查看已生成的数据概览。';
  }
  if ((job.pending_review_count ?? 0) > 0) {
    return '复核结论会单独保存；下载文件保持任务完成时的版本。';
  }
  if ((job.review_count ?? 0) > 0) {
    return '问题记录已完成业务判断；下载文件仍为任务完成时的版本。';
  }
  if (job.mode === 'answer' && job.status === 'succeeded') {
    return '建议先浏览结果预览，确认后再下载 final_result.xlsx。';
  }
  return '通过下方入口查看本次任务的完整信息。';
}

function formatTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit'
  }).format(date);
}
