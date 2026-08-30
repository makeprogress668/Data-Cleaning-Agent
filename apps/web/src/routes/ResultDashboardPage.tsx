import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import { withApiBase } from '../api/config';
import {
  branchResultVersion,
  getResultVersions,
  saveRecipe,
  selectResultVersion,
  undoResultVersion
} from '../api/jobs';
import type { BusinessAnswer, KeyMetric, ResultVersionState } from '../api/types';
import { AppIcon } from '../components/common/AppIcon';
import { EmptyState } from '../components/common/EmptyState';
import { ErrorState } from '../components/common/ErrorState';
import { LoadingState } from '../components/common/LoadingState';
import { ProcessingState } from '../components/common/ProcessingState';
import { BusinessConclusion } from '../components/report/BusinessConclusion';
import { ChartPanel } from '../components/report/ChartPanel';
import { GoalUnderstandingSummary } from '../components/report/GoalUnderstandingSummary';
import { SummaryReconciliation } from '../components/report/SummaryReconciliation';
import { OutputFiles } from '../components/report/OutputFiles';
import { DataPreviewTable } from '../components/table/DataPreviewTable';
import { useJobEvents } from '../hooks/useJobEvents';
import { useJobResult } from '../hooks/useJobResult';
import { useJobStatus } from '../hooks/useJobStatus';

