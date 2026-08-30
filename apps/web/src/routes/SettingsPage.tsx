import { friendlyFileTypes } from '../api/labels';
import { AppIcon } from '../components/common/AppIcon';
import { ErrorState } from '../components/common/ErrorState';
import { LoadingState } from '../components/common/LoadingState';
import { useRuntimeConfig } from '../hooks/useRuntimeConfig';

export function SettingsPage() {
  const { config, isLoading, error } = useRuntimeConfig();

  return (
    <div className="page-stack">
      <section className="page-intro">
        <div>
          <p className="eyebrow">使用说明</p>
          <h2>开始任务前需要了解的信息</h2>
          <p>查看支持的文件类型、上传限制、数据保护方式和结果交付格式。</p>
        </div>
      </section>

      {isLoading ? <LoadingState label="正在读取使用说明" /> : null}
      {error ? <ErrorState message={`使用说明加载失败：${error}`} /> : null}

      {config ? (
        <div className="settings-grid">
          <SettingCard
            icon="check"
            title="服务状态"
            value="可正常使用"
            description="新建任务、处理数据、预览和下载结果均可正常使用。"
            tone="success"
          />
          {config.understanding ? (
            <SettingCard
              icon="spark"
              title="目标理解"
              value={config.understanding.label}
              description={config.understanding.description}
              tone="neutral"
            />
          ) : null}
          <SettingCard
            icon="data"
            title="数据保护"
            value={config.data_protection.label}
            description={config.data_protection.description}
          />
          <SettingCard
            icon="file"
            title="文件上传"
            value={`单个文件不超过 ${config.upload.max_upload_mb} MB`}
            description={`支持 ${friendlyFileTypes(config.upload.allowed_extensions)}。可以同时上传数据文件和配套说明文档。`}
          />
          <SettingCard
            icon="download"
            title="结果交付"
            value="默认交付 Excel"
            description="可直接使用的数据和需要确认的问题会分别整理，并放在同一工作簿中。"
          />
        </div>
      ) : null}
    </div>
  );
}

function SettingCard({
  icon,
  title,
  value,
  description,
  tone = 'neutral'
}: {
  icon: 'check' | 'data' | 'file' | 'download' | 'spark';
  title: string;
  value: string;
  description: string;
  tone?: 'neutral' | 'success';
}) {
  return (
    <section className={`card setting-summary tone-${tone}`}>
      <span className="setting-summary-icon"><AppIcon name={icon} /></span>
      <div>
        <p className="eyebrow">{title}</p>
        <h2>{value}</h2>
        <p>{description}</p>
      </div>
    </section>
  );
}
