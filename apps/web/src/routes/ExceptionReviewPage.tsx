import { useCallback, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';

import { withApiBase } from '../api/config';
import { rematerializeReviewedResult } from '../api/jobs';
import type { ReviewApplied, ReviewRow } from '../api/types';
import { AppIcon } from '../components/common/AppIcon';
import { EmptyState } from '../components/common/EmptyState';
import { ErrorState } from '../components/common/ErrorState';
import { LoadingState } from '../components/common/LoadingState';
import { ProcessingState } from '../components/common/ProcessingState';
import { ExceptionReviewTable } from '../components/table/ExceptionReviewTable';
import { useJobStatus } from '../hooks/useJobStatus';
import { useReview } from '../hooks/useReview';

type Filter = 'all' | ReviewRow['review_status'];

export function ExceptionReviewPage() {
  const { jobId } = useParams();
  const { job, isMissing } = useJobStatus(jobId);
  const {
    items,
    total,
    truncated,
    hasMore,
    decisions,
    persist,
    loadMore,
    isLoading,
    isLoadingMore,
    isSaving,
    error,
    notReady
  } = useReview(jobId, job?.status);
  const [filter, setFilter] = useState<Filter>('all');
  const [applied, setApplied] = useState<ReviewApplied | null>(null);
  const [isApplying, setIsApplying] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);

  const applyDecisions = useCallback(async () => {
    if (!jobId) return;
    setIsApplying(true);
    setApplyError(null);
    try {
      setApplied(await rematerializeReviewedResult(jobId));
      window.dispatchEvent(new CustomEvent('job-status-changed', { detail: { jobId } }));
    } catch (requestError) {
      setApplyError(
        requestError instanceof Error ? requestError.message : '复核结果生成失败'
      );
    } finally {
      setIsApplying(false);
    }
  }, [jobId]);

  const rows = useMemo<ReviewRow[]>(
    () => items.map((item) => ({ ...item, review_status: decisions[item.id] ?? 'pending' })),
    [items, decisions]
  );
  const visibleRows = filter === 'all'
    ? rows
    : rows.filter((row) => row.review_status === filter);
  const pendingCount = job?.pending_review_count
    ?? rows.filter((row) => row.review_status === 'pending').length;
  const acceptedCount = Object.values(decisions).filter((status) => status === 'accepted').length;
  const excludedCount = Object.values(decisions).filter((status) => status === 'excluded').length;
  const completedCount = Math.max(0, total - pendingCount);

  if (!jobId) return <MissingReview />;
  if (isMissing) {
    return (
      <div className="page-stack">
        <EmptyState
          title="任务已不存在"
          description="无法读取这次任务的复核记录，请选择其他任务或重新提交。"
          actionLabel="新建任务"
          actionTo="/upload"
        />
      </div>
    );
  }

  return (
    <div className="page-stack">
      {isLoading ? <LoadingState label="正在读取问题记录" /> : null}
      {notReady && !isLoading ? (
        <ProcessingState label="复核清单生成中" hint="任务完成后会显示需要业务判断的真实记录。" />
      ) : null}
      {error ? <ErrorState message={`复核操作失败：${error}`} /> : null}

      <section className="page-intro review-intro">
        <div>
          <p className="eyebrow">处理后 · 业务判断</p>
          <h2>确认哪些问题记录可以使用</h2>
          <p>
            判断会自动保存。完成后可一键生成「复核后的结果」——确认可用的记录会回到结果表，
            确认不采用的会移出。源数据始终不会被修改。
          </p>
        </div>
        {isSaving ? (
          <span className="save-state">正在保存…</span>
        ) : error ? (
          <span className="save-state">保存状态异常</span>
        ) : !isLoading && !notReady ? (
          <span className="save-state saved"><AppIcon name="check" />结论自动保存</span>
        ) : null}
      </section>

      {job?.status === 'succeeded' && !error && rows.length > 0 ? (
        <>
          <section className="review-progress-card">
            <div className="review-progress-copy">
              <span className="summary-icon"><AppIcon name={pendingCount ? 'warning' : 'check'} /></span>
              <div>
                <strong>{pendingCount ? `还有 ${pendingCount} 条待复核` : '全部问题记录已完成判断'}</strong>
                <p>共 {total} 条问题记录，已处理 {completedCount} 条。</p>
              </div>
            </div>
            <div className="review-progress-track">
              <span style={{ width: `${total ? ((total - pendingCount) / total) * 100 : 100}%` }} />
            </div>
            <div className="review-filter" role="group" aria-label="复核状态筛选">
              <FilterButton active={filter === 'all'} label={`全部 ${total}`} onClick={() => setFilter('all')} />
              <FilterButton active={filter === 'pending'} label={`待复核 ${pendingCount}`} onClick={() => setFilter('pending')} />
              <FilterButton active={filter === 'accepted'} label={`确认可用 ${acceptedCount}`} onClick={() => setFilter('accepted')} />
              <FilterButton active={filter === 'excluded'} label={`不采用 ${excludedCount}`} onClick={() => setFilter('excluded')} />
            </div>
          </section>

          <section className="card review-table-card">
            <div className="section-heading">
              <h2>问题记录</h2>
              <p>“确认不采用”只记录判断；如需修改字段值，请修正源数据后重新提交。</p>
            </div>
            {visibleRows.length ? (
              <ExceptionReviewTable rows={visibleRows} onStatusChange={(id, status) => void persist(id, status)} />
            ) : (
              <p className="table-empty">当前筛选条件下没有记录。</p>
            )}
            {hasMore ? (
              <div className="button-row">
                <button
                  className="secondary-button"
                  disabled={isLoadingMore}
                  onClick={() => void loadMore()}
                  type="button"
                >
                  {isLoadingMore ? '正在加载…' : `继续加载问题记录（已显示 ${rows.length}/${total}）`}
                </button>
              </div>
            ) : truncated ? (
              <p className="table-footnote">其余问题记录暂未加载，请刷新页面后重试。</p>
            ) : null}
          </section>

          <section className="card review-apply-card">
            <div className="section-heading">
              <h2>生成复核后的结果</h2>
              <p>
                按当前判断重新生成一份结果文件：确认可用的记录会并入结果表，
                确认不采用的会移出。原始结果文件保留不变，可随时对照。
              </p>
            </div>
            {applyError ? <ErrorState message={`复核结果生成失败：${applyError}`} /> : null}
            <div className="button-row">
              <button
                className="primary-button"
                disabled={isApplying || acceptedCount + excludedCount === 0}
                onClick={() => void applyDecisions()}
                type="button"
              >
                {isApplying ? '正在生成…' : '按复核结论生成结果'}
              </button>
              {applied ? (
                <a
                  className="secondary-button"
                  download
                  href={withApiBase(`/api/v1/jobs/${jobId}/files/${applied.file_name}`)}
                  rel="noopener noreferrer"
                  target="_blank"
                >
                  <AppIcon name="download" />
                  下载 {applied.file_name}
                </a>
              ) : null}
            </div>
            {acceptedCount + excludedCount === 0 ? (
              <p className="table-footnote">先对至少一条记录做出判断，再生成结果。</p>
            ) : null}
            {applied ? (
              <p className="table-footnote">
                已生成 {applied.result_row_count} 行结果：并入 {applied.accepted_rows} 条确认可用、
                移出 {applied.excluded_rows} 条确认不采用
                {applied.pending_rows ? `，仍有 ${applied.pending_rows} 条待复核未计入` : ''}。
              </p>
            ) : null}
          </section>
        </>
      ) : null}

      {job?.status === 'succeeded' && !isLoading && !notReady && !error && rows.length === 0 ? (
        <section className="review-empty-success">
          <span><AppIcon name="check" /></span>
          <h2>本次没有需要人工确认的记录</h2>
          <p>所有结果均通过当前业务规则校验，可以直接查看和下载。</p>
        </section>
      ) : null}
    </div>
  );
}

function FilterButton({
  active,
  label,
  onClick
}: {
  active: boolean;
  label: string;
  onClick: () => void;
}) {
  return <button className={active ? 'active' : ''} onClick={onClick} type="button">{label}</button>;
}

function MissingReview() {
  return (
    <div className="page-stack">
      <EmptyState
        title="暂无复核记录"
        description="完成一个包含问题检查的任务后，需要业务判断的记录会显示在这里。"
        actionLabel="新建任务"
        actionTo="/upload"
      />
    </div>
  );
}
