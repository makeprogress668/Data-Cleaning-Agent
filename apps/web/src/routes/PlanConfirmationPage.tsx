import { useNavigate, useParams } from 'react-router-dom';

import { capabilityLabel } from '../api/labels';
import type { ConfirmationReason, RetentionRule, TaskSpec } from '../api/types';
import { AppIcon } from '../components/common/AppIcon';
import { EmptyState } from '../components/common/EmptyState';
import { ErrorState } from '../components/common/ErrorState';
import { LoadingState } from '../components/common/LoadingState';
import { GoalUnderstandingSummary } from '../components/report/GoalUnderstandingSummary';
import { usePlanConfirmation } from '../hooks/usePlanConfirmation';

export function PlanConfirmationPage() {
  const { jobId } = useParams();
  const navigate = useNavigate();
  const {
    understanding,
    goal,
    executionPlan,
    outputSpec,
    confirm,
    isLoading,
    isSubmitting,
    error
  } = usePlanConfirmation(jobId);

  if (!jobId) {
    return (
      <div className="page-stack">
        <EmptyState
          title="暂无待确认的计划"
          description="需要确认筛选、去重或交付内容时，方案会显示在这里。请先新建一个任务。"
          actionLabel="去新建任务"
          actionTo="/upload"
        />
      </div>
    );
  }

  async function handleConfirm() {
    const ok = await confirm();
    if (ok) {
      navigate(`/jobs/${jobId}`);
    }
  }

  return (
    <div className="page-stack">
      {isLoading ? <LoadingState label="正在读取处理计划" /> : null}
      {error ? <ErrorState message={`读取处理计划时出错：${error}`} /> : null}

      <section className="page-intro">
        <div>
          <p className="eyebrow">处理前 · 方案确认</p>
          <h2>请确认处理范围和交付内容</h2>
          <p>
            数据尚未开始处理。请重点核对会改变记录数量的步骤和最终交付内容。
          </p>
          {goal ? <p className="muted">任务目标：{goal}</p> : null}
        </div>
      </section>

      {!isLoading && understanding ? (
        <section className="card">
          <div className="section-heading">
            <h2>本次将这样处理</h2>
            <p>请确认处理数据、筛选去重规则和预计影响符合你的业务目标。</p>
          </div>
          <GoalUnderstandingSummary understanding={understanding} />
          <InferredRuleWarning taskSpec={understanding.task_spec} />
          <ConfirmationReasonSummary executionPlan={executionPlan} />
          <div className="plan-grid">
            <article className="plan-card">
              <h3>处理步骤</h3>
              {executionPlan?.steps.length ? (
                <ol>
                  {executionPlan.steps.map((step) => (
                    <li key={step.step_id}>
                      {capabilityLabel(step.capability_id).title}
                      {step.requires_confirmation
                        ? `（需确认：${step.confirmation_reasons
                            .map(confirmationReasonLabel)
                            .join('、')}）`
                        : ''}
                    </li>
                  ))}
                </ol>
              ) : (
                <p>暂无处理步骤</p>
              )}
            </article>
            <article className="plan-card">
              <h3>交付内容</h3>
              {outputSpec ? (
                <ul>
                  <li>{outputSpec.primary_artifact.name}</li>
                  {outputSpec.secondary_artifacts.map((artifact) => (
                    <li key={artifact.name}>{artifact.name}</li>
                  ))}
                  {outputSpec.charts.map((chart) => (
                    <li key={chart.chart_id}>
                      图表：{chart.dimension} / {chart.metric}
                    </li>
                  ))}
                  {outputSpec.report.enabled ? <li>结果说明</li> : null}
                  {outputSpec.include_audit ? <li>处理记录</li> : null}
                </ul>
              ) : (
                <p>暂无交付内容</p>
              )}
            </article>
          </div>
          <div className="button-row">
            <button
              className="primary-button"
              type="button"
              onClick={handleConfirm}
              disabled={isSubmitting}
            >
              {isSubmitting ? '正在开始…' : '确认无误，开始处理'}
            </button>
            <button
              className="secondary-button"
              type="button"
              onClick={() => navigate('/upload', { state: { goal } })}
            >
              重新上传并修改目标
            </button>
          </div>
        </section>
      ) : null}

      {!isLoading && !understanding && !error ? (
        <section className="card">
          <p>该任务当前没有待确认的计划，可返回任务详情查看进度。</p>
          <div className="button-row">
            <button
              className="secondary-button"
              type="button"
              onClick={() => navigate(`/jobs/${jobId}`)}
            >
              返回任务详情
            </button>
          </div>
        </section>
      ) : null}
    </div>
  );
}

function ConfirmationReasonSummary({
  executionPlan
}: {
  executionPlan: ReturnType<typeof usePlanConfirmation>['executionPlan'];
}) {
  const reasons = Array.from(
    new Set(
      executionPlan?.steps.flatMap((step) => step.confirmation_reasons ?? []) ?? []
    )
  );
  if (reasons.length === 0) {
    return null;
  }
  return (
    <div className="inferred-rule-warning">
      <span className="summary-icon"><AppIcon name="warning" /></span>
      <div>
        <strong>这份计划触发了风险复核，请重点核对</strong>
        <ul>
          {reasons.map((reason) => (
            <li key={reason}>{confirmationReasonDescription(reason)}</li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function confirmationReasonLabel(reason: ConfirmationReason): string {
  const labels: Record<ConfirmationReason, string> = {
    missing_authorization: '缺少明确原话',
    impact_unknown: '影响暂时无法估算',
    high_removal_ratio: '预计移除比例较高',
    fuzzy_matching: '使用模糊匹配'
  };
  return labels[reason];
}

function confirmationReasonDescription(reason: ConfirmationReason): string {
  const descriptions: Record<ConfirmationReason, string> = {
    missing_authorization: '删行规则缺少可引用的用户原话，需要你明确批准。',
    impact_unknown: '当前无法可靠估算会影响多少行，需要先核对规则。',
    high_removal_ratio: '预计移除至少四分之一的主表记录，属于高影响操作。',
    fuzzy_matching: '模糊匹配可能把名称相似但实际不同的记录关联在一起。'
  };
  return descriptions[reason];
}

/**
 * Highlight row rules the model inferred from meaning rather than lifting from the
 * user's words. These are the rules most worth a second look before执行, because a
 * mis-inferred filter deletes records.
 */
function InferredRuleWarning({ taskSpec }: { taskSpec?: TaskSpec }) {
  const inferred = [
    ...(taskSpec?.filters ?? []),
    ...(taskSpec?.deduplication ?? [])
  ].filter((rule) => rule.source === 'llm');

  if (inferred.length === 0) {
    return null;
  }

  return (
    <div className="inferred-rule-warning">
      <span className="summary-icon"><AppIcon name="warning" /></span>
      <div>
        <strong>以下删行规则由系统根据你的描述推断，请重点核对</strong>
        <ul>
          {inferred.map((rule, index) => (
            <li key={`${describeRule(rule)}-${index}`}>{describeRule(rule)}</li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function describeRule(rule: RetentionRule): string {
  if (rule.fields?.length) {
    return `按 ${rule.fields.join(' + ')} 去重（保留${
      rule.strategy === 'last' ? '最后' : '第一'
    }条）`;
  }
  const action = rule.mode === 'exclude' ? '排除' : '保留';
  const value = rule.value ?? (rule.values as unknown[] | undefined)?.join('、') ?? '';
  return `${action} ${rule.field} ${rule.op} ${String(value)} 的记录`;
}
