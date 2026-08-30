import { AppIcon } from '../common/AppIcon';

interface BusinessConclusionProps {
  conclusion: string;
  findings: string[];
  actions: string[];
}

export function BusinessConclusion({ conclusion, findings, actions }: BusinessConclusionProps) {
  return (
    <section className="card conclusion-card">
      <div className="conclusion-summary">
        <span className="conclusion-icon"><AppIcon name="spark" /></span>
        <div>
          <p className="eyebrow">处理结论</p>
          <h2>{conclusion}</h2>
        </div>
      </div>

      {(findings.length > 0 || actions.length > 0) ? (
        <div className="conclusion-columns">
          <div>
            <h3>本次处理发现</h3>
            <ul className="plain-insight-list">
              {findings.slice(0, 5).map((finding) => (
                <li key={finding}><AppIcon name="check" />{finding}</li>
              ))}
            </ul>
          </div>
          <div className="next-actions">
            <h3>建议下一步</h3>
            <ol>
              {actions.map((action) => (
                <li key={action}>{action}</li>
              ))}
            </ol>
          </div>
        </div>
      ) : null}
    </section>
  );
}
