import type { JobStatus, ReviewRow } from './types';

export const CAPABILITY_LABELS: Record<string, { title: string; detail: string }> = {
  read_tables: { title: '读取上传数据', detail: '识别工作簿、数据表和字段结构' },
  extract_document_rules: { title: '读取说明规则', detail: '从配套文档中提取明确要求' },
  profile_dataset: { title: '了解数据结构', detail: '定位主要数据表、关键字段和质量问题' },
  detect_relationships: { title: '识别数据关系', detail: '寻找可用于跨表补齐的关联字段' },
  detect_anomalies: { title: '检查基础问题', detail: '处理空格、不可见字符等常见问题' },
  map_fields: { title: '整理字段', detail: '按目标统一字段名称和含义' },
  trim: { title: '清理多余空格', detail: '去除字段内容首尾的无效空格' },
  normalize_case: { title: '统一字母格式', detail: '按目标统一英文大小写' },
  normalize_width: { title: '统一字符格式', detail: '规范全角和半角字符' },
  map_values: { title: '规范字段值', detail: '按目标统一字段内容' },
  fill_missing: { title: '补充缺失值', detail: '按已确认规则补充可确定的空值' },
  filter_rows: { title: '按目标筛选记录', detail: '仅应用已经确认的筛选条件' },
  deduplicate: { title: '按规则去重', detail: '按确认的关键字段保留目标记录' },
  join_tables: { title: '关联数据表', detail: '按确认的关键字段连接相关数据' },
  lookup_fields: { title: '跨表补齐字段', detail: '按已识别关系补充目标信息' },
  derive_column: { title: '计算目标字段', detail: '只生成目标明确要求的新字段' },
  aggregate: { title: '汇总业务数据', detail: '按指定维度计算目标指标' },
  validate_rules: { title: '核对业务规则', detail: '标记不符合要求或需要确认的记录' },
  pivot: { title: '汇总数据', detail: '按目标维度整理统计结果' },
  summarize: { title: '整理结果摘要', detail: '归纳本次处理的关键结论' },
  top_n: { title: '识别重点数据', detail: '按目标指标找出主要项目' },
  trend_analysis: { title: '分析变化趋势', detail: '按指定时间和指标整理趋势' },
  score_quality: { title: '检查结果质量', detail: '核对结果完整性和任务要求' },
  generate_chart: { title: '生成指定图表', detail: '只生成目标中明确要求的图表' },
  build_audit: { title: '保留处理记录', detail: '记录本次处理范围和关键动作' },
  export_table: { title: '生成结果文件', detail: '导出 final_result.xlsx' },
  export_report: { title: '生成结果说明', detail: '输出目标明确要求的结果说明' }
};

/** Chinese display labels for job status, so the UI never leaks raw enum values. */
export const JOB_STATUS_LABELS: Record<JobStatus, string> = {
  pending: '排队中',
  running: '处理中',
  needs_clarification: '待确认',
  awaiting_confirmation: '待确认执行',
  succeeded: '已完成',
  failed: '处理失败',
  cancelled: '已取消'
};

/** Chinese display labels for the human-review decision states. */
export const REVIEW_STATUS_LABELS: Record<ReviewRow['review_status'], string> = {
  pending: '待复核',
  accepted: '确认可用',
  excluded: '确认不采用'
};

export const SEVERITY_LABELS: Record<string, string> = {
  high: '高',
  medium: '中',
  low: '低'
};

export function jobStatusLabel(status: JobStatus): string {
  return JOB_STATUS_LABELS[status] ?? status;
}

export function reviewStatusLabel(status: ReviewRow['review_status']): string {
  return REVIEW_STATUS_LABELS[status] ?? status;
}

export function severityLabel(severity: string): string {
  return SEVERITY_LABELS[severity] ?? severity;
}

export function capabilityLabel(capabilityId: string) {
  return CAPABILITY_LABELS[capabilityId] ?? {
    title: '完成必要处理',
    detail: '按已确认的目标和规则执行'
  };
}

export function friendlyFileTypes(extensions: string[]) {
  const groups = [
    { label: 'Excel', values: ['.xls', '.xlsx', '.xlsm'] },
    { label: 'CSV', values: ['.csv'] },
    { label: 'JSON', values: ['.json'] },
    { label: 'PDF 和 Word', values: ['.pdf', '.doc', '.docx'] },
    { label: '文本说明', values: ['.txt', '.md', '.markdown'] }
  ];
  const available = new Set(extensions);
  return groups
    .filter((group) => group.values.some((value) => available.has(value)))
    .map((group) => group.label)
    .join('、');
}
