import type { ChartItem } from '../../api/types';
import { withApiBase } from '../../api/config';

interface ChartPanelProps {
  charts: ChartItem[];
  exceptionImpact: Array<Record<string, unknown>>;
}

function isEmbeddable(url: string): boolean {
  // Anchor placeholders (e.g. "#exception-impact") are not real chart artifacts.
  return Boolean(url) && !url.startsWith('#');
}

export function ChartPanel({ charts, exceptionImpact }: ChartPanelProps) {
  const maxCount = Math.max(
    1,
    ...exceptionImpact.map((item) => Number(item.count ?? 0)).filter(Number.isFinite)
  );
  const embeddable = charts.filter((chart) => isEmbeddable(chart.url));
  const hasImpact = exceptionImpact.length > 0;

  return (
    <section className="card">
      <div className="section-heading">
        <h2>异常分布</h2>
        <p>按异常类型展示各自影响的行数，便于优先处理最主要的问题。</p>
      </div>

      {hasImpact ? (
        <div className="chart-list" role="img" aria-label="各类异常影响行数条形图">
          {exceptionImpact.map((item) => {
            const label = String(item.issue_type ?? 'unknown');
            const count = Number(item.count ?? 0);
            return (
              <div className="bar-row" key={label}>
                <span title={label}>{label}</span>
                <div className="bar-track">
                  <div
                    className="bar-value"
                    style={{ width: `${(count / maxCount) * 100}%` }}
                  />
                </div>
                <strong>{count}</strong>
              </div>
            );
          })}
        </div>
      ) : (
        <p>本次没有异常影响，结果可直接使用。</p>
      )}

      {embeddable.length > 0 ? (
        <div className="chart-embed-list">
          {embeddable.map((chart) => (
            <figure className="chart-embed" key={`${chart.title}-${chart.url}`}>
              <figcaption>{chart.title}</figcaption>
              {chart.type === 'image' ? (
                <img src={withApiBase(chart.url)} alt={chart.title} loading="lazy" />
              ) : (
                <iframe title={chart.title} src={withApiBase(chart.url)} loading="lazy" />
              )}
            </figure>
          ))}
        </div>
      ) : null}
    </section>
  );
}
