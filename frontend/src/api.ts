import type { Job, Registry, Request, Result, Upload } from './types';
const root = '/api/analysis';
export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(root + path, init);
  if (!response.ok) {
    const body = await response.text();
    let message = body;
    try { const parsed = JSON.parse(body); message = typeof parsed.detail === 'string' ? parsed.detail : JSON.stringify(parsed.detail ?? parsed); } catch { /* Preserve non-JSON server errors. */ }
    throw new Error(`${response.status}: ${message || response.statusText}`);
  }
  return response.json() as Promise<T>;
}
export const api = {
  registry: () => request<Registry>('/registry'),
  create: (body: Request) => request<Job>('/jobs', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  job: (id: string, signal?: AbortSignal) => request<Job>(`/jobs/${encodeURIComponent(id)}`, { signal }),
  cancel: (id: string) => request<Job>(`/jobs/${encodeURIComponent(id)}/cancel`, {method:'POST'}),
  result: (id: string) => request<Result>(`/jobs/${encodeURIComponent(id)}/result`),
  upload: (file: File) => request<Upload>(`/uploads?filename=${encodeURIComponent(file.name)}`, {method:'POST',headers:{'Content-Type':'image/tiff'},body:file}),
  labels: (id: string) => request<unknown>(`/jobs/${encodeURIComponent(id)}/labels`),
  saveLabels: (id: string, labels: unknown) => request<unknown>(`/jobs/${encodeURIComponent(id)}/labels`, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(labels)}),
};
export function safeUrl(url: string): string | undefined {
  try { const parsed = new URL(url, location.origin); return parsed.origin === location.origin && parsed.pathname.startsWith('/api/analysis/') ? parsed.href : undefined; } catch { return undefined; }
}
export function datasetUrl(id: string) { return `${root}/jobs/${encodeURIComponent(id)}/dataset`; }
