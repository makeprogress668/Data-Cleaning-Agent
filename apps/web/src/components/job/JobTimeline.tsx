import type { JobStatus } from '../../api/types';

interface JobTimelineProps {
  status: JobStatus;
  mode?: 'discover' | 'plan' | 'answer';
  errorMessage?: string;
  failureStage?: 'planning' | 'execution' | 'delivery';
}

type StageState = 'done' | 'active' | 'pending' | 'failed';

interface Stage {
  key: string;
  stage: string;
  description: string;
  state: StageState;
}

/**
 * Derive a truthful progress timeline from the real job status, instead of the old
 * hard-coded "all done" mock. The engine runs discovery → plan → execute → deliver
 * as one synchronous job, so we reflect coarse progress rather than fake per-step
 * timestamps: while running everything up to "执行清洗" is in-flight; on success all
 * stages are done; on failure the delivery stage is marked failed.
 */
function buildStages(
  status: JobStatus,
  mode: 'discover' | 'plan' | 'answer',
  errorMessage?: string,
  failureStage?: 'planning' | 'execution' | 'delivery'
): Stage[] {
  const labels: Array<{ key: string; stage: string; description: string }> = [
    { key: 'received', stage: '接收文件', description: '已接收上传的表格与说明文档' },
    { key: 'discovery', stage: '数据概览', description: '识别主表、关联键和字段情况' },
    { key: 'plan', stage: '生成方案', description: '把目标转成可核对的处理方案' },
    { key: 'execute', stage: '执行处理', description: '匹配、计算、校验并分级复核' },
    { key: 'deliver', stage: '输出结果', description: '生成结论、评分、图表和可下载文件' }
  ];
  const failedIndex =
    failureStage === 'delivery'
      ? 4
      : failureStage === 'execution'
        ? 3
        : failureStage === 'planning'
          ? 2
          : 1;

  return labels.map((item, index) => {
    let state: StageState = 'pending';
    if (status === 'succeeded') {
      const completedIndex = mode === 'discover' ? 1 : mode === 'plan' ? 2 : 4;
      state = index <= completedIndex ? 'done' : 'pending';
    } else if (status === 'failed' || status === 'cancelled') {
      state = index < failedIndex ? 'done' : index === failedIndex ? 'failed' : 'pending';
    } else if (status === 'needs_clarification') {
      // Paused after planning, waiting for the user to answer questions: file +
      // discovery done, "生成方案" is the active (paused) stage.
      state = index <= 1 ? 'done' : index === 2 ? 'active' : 'pending';
    } else if (status === 'awaiting_confirmation') {
      // Planned but not executed, waiting for the user to approve the plan: file +
      // discovery done, "生成方案" is the active (paused) stage.
      state = index <= 1 ? 'done' : index === 2 ? 'active' : 'pending';
    } else if (status === 'running' || status === 'pending') {
      state = index === 0 ? 'done' : index === 1 ? 'active' : 'pending';
    }
    return { ...item, state };
  }).map((stage) =>
    stage.state === 'failed' && errorMessage
      ? { ...stage, description: errorMessage }
      : stage
  );
}

export function JobTimeline({
  status,
  mode = 'answer',
  errorMessage,
  failureStage
}: JobTimelineProps) {
  const stages = buildStages(status, mode, errorMessage, failureStage);

  return (
    <section className="card">
      <div className="section-heading">
        <h2>处理进度</h2>
        <p>
          {mode === 'discover'
            ? '本次只识别数据结构，不执行数据处理。'
            : mode === 'plan'
              ? '本次只生成方案，不执行数据处理。'
              : '从接收文件到输出结果的实时进度。'}
        </p>
      </div>
      <ol className="timeline">
        {stages.map((item) => (
          <li key={item.key}>
            <span className={`timeline-dot ${item.state}`} />
            <div>
              <strong>{item.stage}</strong>
              <p>{item.description}</p>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
