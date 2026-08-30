import type { ExecutionEvent } from '../../api/types';

/**
 * 「汇总表和明细表为什么对不上」——这个问题必须有答案。
 *
 * 汇总不是明细的简单相加：上传里的合计行不能再被计入它自己汇总的那个总数，写着
 * 「待确认」的金额也加不进去。执行器一直在记录这两个数，但没有任何界面读过，
 * 于是用户看到 4 行明细、总数却只算了 3 行时，无处可查。
 *
 * 什么都没发生就什么都不显示 —— 一条「0 行被排除」的提示只是噪音。
 */
export function SummaryReconciliation({ events }: { events: ExecutionEvent[] }) {
  const aggregate = events.find((event) => event.capability_id === 'aggregate');
  const metrics = aggregate?.metrics ?? {};
  const excludedRows = toCount(metrics.excluded_non_data_rows);
  const unreadable = toCount(metrics.unreadable_measure_values);

  if (excludedRows === 0 && unreadable === 0) {
    return null;
  }

  return (
    <div className="insight-box">
      <strong>汇总口径说明</strong>
      <ul>
        {excludedRows > 0 ? (
          <li>
            {excludedRows} 行不是数据记录（合计行、重复表头等），保留在处理结果里，
            但不计入汇总。
          </li>
        ) : null}
        {unreadable > 0 ? (
          <li>
            {unreadable} 个数值无法识别为数字，未计入合计。
            这些单元格原样保留在处理结果里，可核对后再决定如何处理。
          </li>
        ) : null}
      </ul>
    </div>
  );
}

function toCount(value: unknown): number {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.trunc(parsed) : 0;
}
