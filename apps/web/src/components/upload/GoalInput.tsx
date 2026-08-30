import { useId } from 'react';

interface GoalInputProps {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}

export function GoalInput({ value, onChange, disabled = false }: GoalInputProps) {
  const textareaId = useId();
  return (
    <section className="card">
      <div className="section-heading">
        <label htmlFor={textareaId}>
          <h2>说明目标</h2>
        </label>
        <p>用平实的话说明：要处理哪些数据、期望得到什么结果、哪些记录需要人工确认。</p>
      </div>

      <textarea
        id={textareaId}
        className="goal-input"
        disabled={disabled}
        value={value}
        rows={7}
        placeholder="例如：清洗订单数据，补齐客户和商品的标准编码，输出可直接导入系统的结果，并单独列出不能导入的原因。"
        onChange={(event) => onChange(event.target.value)}
      />
    </section>
  );
}
