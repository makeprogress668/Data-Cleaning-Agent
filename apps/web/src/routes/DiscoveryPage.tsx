import { useParams } from 'react-router-dom';

import { AppIcon } from '../components/common/AppIcon';
import { DataPreviewTable } from '../components/table/DataPreviewTable';
import { EmptyState } from '../components/common/EmptyState';
import { ErrorState } from '../components/common/ErrorState';
import { LoadingState } from '../components/common/LoadingState';
import { ProcessingState } from '../components/common/ProcessingState';
import { useDiscovery } from '../hooks/useDiscovery';
import { useJobStatus } from '../hooks/useJobStatus';

export function DiscoveryPage() {
  const { jobId } = useParams();
  const { job, isMissing } = useJobStatus(jobId);
  const { discovery, isLoading, error, notReady } = useDiscovery(jobId, job?.status);

  if (!jobId) {
    return <MissingDiscovery />;
  }
  if (isMissing) {
    return (
      <div className="page-stack">
        <EmptyState
          title="任务已不存在"
          description="无法读取这次任务的数据概览，请选择其他任务或重新提交。"
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
          <p className="eyebrow">处理前 · 数据概览</p>
          <h2>这批数据包含什么</h2>
          <p>核对输入文件、数据表、关键字段和初步问题，再查看处理方案。</p>
        </div>
      </section>

      {isLoading ? <LoadingState label="正在读取数据概览" /> : null}
      {notReady && !isLoading ? (
        <ProcessingState
          label="正在整理数据概览"
          hint="完成后可查看数据表、关联字段和初步问题。"
        />
      ) : null}
      {error ? <ErrorState message={`数据概览加载失败：${error}`} /> : null}

      {discovery ? (
        <>
          <section className="discovery-summary-grid">
            <SummaryCard
              icon="file"
              label="输入文件"
              value={discovery.files.length}
              detail={discovery.files.join('、') || '未识别到文件'}
            />
            <SummaryCard
              icon="data"
              label="数据表"
              value={discovery.tables.length}
              detail={primaryTableDescription(discovery.tables)}
            />
            <SummaryCard
              icon="plan"
              label="表间关系"
              value={discovery.relationships.length}
              detail={discovery.relationships.length ? '已找到可用于关联的字段' : '未发现明显跨表关系'}
            />
            <SummaryCard
              icon={discovery.issues.length ? 'warning' : 'check'}
              label="初步问题"
              tone={discovery.issues.length ? 'warning' : 'success'}
              value={discovery.issues.length}
              detail={discovery.issues.length ? '建议在方案中确认处理方式' : '未发现明显空值问题'}
            />
          </section>

          {discovery.issues.length > 0 ? (
            <section className="card issue-summary-card">
              <div className="section-heading">
                <p className="eyebrow">需要关注</p>
                <h2>初步数据质量问题</h2>
                <p>请在处理方案中确认这些问题是否需要修正。</p>
              </div>
              <ul className="issue-list">
                {discovery.issues.map((issue) => (
                  <li key={issue}><AppIcon name="warning" /><span>{issue}</span></li>
                ))}
              </ul>
            </section>
          ) : null}

          <section className="card">
            <div className="section-heading">
              <p className="eyebrow">数据地图</p>
              <h2>数据表及用途</h2>
              <p>用于核对本次任务的主要数据表和辅助数据表。</p>
            </div>
            <DataPreviewTable rows={discovery.tables} emptyLabel="暂无可展示的数据表。" />
          </section>

          <div className="two-column">
            <section className="card">
              <div className="section-heading">
                <h2>关键字段候选</h2>
                <p>可用于去重、关联或定位单条记录。</p>
              </div>
              <DataPreviewTable rows={discovery.keys} emptyLabel="未找到可靠的关键字段。" />
            </section>
            <section className="card">
              <div className="section-heading">
                <h2>数据表关系</h2>
                <p>展示可用于跨表补齐信息的关联候选。</p>
              </div>
              <DataPreviewTable rows={discovery.relationships} emptyLabel="未发现明显的表间关联。" />
            </section>
          </div>
        </>
      ) : null}
    </div>
  );
}

function MissingDiscovery() {
  return (
    <div className="page-stack">
      <EmptyState
        title="暂无数据概览"
        description="新建任务后，可在这里查看输入文件、数据表和初步质量问题。"
        actionLabel="新建任务"
        actionTo="/upload"
      />
    </div>
  );
}

function SummaryCard({
  icon,
  label,
  value,
  detail,
  tone = 'neutral'
}: {
  icon: 'file' | 'data' | 'plan' | 'warning' | 'check';
  label: string;
  value: number;
  detail: string;
  tone?: 'neutral' | 'warning' | 'success';
}) {
  return (
    <article className={`discovery-summary-card tone-${tone}`}>
      <span className="summary-icon"><AppIcon name={icon} /></span>
      <div><span>{label}</span><strong>{value}</strong><p>{detail}</p></div>
    </article>
  );
}

function primaryTableDescription(tables: Array<{ name: string; role: string }>) {
  const primary = tables.find((table) => /主表|primary/i.test(table.role));
  return primary ? `建议主表：${primary.name}` : tables[0] ? `包含：${tables[0].name}` : '暂无数据表';
}
