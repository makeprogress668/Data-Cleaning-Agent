import type { OutputFile, OutputSpec } from '../../api/types';
import { withApiBase } from '../../api/config';
import { AppIcon } from '../common/AppIcon';

interface OutputFilesProps {
  files: OutputFile[];
  outputSpec?: OutputSpec;
  resultRowCount?: number;
  reviewCount?: number;
  pendingReviewCount?: number;
}

const FILE_TYPE_LABELS: Record<string, string> = {
  excel: 'Excel 工作簿',
  csv: 'CSV 文件',
  json: 'JSON 数据',
  html: 'HTML 报告',
  markdown: 'Markdown 报告',
  pdf: 'PDF 文档'
};

function isDownloadable(url: string): boolean {
  return Boolean(url) && !url.startsWith('#');
}

export function OutputFiles({
  files,
  outputSpec,
  resultRowCount,
  reviewCount = 0,
  pendingReviewCount = reviewCount
}: OutputFilesProps) {
  const downloadable = files.filter((file) => isDownloadable(file.url));
  const primary = downloadable.find((file) => file.name === 'final_result.xlsx');
  const secondary = downloadable.filter((file) => file !== primary);

  return (
    <section className="card deliverables-card">
      <div className="section-heading section-heading-row">
        <div>
          <p className="eyebrow">交付产物</p>
          <h2>结果文件</h2>
          <p>下载完整结果；需要确认的问题记录会放在同一工作簿中。</p>
        </div>
        {primary ? (
          <a
            className="primary-button"
            download
            href={withApiBase(primary.url)}
            rel="noopener noreferrer"
            target="_blank"
          >
            <AppIcon name="download" />
            下载结果
          </a>
        ) : null}
      </div>

      {primary ? (
        <article className="primary-file">
          <span className="primary-file-icon"><AppIcon name="file" /></span>
          <div className="primary-file-main">
            <strong>{primary.name}</strong>
            <p>
              {resultRowCount == null ? '本次任务的主要处理结果' : `${resultRowCount} 行处理结果`}
              {reviewCount > 0
                ? `，另有 ${reviewCount} 条问题记录，其中 ${pendingReviewCount} 条待确认`
                : '，当前没有待处理问题'}
            </p>
            <div className="sheet-list">
              <span>
                <AppIcon name="check" />
                <strong>处理结果</strong>
                <small>可直接查看和使用的业务数据</small>
              </span>
              {(outputSpec?.secondary_artifacts ?? []).map((artifact) => (
                <span key={artifact.name}>
                  <AppIcon name="warning" />
                  <strong>{artifact.name}</strong>
                  <small>{artifact.description || '需要业务确认的问题记录'}</small>
                </span>
              ))}
            </div>
          </div>
          <a
            aria-label={`下载 ${primary.name}`}
            className="file-download-button"
            download
            href={withApiBase(primary.url)}
            rel="noopener noreferrer"
            target="_blank"
          >
            <AppIcon name="download" />
          </a>
        </article>
      ) : (
        <p className="table-empty">暂无可下载的结果文件。</p>
      )}

      {secondary.length > 0 ? (
        <div className="secondary-files">
          {secondary.map((file) => (
            <a
              download
              href={withApiBase(file.url)}
              key={`${file.name}-${file.url}`}
              rel="noopener noreferrer"
              target="_blank"
            >
              <AppIcon name="file" />
              <span>
                <strong>{file.name}</strong>
                <small>{FILE_TYPE_LABELS[file.type] ?? file.type}</small>
              </span>
              <AppIcon name="download" />
            </a>
          ))}
        </div>
      ) : null}
    </section>
  );
}
