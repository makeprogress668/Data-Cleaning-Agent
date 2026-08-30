import type {
  GoalUnderstanding,
  TaskDeduplication,
  TaskFilter
} from '../../api/types';

const ACTION_LABELS: Record<string, string> = {
  clean: '清洗并规范数据',
  filter: '按条件筛选记录',
  deduplicate: '按规则去重',
  lookup: '跨表补齐字段',
  derive: '计算目标字段',
  validate: '校验业务规则',
  analyze: '统计分析',
  chart: '生成指定图表',
  review: '输出待确认记录',
  annotate: '标记目标记录',
  export: '导出结果'
};

export function GoalUnderstandingSummary({
  understanding
}: {
  understanding: GoalUnderstanding;
}) {
  const confidence = understanding.understanding_confidence;
  const showWarning =
    understanding.is_generic_fallback === true || confidence === 'low';
  const assumptions = understanding.assumptions ?? [];
  const taskSpec = understanding.task_spec;
  const scope = taskSpec
    ? unique([
        taskSpec.primary_entity ?? '',
        ...taskSpec.target_tables
      ])
    : [];
  const rules = taskSpec
    ? [
        ...taskSpec.filters.map(formatFilter),
        ...taskSpec.deduplication.map(formatDeduplication)
      ]
    : [];
  const outputs = unique([
    ...(taskSpec?.output_intent.requested_outputs ?? []),
    ...(understanding.requested_outputs ?? [])
  ]);
  const notes = unique([
    ...assumptions,
    ...(taskSpec?.missing_slots.map((slot) => slot.question) ?? [])
  ]);

  return (
    <div className="understanding-summary">
      {understanding.understanding_source === 'deterministic_fallback' ? (
        <div className="notice-warning">
          <strong>本次未能调用语义理解，已按关键词规则处理</strong>
          <p>
            结果可能与平时不同：规则只认得固定说法，同义表述会被漏掉。
            {understanding.understanding_fallback_reason
              ? `（原因：${understanding.understanding_fallback_reason}）`
              : null}
            建议稍后重试；确认下方处理范围无误后再使用本次结果。
          </p>
        </div>
      ) : null}

      {showWarning ? (
        <div className="notice-warning">
          <strong>处理范围还不够明确</strong>
          <p>
            当前描述可能产生多种理解。建议核对下方处理范围，必要时返回修改目标后重新提交。
          </p>
        </div>
      ) : null}

      {taskSpec ? (
        <>
          <div className="insight-box">
            <strong>任务目标</strong>
            <p>{taskSpec.objective}</p>
          </div>
          {understanding.impact_preview ? (
            <ImpactPreview preview={understanding.impact_preview} />
          ) : null}
          <div className="plan-grid">
            {scope.length > 0 ? <GoalList title="处理数据" items={scope} /> : null}
            <GoalList
              title="完成动作"
              items={taskSpec.actions.map((action) => ACTION_LABELS[action] ?? action)}
            />
            {rules.length > 0 ? <GoalList title="筛选与去重" items={rules} /> : null}
            {outputs.length > 0 ? <GoalList title="交付内容" items={outputs} /> : null}
            {notes.length > 0 ? <GoalList title="注意事项" items={notes} /> : null}
          </div>
        </>
      ) : (
        <div className="plan-grid">
          {(understanding.focus ?? []).length > 0 ? (
            <GoalList title="处理重点" items={understanding.focus ?? []} />
          ) : null}
          {outputs.length > 0 ? <GoalList title="交付内容" items={outputs} /> : null}
          {notes.length > 0 ? <GoalList title="注意事项" items={notes} /> : null}
        </div>
      )}
    </div>
  );
}

function ImpactPreview({
  preview
}: {
  preview: NonNullable<GoalUnderstanding['impact_preview']>;
}) {
  return (
    <div className="insight-box">
      <strong>预计影响</strong>
      <p>
        主表 {preview.base_table || '尚未确定'} 共 {preview.input_rows} 行。
        {preview.estimate_available
          ? `预计输出 ${preview.estimated_output_rows ?? 0} 行，移除 ${preview.estimated_removed_rows ?? 0} 行（约 ${((preview.estimated_removal_ratio ?? 0) * 100).toFixed(1)}%）。`
          : '当前无法可靠估算输出行数，执行后会按实际结果校验。'}
      </p>
      {preview.affected_fields.length > 0 ? (
        <p>涉及字段：{preview.affected_fields.join('、')}</p>
      ) : null}
      {preview.added_fields.length > 0 ? (
        <p>将新增字段：{preview.added_fields.join('、')}</p>
      ) : (
        <p>不会新增业务字段。</p>
      )}
    </div>
  );
}

const OPERATOR_LABELS: Record<string, string> = {
  gt: '大于',
  gte: '大于等于',
  lt: '小于',
  lte: '小于等于',
  equals: '等于',
  not_equals: '不等于',
  in: '属于',
  not_in: '不属于'
};

function formatFilter(item: TaskFilter): string {
  const mode = item.mode === 'exclude' ? '排除' : '保留';
  const operand = Array.isArray(item.values)
    ? item.values.join('、')
    : String(item.value ?? '');
  return `${mode} ${String(item.field ?? '')} ${OPERATOR_LABELS[String(item.op)] ?? String(item.op ?? '')} ${operand}`.trim();
}

function formatDeduplication(item: TaskDeduplication): string {
  const fields = item.fields.join('、');
  const strategy = {
    first: '保留第一条',
    last: '保留最后一条',
    latest: '保留时间最新的一条',
    earliest: '保留时间最早的一条'
  }[item.strategy];
  return `按 ${fields} 去重，${strategy}`;
}

function GoalList({ title, items }: { title: string; items: string[] }) {
  return (
    <article className="plan-card">
      <h3>{title}</h3>
      {items.length > 0 ? (
        <ul>
          {items.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      ) : (
        <p>暂无</p>
      )}
    </article>
  );
}

function unique(items: string[]) {
  return [...new Set(items.map((item) => item.trim()).filter(Boolean))];
}