export function ResultDashboardPage() {
  const { jobId } = useParams();
  const { job, isMissing } = useJobStatus(jobId);
  const { result, isLoading, error, notReady } = useJobResult(jobId, {
    refreshKey: job?.status
  });
  const events = useJobEvents(jobId, job?.status);
  const [versions, setVersions] = useState<ResultVersionState | null>(null);
  const [versionError, setVersionError] = useState<string | null>(null);
  const [versionBusy, setVersionBusy] = useState(false);
  const [recipeName, setRecipeName] = useState('');
  const [recipeSaved, setRecipeSaved] = useState(false);

  useEffect(() => {
    if (!jobId || !result) return;
    let active = true;
    getResultVersions(jobId)
      .then((state) => { if (active) setVersions(state); })
      .catch((requestError) => {
        if (active) setVersionError(requestError instanceof Error ? requestError.message : '结果版本加载失败');
      });
    return () => { active = false; };
  }, [jobId, result]);

  async function changeVersion(versionId: string) {
    if (!jobId || !versions) return;
    setVersionBusy(true);
    setVersionError(null);
    try {
      const response = await selectResultVersion(jobId, versionId);
      setVersions({ ...versions, selected_version_id: response.selected_version_id });
    } catch (requestError) {
      setVersionError(requestError instanceof Error ? requestError.message : '结果版本切换失败');
    } finally {
      setVersionBusy(false);
    }
  }

  async function undoVersion() {
    if (!jobId || !versions) return;
    setVersionBusy(true);
    setVersionError(null);
    try {
      const response = await undoResultVersion(jobId);
      setVersions({ ...versions, selected_version_id: response.selected_version_id });
    } catch (requestError) {
      setVersionError(requestError instanceof Error ? requestError.message : '结果版本撤销失败');
    } finally {
      setVersionBusy(false);
    }
  }

  async function branchVersion() {
    if (!jobId || !versions) return;
    setVersionBusy(true);
    setVersionError(null);
    try {
      await branchResultVersion(jobId, versions.selected_version_id);
      setVersions(await getResultVersions(jobId));
    } catch (requestError) {
      setVersionError(requestError instanceof Error ? requestError.message : '结果分支创建失败');
    } finally {
      setVersionBusy(false);
    }
  }

  async function createRecipeFromResult() {
    if (!jobId || !recipeName.trim()) return;
    setVersionError(null);
    try {
      await saveRecipe(jobId, recipeName.trim());
      setRecipeSaved(true);
    } catch (requestError) {
      setVersionError(requestError instanceof Error ? requestError.message : 'Recipe 保存失败');
    }
  }

  if (!jobId) {
    return <MissingResult />;
  }
  if (isMissing) {
    return (
      <div className="page-stack">
        <EmptyState
          title="结果所属任务已不存在"
          description="该任务可能已删除或超过保存期限，请选择其他任务或重新提交。"
          actionLabel="新建任务"
          actionTo="/upload"
        />
      </div>
    );
  }
  if (
    job?.status === 'succeeded'
    && (job.mode !== 'answer' || job.has_result === false)
  ) {
    return (
      <div className="page-stack">
        <EmptyState
          title={job.mode === 'plan' ? '这个任务只生成处理方案' : '这个任务只生成数据概览'}
          description="本次没有执行数据处理，因此不会生成结果文件。"
          actionLabel="返回任务总览"
          actionTo={`/jobs/${job.job_id}`}
        />
      </div>
    );
  }
  if (!result) {
    return (
      <div className="page-stack">
        {isLoading ? <LoadingState label="正在读取处理结果" /> : null}
        {notReady && !isLoading ? (
          <ProcessingState
            label="处理结果尚未生成"
            hint="任务完成后会自动出现结果预览和下载入口。"
          />
        ) : null}
        {error ? <ErrorState message={`处理结果加载失败：${error}`} /> : null}
      </div>
    );
  }

  const answer: BusinessAnswer = result;
  const primaryOutput = answer.output_files.find((file) => file.name === 'final_result.xlsx');
  const metrics = metricMap(answer.key_metrics);
  const pendingReview = job?.pending_review_count ?? answer.review_item_count;
  const reviewCompleted = answer.review_item_count > 0 && pendingReview === 0;
  const score = answer.data_quality_score;
  const usableRows = metrics.get('可直接使用')?.value ?? job?.counts?.valid ?? 0;
  const totalRows = Number(metrics.get('总记录数')?.value ?? job?.counts?.total ?? 0);
  const reviewApplied = answer.review_applied ?? null;
  const displayConclusion = reviewCompleted
    ? '处理结果已生成，问题记录已完成业务判断'
    : answer.review_item_count === 0 && totalRows > 0
      ? `处理完成，${usableRows} 条数据可直接使用`
      : answer.final_conclusion;
  const reviewedOutput = reviewApplied
    ? answer.output_files.find((file) => file.name === reviewApplied.file_name)
    : undefined;
  const pendingMetric = {
    label: '待业务确认',
    value: pendingReview,
    description: pendingReview > 0 ? '建议使用结果前完成复核' : '当前没有待确认记录'
  };
  const selectedVersion = versions?.versions.find(
    (item) => item.version_id === versions.selected_version_id
  );

  return (
    <div className="page-stack result-page">
      {error ? <ErrorState message={`结果刷新失败：${error}`} /> : null}

      <section className="result-hero">
        <div className="result-state-mark">
          <AppIcon name={pendingReview > 0 ? 'warning' : 'check'} />
        </div>
        <div className="result-hero-copy">
          <p className="eyebrow">任务处理完成</p>
          <h2>{displayConclusion}</h2>
          <p className="result-goal">目标：{answer.goal}</p>
          <div className="result-status-row">
            <span className={pendingReview > 0 ? 'result-readiness warning' : 'result-readiness success'}>
              {pendingReview > 0
                ? `${pendingReview} 条仍需业务复核`
                : reviewCompleted
                  ? '问题记录已完成业务判断'
                  : '当前结果可直接查看使用'}
            </span>
            <span>
              {pendingReview > 0
                ? '建议复核后再用于正式业务'
                : reviewApplied
                  ? `复核结论已应用，可下载 ${reviewApplied.file_name}`
                  : reviewCompleted
                    ? '复核结论已保存，可在复核页生成应用了判断的结果文件'
                    : '建议导入前做一次业务抽查'}
            </span>
          </div>
        </div>
        <div className="result-hero-actions">
          {primaryOutput ? (
            <a
              className="primary-button"
              download
              href={withApiBase(primaryOutput.url)}
              rel="noopener noreferrer"
              target="_blank"
            >
              <AppIcon name="download" />
              下载 Excel
            </a>
          ) : null}
          {reviewedOutput ? (
            <a
              className="secondary-button"
              download
              href={withApiBase(reviewedOutput.url)}
              rel="noopener noreferrer"
              target="_blank"
            >
              <AppIcon name="download" />
              下载复核后结果
            </a>
          ) : null}
          {answer.review_item_count > 0 ? (
            <Link className="secondary-button" to={`/jobs/${answer.job_id}/review`}>
              查看问题记录
            </Link>
          ) : null}
        </div>
      </section>

      <section className="card version-workflow">
        <div className="section-heading section-heading-row">
          <div>
            <p className="eyebrow">可追溯交付</p>
            <h2>结果版本与 Recipe</h2>
            <p>切换只移动当前指针，不删除历史文件；撤销返回父版本，分支保留两条结果链。</p>
          </div>
          {selectedVersion ? (
            <a className="secondary-button" download href={withApiBase(selectedVersion.url)}>
              <AppIcon name="download" />下载所选版本
            </a>
          ) : null}
        </div>
        {versionError ? <ErrorState message={versionError} /> : null}
        <div className="version-controls">
          <label>
            <span>当前结果版本</span>
            <select
              disabled={versionBusy}
              value={versions?.selected_version_id ?? ''}
              onChange={(event) => void changeVersion(event.target.value)}
            >
              {(versions?.versions ?? []).map((item) => (
                <option key={item.version_id} value={item.version_id}>
                  v{item.version_number} · {item.kind === 'original' ? '原始结果' : '复核结果'}
                </option>
              ))}
            </select>
          </label>
          <div className="button-row">
            <button
              className="secondary-button"
              disabled={versionBusy || !selectedVersion?.parent_version_id}
              type="button"
              onClick={() => void undoVersion()}
            >
              撤销到父版本
            </button>
            <button
              className="secondary-button"
              disabled={versionBusy || !selectedVersion}
              type="button"
              onClick={() => void branchVersion()}
            >
              从当前版本创建分支
            </button>
          </div>
        </div>
        <div className="recipe-save-row">
          <label>
            <span>保存本次处理定义为 Recipe</span>
            <input
              disabled={recipeSaved}
              placeholder="例如：月度订单汇总"
              value={recipeName}
              onChange={(event) => setRecipeName(event.target.value)}
            />
          </label>
          <button
            className="secondary-button"
            disabled={recipeSaved || !recipeName.trim()}
            type="button"
            onClick={() => void createRecipeFromResult()}
          >
            {recipeSaved ? '已保存' : '保存 Recipe'}
          </button>
        </div>
      </section>

      <section className="result-metric-strip" aria-label="结果关键指标">
        <ResultMetric label="输入记录" metric={metrics.get('总记录数')} />
        <ResultMetric label="可直接使用" metric={metrics.get('可直接使用')} tone="success" />
        <ResultMetric
          label="待业务确认"
          metric={pendingMetric}
          tone={pendingReview > 0 ? 'warning' : 'neutral'}
        />
        {metrics.has('按目标排除') ? (
          <ResultMetric label="按目标排除" metric={metrics.get('按目标排除')} />
        ) : null}
        <article className={`result-metric ${score >= 85 ? 'tone-success' : 'tone-warning'}`}>
          <span>质量评分</span>
          <strong>{score}<small>/100</small></strong>
          <p>{score >= 85 ? '整体质量良好' : '建议重点检查问题记录'}</p>
        </article>
      </section>

      {answer.result_preview.length > 0 ? (
        <section className="card result-preview-card">
          <div className="section-heading section-heading-row">
            <div>
              <p className="eyebrow">主要结果</p>
              <h2>结果预览</h2>
              <p>先在页面中抽查关键数据，确认后再下载完整文件。</p>
            </div>
            <span className="preview-count">展示前 {Math.min(10, answer.result_preview.length)} 行</span>
          </div>
          <DataPreviewTable rows={answer.result_preview} maxRows={10} />
        </section>
      ) : null}

      <BusinessConclusion
        actions={answer.recommended_actions}
        conclusion={displayConclusion}
        findings={answer.key_findings}
      />

      <OutputFiles
        files={answer.output_files}
        outputSpec={answer.output_spec}
        resultRowCount={job?.result_row_count}
        reviewCount={answer.review_item_count}
        pendingReviewCount={pendingReview}
      />

      {answer.charts.length > 0 || answer.exception_impact.length > 0 ? (
        <ChartPanel charts={answer.charts} exceptionImpact={answer.exception_impact} />
      ) : null}

      {answer.goal_understanding ? (
        <details className="card result-details">
          <summary>
            <span>查看本次处理说明</span>
            <small>处理范围、业务动作和采用的假设</small>
          </summary>
          <div className="result-details-content">
            <GoalUnderstandingSummary understanding={answer.goal_understanding} />
            <SummaryReconciliation events={events} />
          </div>
        </details>
      ) : null}
    </div>
  );
}

function MissingResult() {
  return (
    <div className="page-stack">
      <EmptyState
        title="暂无处理结果"
        description="新建并完成一个直接处理任务后，结果会显示在这里。"
        actionLabel="新建任务"
        actionTo="/upload"
      />
    </div>
  );
}

function metricMap(metrics: KeyMetric[]) {
  return new Map(metrics.map((metric) => [metric.label, metric]));
}

function ResultMetric({
  label,
  metric,
  tone = 'neutral'
}: {
  label: string;
  metric?: KeyMetric;
  tone?: 'neutral' | 'success' | 'warning';
}) {
  return (
    <article className={`result-metric tone-${tone}`}>
      <span>{label}</span>
      <strong>{metric?.value ?? 0}</strong>
      <p>{metric?.description ?? '本次任务统计'}</p>
    </article>
  );
}
