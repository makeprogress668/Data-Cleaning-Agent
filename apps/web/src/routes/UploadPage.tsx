import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { getRecipes, getSemanticModels } from '../api/jobs';
import type { JobMode, RecipeSummary, SemanticModelSummary } from '../api/types';
import { AppIcon } from '../components/common/AppIcon';
import { ErrorState } from '../components/common/ErrorState';
import { LoadingState } from '../components/common/LoadingState';
import { FileUploader } from '../components/upload/FileUploader';
import { GoalInput } from '../components/upload/GoalInput';
import { useCreateJob } from '../hooks/useCreateJob';
import { useRuntimeConfig } from '../hooks/useRuntimeConfig';
import { saveRecentJob } from '../hooks/useRecentJob';

const MODE_LABELS: Record<JobMode, { title: string; hint: string }> = {
  discover: { title: '仅数据概览', hint: '只梳理数据结构和质量，不做处理' },
  plan: { title: '仅生成方案', hint: '生成可审阅的处理方案，先看不执行' },
  answer: { title: '直接处理', hint: '按目标完成处理并输出结果（推荐）' }
};

const FLOW_STEPS = [
  { title: '上传数据', hint: '上传表格和说明文档' },
  { title: '说明目标', hint: '用一句话说明要做什么' },
  { title: '获取结果', hint: '得到处理后的数据和复核清单' }
];

export function UploadPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const initialGoal =
    (location.state as { goal?: string } | null)?.goal?.trim() || '';
  const [files, setFiles] = useState<File[]>([]);
  const [goal, setGoal] = useState(initialGoal);
  const [mode, setMode] = useState<JobMode>('answer');
  const [recipes, setRecipes] = useState<RecipeSummary[]>([]);
  const [semanticModels, setSemanticModels] = useState<SemanticModelSummary[]>([]);
  const [recipeId, setRecipeId] = useState('');
  const [semanticModelId, setSemanticModelId] = useState('');
  const [localError, setLocalError] = useState<string | null>(null);
  const { createJob, isLoading, error } = useCreateJob();
  const { config } = useRuntimeConfig();

  useEffect(() => {
    let active = true;
    Promise.all([getRecipes(), getSemanticModels()])
      .then(([loadedRecipes, loadedModels]) => {
        if (!active) return;
        setRecipes(loadedRecipes);
        setSemanticModels(loadedModels);
      })
      .catch(() => {
        // These are optional accelerators; task creation remains available.
      });
    return () => { active = false; };
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLocalError(null);

    if (files.length === 0) {
      setLocalError('请先上传至少一个数据文件或规则文档。');
      return;
    }

    if (!goal.trim()) {
      setLocalError('请填写本次要完成的处理目标。');
      return;
    }

    try {
      const response = await createJob({
        files,
        goal,
        mode,
        recipeId: recipeId || undefined,
        semanticModelId: semanticModelId || undefined
      });
      saveRecentJob({
        job_id: response.job_id,
        goal,
        mode,
        created_at: new Date().toISOString()
      });
      window.dispatchEvent(new CustomEvent('job-list-changed'));
      navigate(`/jobs/${response.job_id}`);
    } catch {
      // useCreateJob exposes the request error for rendering.
    }
  }

  return (
    <form className="page-stack" onSubmit={handleSubmit}>
      <section className="upload-intro">
        <div>
          <p className="eyebrow">新建任务</p>
          <h2>上传数据，说明目标，获取处理结果</h2>
          <p>
            支持上传表格、模板和说明文档。任务完成后可在线预览结果、下载 Excel，
            需要确认的问题会单独列出。
          </p>
        </div>
        <button className="primary-button" type="submit" disabled={isLoading}>
          {isLoading ? '正在创建任务…' : <><AppIcon name="spark" />开始处理</>}
        </button>
      </section>

      <ol className="flow-steps" aria-label="处理流程">
        {FLOW_STEPS.map((step, index) => (
          <li className={index === 0 ? 'flow-step active' : 'flow-step'} key={step.title}>
            <span className="flow-step-index">{index + 1}</span>
            <div className="flow-step-body">
              <strong>{step.title}</strong>
              <span>{step.hint}</span>
            </div>
          </li>
        ))}
      </ol>

      {(localError || error) && <ErrorState message={localError ?? error ?? ''} />}
      {isLoading ? <LoadingState label="正在提交任务" /> : null}

      <div className="upload-workspace">
        <FileUploader
          files={files}
          onChange={setFiles}
          allowedExtensions={config?.upload.allowed_extensions}
          maxUploadMb={config?.upload.max_upload_mb}
        />
        <GoalInput value={goal} onChange={setGoal} disabled={Boolean(recipeId)} />
      </div>

      {(recipes.length > 0 || semanticModels.length > 0) ? (
        <section className="card reusable-contracts">
          <div className="section-heading">
            <h2>复用已验收定义（可选）</h2>
            <p>Recipe 固定原任务目标；业务语义模型把业务指标和表间关系映射到本次上传。</p>
          </div>
          <div className="contract-selectors">
            <label>
              <span>Recipe</span>
              <select
                value={recipeId}
                onChange={(event) => {
                  const value = event.target.value;
                  setRecipeId(value);
                  if (value) {
                    setSemanticModelId('');
                    const selected = recipes.find((item) => item.recipe_id === value);
                    if (selected) setGoal(selected.goal);
                  }
                }}
              >
                <option value="">不使用 Recipe</option>
                {recipes.map((item) => (
                  <option key={item.recipe_id} value={item.recipe_id}>{item.name}</option>
                ))}
              </select>
            </label>
            <label>
              <span>业务语义模型</span>
              <select
                disabled={Boolean(recipeId)}
                value={semanticModelId}
                onChange={(event) => setSemanticModelId(event.target.value)}
              >
                <option value="">不使用业务语义模型</option>
                {semanticModels.map((item) => (
                  <option key={item.semantic_model_id} value={item.semantic_model_id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
        </section>
      ) : null}

      <section className="card">
        <div className="section-heading">
          <h2>处理方式</h2>
          <p>默认「直接处理」会按目标输出结果；如需先看清楚，可选择仅概览或仅生成方案。</p>
        </div>
        <div className="mode-options">
          {(Object.keys(MODE_LABELS) as JobMode[]).map((item) => (
            <label key={item} className={mode === item ? 'mode-card selected' : 'mode-card'}>
              <input
                checked={mode === item}
                name="mode"
                type="radio"
                value={item}
                onChange={() => setMode(item)}
              />
              <strong>{MODE_LABELS[item].title}</strong>
              <span>{MODE_LABELS[item].hint}</span>
            </label>
          ))}
        </div>
      </section>
    </form>
  );
}
