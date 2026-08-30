interface DataPreviewTableProps {
  rows: ReadonlyArray<Record<string, unknown>>;
  emptyLabel?: string;
  maxRows?: number;
}

const COLUMN_LABELS: Record<string, string> = {
  name: '数据表',
  rows: '记录数',
  columns: '字段数',
  role: '用途',
  table: '数据表',
  field: '字段',
  confidence: '置信度',
  uniqueness: '唯一性',
  from: '来源',
  to: '关联目标',
  match_rate: '匹配率',
  recommendation: '建议',
  record_id: '记录编号',
  source_row: '源行号',
  source_table: '来源表',
  '处理状态': '处理状态',
  '问题类型': '问题类型',
  '建议处理': '建议处理',
  '影响字段': '影响字段',
  '当前值': '当前值',
  '源行号': '源行号'
};

export function DataPreviewTable({
  rows,
  emptyLabel = '暂无数据。',
  maxRows = 200
}: DataPreviewTableProps) {
  if (rows.length === 0) {
    return <p className="table-empty">{emptyLabel}</p>;
  }

  const columns = Array.from(
    rows.reduce<Set<string>>((keys, row) => {
      Object.keys(row).forEach((key) => keys.add(key));
      return keys;
    }, new Set<string>())
  );
  const visibleRows = rows.slice(0, maxRows);
  const hiddenCount = rows.length - visibleRows.length;

  return (
    <div className="table-wrap data-table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column} title={column}>
                {COLUMN_LABELS[column] ?? humanizeColumn(column)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {visibleRows.map((row, index) => (
            <tr key={index}>
              {columns.map((column) => (
                <td key={column}>{renderValue(column, row[column])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {hiddenCount > 0 ? (
        <p className="table-footnote">
          当前展示前 {maxRows} 行，其余 {hiddenCount} 行请在结果文件中查看。
        </p>
      ) : null}
    </div>
  );
}

function renderValue(column: string, value: unknown) {
  if (value == null || value === '') {
    return <span className="empty-value">空值</span>;
  }
  const text = String(value);
  if (column === '处理状态') {
    const tone = text === '通过' ? 'success' : text === '需处理' ? 'warning' : 'neutral';
    return <span className={`cell-status tone-${tone}`}>{text}</span>;
  }
  if (column === 'confidence') {
    const labels: Record<string, string> = { high: '高', medium: '中', low: '低' };
    return labels[text] ?? text;
  }
  return text;
}

function humanizeColumn(column: string) {
  return column
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (character) => character.toUpperCase());
}
