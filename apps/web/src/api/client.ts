import { API_BASE_URL, REQUEST_TIMEOUT_MS } from './config';

/** Error carrying the HTTP status so callers can distinguish 404 (not ready) etc. */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function buildRequestError(response: Response): Promise<ApiError> {
  const fallback = `请求失败：${response.status}`;

  try {
    const payload = (await response.json()) as { detail?: unknown };
    if (typeof payload.detail === 'string' && payload.detail.trim()) {
      return new ApiError(`${fallback}，${payload.detail}`, response.status);
    }
  } catch {
    // Non-JSON error responses keep the status-only message.
  }

  return new ApiError(fallback, response.status);
}

async function fetchWithTimeout(
  path: string,
  init: RequestInit,
  timeoutMs = REQUEST_TIMEOUT_MS
): Promise<Response> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(`${API_BASE_URL}${path}`, { ...init, signal: controller.signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiError('请求超时，请检查后端服务是否可用。', 0);
    }
    throw new ApiError('网络请求失败，请确认后端服务已启动。', 0);
  } finally {
    window.clearTimeout(timer);
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetchWithTimeout(path, { method: 'GET' });
  if (!response.ok) {
    throw await buildRequestError(response);
  }
  return response.json() as Promise<T>;
}

export async function apiPostForm<T>(path: string, formData: FormData): Promise<T> {
  // Uploads may take a while; no client-side timeout so large files can finish.
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: 'POST',
    body: formData
  }).catch(() => {
    throw new ApiError('网络请求失败，请确认后端服务已启动。', 0);
  });
  if (!response.ok) {
    throw await buildRequestError(response);
  }
  return response.json() as Promise<T>;
}

export async function apiPostJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetchWithTimeout(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  if (!response.ok) {
    throw await buildRequestError(response);
  }
  return response.json() as Promise<T>;
}
