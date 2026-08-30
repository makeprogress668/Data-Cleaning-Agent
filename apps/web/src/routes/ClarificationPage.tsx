import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import { EmptyState } from '../components/common/EmptyState';
import { ErrorState } from '../components/common/ErrorState';
import { LoadingState } from '../components/common/LoadingState';
import { useClarification } from '../hooks/useClarification';

export function ClarificationPage() {
  const { jobId } = useParams();
  const navigate = useNavigate();
  const { questions, goal, round, maxRounds, submit, isLoading, isSubmitting, error } =
    useClarification(jobId);
  const [answers, setAnswers] = useState<Record<string, string>>({});

  // Seed an empty answer per question once they load.
  useEffect(() => {
    setAnswers((current) => {
      const next = { ...current };
      questions.forEach((question) => {
        if (!(question.id in next)) {
          next[question.id] = '';
        }
      });
      return next;
    });
  }, [questions]);

  if (!jobId) {
    return (
      <div className="page-stack">
        <EmptyState
          title="暂无需要澄清的任务"
          description="当任务需要你确认关键信息时，会在这里列出问题。请先新建一个任务。"
          actionLabel="去新建任务"
          actionTo="/upload"
        />
      </div>
    );
  }

  async function resumeWith(nextAnswers: Record<string, string>) {
    const payload = questions.map((question) => ({
      question_id: question.id,
      answer: nextAnswers[question.id]?.trim() ?? ''
    }));
    const ok = await submit(payload);
    if (ok) {
      navigate(`/jobs/${jobId}`);
    }
  }

  // Single-select: one click records the choice and immediately resumes execution.
  async function handlePick(questionId: string, option: string) {
    const nextAnswers = { ...answers, [questionId]: option };
    setAnswers(nextAnswers);
    if (
      questions.length === 1
      && questions[0].id === questionId
      && questions[0].options?.length
    ) {
      await resumeWith(nextAnswers);
    }
  }

  const answeredCount = questions.filter(
    (question) => (answers[question.id]?.trim() ?? '') !== ''
  ).length;
  const hasFreeTextQuestion = questions.some(
    (question) => !question.options || question.options.length === 0
  );

  return (
    <div className="page-stack">
      {isLoading ? <LoadingState label="正在读取澄清问题" /> : null}
      {error ? <ErrorState message={`处理澄清问题时出错：${error}`} /> : null}

      <section className="page-intro">
        <div>
          <p className="eyebrow">执行前 · 需要你确认</p>
          <h2>补充一个关键信息</h2>
          <p>这个信息会影响处理结果，请选择或填写最符合实际情况的答案。</p>
          {goal ? <p className="muted">任务目标：{goal}</p> : null}
        </div>
        {questions.length > 0 ? (
          <span className="status-pill status-running">
            第 {round}/{maxRounds} 轮 · {answeredCount}/{questions.length} 已填写
          </span>
        ) : null}
      </section>

      {!isLoading && questions.length === 0 ? (
        <section className="card">
          <p>该任务当前没有待澄清的问题，可返回任务详情查看进度。</p>
          <div className="button-row">
            <button className="secondary-button" type="button" onClick={() => navigate(`/jobs/${jobId}`)}>
              返回任务详情
            </button>
          </div>
        </section>
      ) : null}

      {questions.length > 0 ? (
        <section className="card">
          <div className="section-heading">
            <h2>待确认问题</h2>
            <p>
              {hasFreeTextQuestion
                ? '请完成全部问题后提交。'
                : questions.length === 1
                  ? '选择答案后将继续处理。'
                  : '请完成全部选择后提交。'}
            </p>
          </div>
          <div className="clarification-list">
            {questions.map((question, index) => (
              <div className="clarification-item" key={question.id}>
                <label htmlFor={`answer-${question.id}`}>
                  <span className="clarification-index">{index + 1}.</span> {question.question}
                </label>
                {question.impact ? <p className="muted">影响：{question.impact}</p> : null}
                {question.options && question.options.length > 0 ? (
                  <div className="option-cards">
                    {question.options.map((option) => (
                      <button
                        type="button"
                        key={option}
                        className={`option-card${
                          answers[question.id] === option ? ' selected' : ''
                        }`}
                        disabled={isSubmitting}
                        onClick={() => handlePick(question.id, option)}
                      >
                        {option}
                      </button>
                    ))}
                  </div>
                ) : (
                  <textarea
                    id={`answer-${question.id}`}
                    className="clarification-input"
                    rows={2}
                    value={answers[question.id] ?? ''}
                    placeholder="请输入完成任务所需的信息"
                    onChange={(event) =>
                      setAnswers((current) => ({ ...current, [question.id]: event.target.value }))
                    }
                  />
                )}
              </div>
            ))}
          </div>
          <div className="button-row">
            {hasFreeTextQuestion || questions.length > 1 ? (
              <button
                className="primary-button"
                type="button"
                disabled={isSubmitting || answeredCount < questions.length}
                onClick={() => resumeWith(answers)}
              >
                {isSubmitting ? '正在继续…' : '提交答案并继续'}
              </button>
            ) : null}
            <button
              className="secondary-button"
              type="button"
              onClick={() => navigate(`/jobs/${jobId}`)}
            >
              返回任务详情
            </button>
          </div>
        </section>
      ) : null}
    </div>
  );
}
