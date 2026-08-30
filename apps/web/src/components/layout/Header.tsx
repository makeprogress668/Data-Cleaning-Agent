import { useLocation } from 'react-router-dom';

import { useRuntimeConfig } from '../../hooks/useRuntimeConfig';

export function Header() {
  const location = useLocation();
  const { config, isLoading, error } = useRuntimeConfig();
  const connected = Boolean(config) && !error;
  const statusText = isLoading ? '正在准备' : connected ? '可正常使用' : '暂不可用';
  const statusClass = isLoading ? 'status-dot pending' : connected ? 'status-dot' : 'status-dot offline';
  const page = pageTitle(location.pathname);

  return (
    <header className="topbar">
      <div>
        <p className="topbar-context">数据处理工作台</p>
        <h1>{page}</h1>
      </div>
      <div className="topbar-status">
        <span className={statusClass} />
        {statusText}
      </div>
    </header>
  );
}

function pageTitle(pathname: string): string {
  if (pathname === '/upload') return '新建任务';
  if (pathname === '/settings') return '使用说明';
  if (pathname.endsWith('/discovery')) return '数据概览';
  if (pathname.endsWith('/plan')) return '处理方案';
  if (pathname.endsWith('/result')) return '处理结果';
  if (pathname.endsWith('/review')) return '异常复核';
  if (pathname.endsWith('/clarification')) return '补充任务信息';
  if (pathname.endsWith('/plan-confirmation')) return '确认处理方案';
  if (pathname.startsWith('/jobs/')) return '任务总览';
  return '数据炼金师';
}
