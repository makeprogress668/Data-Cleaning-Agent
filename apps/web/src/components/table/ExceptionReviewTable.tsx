import type { ReviewRow } from '../../api/types';
import { reviewStatusLabel, severityLabel } from '../../api/labels';

interface ExceptionReviewTableProps {
  rows: ReviewRow[];
  onStatusChange: (id: string, status: ReviewRow['review_status']) => void;
}

const statuses: ReviewRow['review_status'][] = ['pending', 'accepted', 'excluded'];

export function ExceptionReviewTable({ rows, onStatusChange }: ExceptionReviewTableProps) {
  return (
    <div className="table-wrap review-table-wrap">
      <table>
        <thead>
          <tr>
            <th>来源记录</th>
            <th>发现的问题</th>
            <th>影响字段</th>
            <th>当前值</th>
            <th>建议处理</th>
            <th>业务结论</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <td><strong>{row.id}</strong><small>源行 {row.source_row}</small></td>
              <td><strong>{row.issue_type}</strong><span className={`severity severity-${row.severity}`}>{severityLabel(row.severity)}优先级</span></td>
              <td><code>{row.affected_field || '—'}</code></td>
              <td>{row.current_value ? row.current_value : <span className="empty-value">空值</span>}</td>
              <td>{row.recommended_action}</td>
              <td>
                <select
                  aria-label={`设置 ${row.id} 的复核状态`}
                  className={`review-select status-${row.review_status}`}
                  value={row.review_status}
                  onChange={(event) =>
                    onStatusChange(row.id, event.target.value as ReviewRow['review_status'])
                  }
                >
                  {statuses.map((status) => (
                    <option key={status} value={status}>{reviewStatusLabel(status)}</option>
                  ))}
                </select>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
