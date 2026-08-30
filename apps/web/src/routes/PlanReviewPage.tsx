import { useParams } from 'react-router-dom';

import { capabilityLabel } from '../api/labels';
import type { ExecutionStep } from '../api/types';
import { AppIcon } from '../components/common/AppIcon';
import { EmptyState } from '../components/common/EmptyState';
import { ErrorState } from '../components/common/ErrorState';
import { LoadingState } from '../components/common/LoadingState';
import { ProcessingState } from '../components/common/ProcessingState';
import { useJobPlan } from '../hooks/useJobPlan';
import { useJobStatus } from '../hooks/useJobStatus';

export function PlanReviewPage() {
  const { jobId } = useParams();
  const { job, isMissing } = useJobStatus(jobId);
  const { plan, isLoading, error, notReady } = useJobPlan(jobId, job?.status);
  const isPlanOnly = job?.mode === 'plan';
  const planWasExecuted = job?.mode === 'answer' && job.status === 'succeeded';
  const unknownEstimateLabel = planWasExecuted ? '未提前预估' : '待确认';

  if (!jobId) return <MissingPlan />;
  if (isMissing) {
    return (
      <div className="page-stack">
        <EmptyState
          title="任务已不存在"
          description="无法读取这次任务的处理方案，请选择其他任务或重新提交。"
          actionLabel="新建任务"
          actionTo="/upload"
        />
      </div>
    );
  }

  return (
    <div className="page-stack">
      <section className="page-intro">
        <div>
          <p className="eyebrow">{isPlanOnly ? '处理前 · 审阅方案' : '任务方案'}</p>
          <h2>{isPlanOnly ? '拟定的处理方案' : '本次处理方案'}</h2>
          <p>重点查看会做什么、影响多少记录、依据哪些规则，以及最终交付什么。</p>
        </div>
      </section>

      {isLoading ? <LoadingState label="正在读取处理方案" /> : null}
      {notReady && !isLoading ? (
        <ProcessingState
          label="正在生成处理方案"
          hint="完成后可查看处理步骤、影响范围和交付内容。"
        />
      ) : null}
      {error ? <ErrorState message={`处理方案加载失败：${error}`} /> : null}

      {plan ? (
        <>
          <section className="plan-impact">
            <div className="plan-impact-heading">
              <span><AppIcon name="plan" /></span>
              <div>
                <p className="eyebrow">预计影响</p>
                <h2>
                  {impactHeadline(
                    plan.impact_preview.input_rows,
                    plan.impact_preview.estimated_removed_rows,
                    planWasExecuted
                  )}
                </h2>
                <p>以 <strong>{plan.main_table || '待确认主表'}</strong> 作为本次处理主表。</p>
              </div>
            </div>
            <div className="impact-numbers">
              <ImpactMetric label="输入记录" value={plan.impact_preview.input_rows} />
              <ImpactMetric
                label="预计输出"
                value={plan.impact_preview.estimated_output_rows ?? unknownEstimateLabel}
              />
              <ImpactMetric
                label="预计移除"
                tone={(plan.impact_preview.estimated_removed_rows ?? 0) > 0 ? 'warning' : 'neutral'}
                value={plan.impact_preview.estimated_removed_rows ?? unknownEstimateLabel}
              />
            </div>
            <div className="impact-notes">
              <p><strong>涉及字段：</strong>{plan.impact_preview.affected_fields.join('、') || '尚未确定'}</p>
              <p>
                <strong>新增字段：</strong>
                {plan.impact_preview.added_fields.length
                  ? plan.impact_preview.added_fields.join('、')
                  : '不会新增业务字段'}
              </p>
            </div>
          </section>

          <section className="card">
            <div className="section-heading">
              <p className="eyebrow">处理顺序</p>
              <h2>处理步骤</h2>
              <p>会改变记录数量的步骤已单独标记，并会在处理前请你确认。</p>
            </div>
            <ExecutionPlan steps={plan.execution_steps} />
          </section>

          <div className="two-column">
            <section className="card">
              <div className="section-heading">
                <h2>业务规则</h2>
                <p>用于判断哪些记录通过、哪些记录需要复核。</p>
              </div>
              <FriendlyList
                empty="本次没有额外业务校验规则。"
                items={[...new Set([...plan.anomaly_rules, ...plan.document_rules])]}
              />
            </section>
            <section className="card">
              <div className="section-heading">
                <h2>交付内容</h2>
                <p>任务完成后可查看和下载以下内容。</p>
              </div>
              <ul className="delivery-list">
                <li><AppIcon name="file" /><span><strong>{plan.output_spec.primary_artifact.name}</strong><small>{plan.output_spec.primary_artifact.description}</small></span></li>
                {plan.output_spec.secondary_artifacts.map((artifact) => (
                  <li key={artifact.name}><AppIcon name="file" /><span><strong>{artifact.name}</strong><small>{artifact.description}</small></span></li>
                ))}
                {plan.output_spec.charts.map((chart) => (
                  <li key={chart.chart_id}><AppIcon name="result" /><span><strong>指定图表</strong><small>{chart.dimension} / {chart.metric}</small></span></li>
                ))}
              </ul>
            </section>
          </div>

          {plan.risks.length > 0 ? (
            <section className="attention-banner static">
              <span className="attention-icon"><AppIcon name="warning" /></span>
              <div><strong>需要注意</strong><p>{plan.risks.join('；')}</p></div>
            </section>
          ) : null}

        </>
      ) : null}
    </div>
  );
}

function MissingPlan() {
  return (
    <div className="page-stack">
      <EmptyState
        title="暂无处理方案"
        description="新建任务后，可在这里查看处理步骤、影响范围、业务规则和交付内容。"
        actionLabel="新建任务"
        actionTo="/upload"
      />
    </div>
  );
}

function ImpactMetric({
  label,
  value,
  tone = 'neutral'
}: {
  label: string;
  value: string | number;
  tone?: 'neutral' | 'warning';
}) {
  return <div className={`impact-number tone-${tone}`}><span>{label}</span><strong>{value}</strong></div>;
}

function ExecutionPlan({ steps }: { steps: ExecutionStep[] }) {
  return (
    <ol className="execution-plan">
      {steps.map((step, index) => {
        const content = capabilityLabel(step.capability_id);
        return (
          <li key={step.step_id}>
            <span className="execution-index">{index + 1}</span>
            <span className="execution-copy"><strong>{content.title}</strong><small>{content.detail}</small></span>
            {step.requires_confirmation ? <span className="risk-label">执行前确认</span> : null}
          </li>
        );
      })}
    </ol>
  );
}

function FriendlyList({ items, empty }: { items: string[]; empty: string }) {
  if (!items.length) return <p className="table-empty">{empty}</p>;
  return <ul className="plain-insight-list">{items.map((item) => <li key={item}><AppIcon name="check" />{item}</li>)}</ul>;
}

function impactHeadline(input: number, removed: number | null, executed: boolean) {
  if (removed == null) {
    return executed
      ? `${input} 行输入，执行前未预估输出规模`
      : `${input} 行输入，输出规模待确认`;
  }
  if (removed > 0) return `${input} 行输入，预计移除 ${removed} 行`;
  return `${input} 行输入，预计保留全部记录`;
}
