import { useState } from 'react';
import type { DragEvent } from 'react';

import { friendlyFileTypes } from '../../api/labels';

interface FileUploaderProps {
  files: File[];
  onChange: (files: File[]) => void;
  allowedExtensions?: string[];
  maxUploadMb?: number;
}

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }
  return `${Math.ceil(bytes / 1024)} KB`;
}

function fileExtension(name: string): string {
  const dot = name.lastIndexOf('.');
  return dot >= 0 ? name.slice(dot).toLowerCase() : '';
}

export function FileUploader({
  files,
  onChange,
  allowedExtensions = [],
  maxUploadMb
}: FileUploaderProps) {
  const [isDragging, setIsDragging] = useState(false);
  const [rejected, setRejected] = useState<string[]>([]);

  const acceptAttr = allowedExtensions.length > 0 ? allowedExtensions.join(',') : undefined;
  const maxBytes = maxUploadMb ? maxUploadMb * 1024 * 1024 : undefined;

  function validateAndAdd(incoming: File[]) {
    const problems: string[] = [];
    const accepted: File[] = [];

    for (const file of incoming) {
      const ext = fileExtension(file.name);
      if (allowedExtensions.length > 0 && !allowedExtensions.includes(ext)) {
        problems.push(`${file.name}：不支持的文件类型（${ext || '无扩展名'}）`);
        continue;
      }
      if (maxBytes && file.size > maxBytes) {
        problems.push(`${file.name}：超过 ${maxUploadMb} MB 上限`);
        continue;
      }
      accepted.push(file);
    }

    setRejected(problems);
    if (accepted.length === 0) {
      return;
    }
    onChange([...files, ...accepted]);
  }

  function handleDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setIsDragging(false);
    validateAndAdd(Array.from(event.dataTransfer.files ?? []));
  }

  function removeFile(targetIndex: number) {
    onChange(files.filter((_file, index) => index !== targetIndex));
  }

  return (
    <section className="card">
      <div className="section-heading">
        <h2>上传数据</h2>
        <p>
          支持 {allowedExtensions.length > 0 ? friendlyFileTypes(allowedExtensions) : 'Excel、CSV、JSON 和文本说明'}
          {maxUploadMb ? ` 等数据表格和说明文档，单个文件不超过 ${maxUploadMb} MB。` : ' 等数据表格和说明文档。'}
        </p>
      </div>

      <label
        className={isDragging ? 'upload-zone dragging' : 'upload-zone'}
        onDragOver={(event) => {
          event.preventDefault();
          setIsDragging(true);
        }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={handleDrop}
      >
        <input
          className="upload-input"
          multiple
          type="file"
          aria-label="选择数据文件或规则文档"
          accept={acceptAttr}
          onChange={(event) => {
            validateAndAdd(Array.from(event.target.files ?? []));
            // Reset so re-selecting the same file still fires onChange.
            event.target.value = '';
          }}
        />
        <span className="upload-zone-icon" aria-hidden="true">
          ⬆
        </span>
        <span>点击选择，或将文件拖到此处</span>
        <small>可一次上传多张表格和配套的规则文档</small>
      </label>

      {rejected.length > 0 ? (
        <ul className="upload-errors" role="alert">
          {rejected.map((message) => (
            <li key={message}>{message}</li>
          ))}
        </ul>
      ) : null}

      {files.length > 0 ? (
        <ul className="file-list">
          {files.map((file, index) => (
            <li key={`${file.name}-${file.size}-${file.lastModified}-${index}`}>
              <span className="file-name">{file.name}</span>
              <small>{formatSize(file.size)}</small>
              <button
                className="file-remove"
                type="button"
                aria-label={`移除 ${file.name}`}
                onClick={() => removeFile(index)}
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
