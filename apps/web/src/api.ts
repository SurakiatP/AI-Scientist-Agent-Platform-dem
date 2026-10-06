import type { PlanView, PreparationJobView, RunReadinessView, RunView } from '../../../contracts/api-types';

export type ResearchWorkflow = 'literature' | 'resources';
export const preparePlan = (runId: string, expectedRevision: number, searchTerms: string[], workflow: ResearchWorkflow = 'literature') => request<RunView>(`/api/v1/runs/${encodeURIComponent(runId)}/prepare-plan`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: expectedRevision, workflow, search_terms: searchTerms }) });
export const getPreparation = (projectId: string, jobId: string, signal?: AbortSignal) => request<PreparationJobView>(`/api/v1/projects/${encodeURIComponent(projectId)}/preparations/${encodeURIComponent(jobId)}`, { signal });

const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
export const validUuid = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
const digest = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
const integer = (value: unknown, minimum = 0): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum;
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every((item) => typeof item === 'string');
const optionalString = (value: unknown) => value === undefined || value === null || typeof value === 'string';

export function strictPlanView(value: unknown, currentRun: RunView): value is PlanView {
  if (!record(value) || !validUuid(value.run_id) || value.run_id !== currentRun.run_id || !integer(value.revision, 1) || !integer(currentRun.revision, 1) || value.revision !== currentRun.revision || !digest(value.plan_digest) || !record(value.plan)) return false;
  const plan = value.plan;
  if (!digest(plan.input_snapshot_digest) || !validUuid(plan.provider_id) || typeof plan.model !== 'string' || !plan.model || !strings(plan.stages) || !strings(plan.allowed_ops) || !strings(plan.data_recipients) || !integer(plan.token_limit) || !integer(plan.elapsed_limit_ms) || !Array.isArray(plan.packages) || !plan.packages.every((item) => record(item) && typeof item.name === 'string' && typeof item.version === 'string' && typeof item.source === 'string' && digest(item.sha256)) || (plan.peer_releases !== undefined && (!Array.isArray(plan.peer_releases) || !plan.peer_releases.every(record)))) return false;
  if (plan.scientific === undefined || plan.scientific === null) return true;
  const binding = plan.scientific;
  return record(binding) && binding.catalog_commit === '154988403bb5a18e9d3c0ce4e6d5e2e4b184a298' && digest(binding.registry_sha256) && strings(binding.capability_ids) && binding.capability_ids.length > 0 && digest(binding.instruction_fingerprint) && typeof binding.profile_id === 'string' && binding.profile_id.length > 0 && (binding.profile_version === undefined || binding.profile_version === '1') && (binding.tool_version === undefined || binding.tool_version === '1') && typeof binding.image_digest === 'string' && /^sha256:[0-9a-f]{64}$/.test(binding.image_digest) && binding.input_snapshot_digest === plan.input_snapshot_digest && (binding.parameters === undefined || record(binding.parameters)) && integer(binding.max_result_bytes, 1) && integer(binding.timeout_ms, 1) && integer(binding.memory_limit_bytes, 1) && integer(binding.workspace_limit_bytes, 1);
}

export function strictReadiness(value: unknown, currentRun: RunView): value is RunReadinessView {
  return record(value) && validUuid(value.run_id) && value.run_id === currentRun.run_id && integer(value.revision, 1) && integer(currentRun.revision, 1) && value.revision === currentRun.revision && digest(value.plan_digest) && (value.binding_sha256 === undefined || value.binding_sha256 === null || digest(value.binding_sha256)) && typeof value.state === 'string' && ['ready', 'missing', 'preparing', 'blocked', 'failed'].includes(value.state) && Array.isArray(value.requirements) && value.requirements.every((item) => record(item) && typeof item.id === 'string' && typeof item.label === 'string' && typeof item.purpose === 'string' && typeof item.state === 'string' && ['ready', 'missing', 'preparing', 'blocked', 'failed'].includes(item.state) && typeof item.action === 'string' && ['none', 'configure_connection', 'prepare_environment', 'request_approval', 'provide_hardware'].includes(item.action) && optionalString(item.reason));
}

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly requestId: string;

  constructor(code: string, status: number, requestId: string, message?: string) {
    super(message || 'The request could not be completed.');
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.requestId = requestId;
  }
}

const apiMessages: Record<string, [string, string]> = {
  forbidden: ['This request is not authorized.', 'คำขอนี้ไม่ได้รับอนุญาต'],
  not_found: ['The requested item was not found.', 'ไม่พบรายการที่ร้องขอ'],
  revision_conflict: ['This item changed. Reload and try again.', 'รายการนี้มีการเปลี่ยนแปลง โปรดโหลดใหม่แล้วลองอีกครั้ง'],
  idempotency_conflict: ['This request key was already used with different content.', 'มีการใช้รหัสคำขอนี้กับข้อมูลอื่นแล้ว'],
  approval_required: ['Owner approval is required.', 'ต้องได้รับการอนุมัติจากเจ้าของก่อน'],
  cursor_expired: ['The event cursor or page size is invalid.', 'ตำแหน่งเหตุการณ์หรือขนาดหน้าไม่ถูกต้อง'],
  budget_exhausted: ['The approved usage limit has been reached.', 'ถึงขีดจำกัดการใช้งานที่อนุมัติแล้ว'],
  storage_unavailable: ['Secure storage is unavailable.', 'พื้นที่จัดเก็บที่ปลอดภัยไม่พร้อมใช้งาน'],
  request_too_large: ['The request exceeds the size limit.', 'คำขอมีขนาดเกินกำหนด'],
  unsupported_file_type: ['This file type is not supported.', 'ไม่รองรับชนิดไฟล์นี้'],
  file_type_mismatch: ['The file content does not match its type.', 'เนื้อหาไฟล์ไม่ตรงกับชนิดไฟล์'],
  invalid_file: ['This file cannot be accepted.', 'ไม่สามารถรับไฟล์นี้ได้'],
  preparation_failed: ['File preparation failed.', 'เตรียมไฟล์ไม่สำเร็จ'],
  invalid_response: ['The service returned an invalid response.', 'บริการส่งข้อมูลตอบกลับที่ไม่ถูกต้อง'],
  request_failed: ['The request could not be completed.', 'ดำเนินการตามคำขอไม่สำเร็จ'],
};

export function apiCodeLabel(code: string, language: 'th' | 'en'): string {
  const message = apiMessages[code];
  return message ? message[language === 'th' ? 1 : 0] : language === 'th' ? 'เกิดข้อผิดพลาดในการดำเนินการ' : 'The request could not be completed.';
}

export function apiErrorMessage(error: unknown, language: 'th' | 'en', fallback?: string): string {
  if (error instanceof ApiError) return apiCodeLabel(error.code, language);
  if (language === 'th') return 'เชื่อมต่อบริการไม่สำเร็จ โปรดลองอีกครั้ง';
  return error instanceof Error ? error.message : fallback ?? 'The request could not be completed.';
}

let csrfToken: string | null = null;
let csrfFlight: Promise<string> | null = null;

function safePath(path: string): string {
  if (!path.startsWith('/api/v1/') || path.startsWith('//') || path.includes('\\') || /[\u0000-\u0020]/.test(path)) {
    throw new TypeError('API requests must use a relative /api/v1 path.');
  }
  const url = new URL(path, window.location.origin);
  if (url.origin !== window.location.origin || !url.pathname.startsWith('/api/v1/') || url.pathname.split('/').includes('..')) {
    throw new TypeError('API requests must remain on this origin under /api/v1.');
  }
  return `${url.pathname}${url.search}`;
}

async function getCsrf(): Promise<string> {
  if (csrfToken) return csrfToken;
  if (!csrfFlight) {
    csrfFlight = fetch('/api/v1/owner/session', { credentials: 'same-origin', redirect: 'error', headers: { Accept: 'application/json' } })
      .then(async (response) => {
        const body: unknown = await response.json().catch(() => null);
        if (!response.ok) throw toApiError(response.status, body);
        const token = (body as { csrf_token?: unknown } | null)?.csrf_token;
        if (typeof token !== 'string' || !token) throw new ApiError('invalid_response', 502, '');
        csrfToken = token;
        return token;
      })
      .finally(() => { csrfFlight = null; });
  }
  return csrfFlight;
}

export async function refreshOwnerSession(): Promise<void> {
  csrfToken = null;
  await getCsrf();
}

function toApiError(status: number, body: unknown): ApiError {
  const payload = body && typeof body === 'object' ? body as Record<string, unknown> : {};
  return new ApiError(
    typeof payload.code === 'string' ? payload.code : 'request_failed', status,
    typeof payload.request_id === 'string' ? payload.request_id : '',
    typeof payload.message === 'string' ? payload.message : 'The request could not be completed.',
  );
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const safe = safePath(path);
  const method = (init.method ?? 'GET').toUpperCase();
  const headers = new Headers(init.headers);
  headers.set('Accept', headers.get('Accept') ?? 'application/json');
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) headers.set('X-CSRF-Token', await getCsrf());
  const response = await fetch(safe, { ...init, method, headers, credentials: 'same-origin', redirect: 'error' });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 403 && !['GET', 'HEAD', 'OPTIONS'].includes(method)) csrfToken = null;
    throw toApiError(response.status, body);
  }
  return body as T;
}

export async function requestBlob(path: string, signal?: AbortSignal, maxBytes = 8 * 1024 * 1024): Promise<{ blob: Blob; contentType: string }> {
  if (!Number.isSafeInteger(maxBytes) || maxBytes < 1) throw new TypeError('Invalid content limit.');
  const response = await fetch(safePath(path), { credentials: 'same-origin', redirect: 'error', headers: { Accept: 'application/pdf, text/plain, text/markdown, text/csv, application/json, image/png, image/jpeg, image/webp, image/gif' }, signal });
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    throw toApiError(response.status, body);
  }
  const contentType = response.headers.get('content-type')?.split(';')[0].trim().toLowerCase() ?? 'application/octet-stream';
  const declared = response.headers.get('content-length');
  if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > maxBytes)) {
    await response.body?.cancel();
    throw new ApiError('request_too_large', 413, '');
  }
  if (!response.body) return { blob: new Blob([], { type: contentType }), contentType };
  const reader = response.body.getReader();
  const chunks: Uint8Array<ArrayBuffer>[] = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maxBytes) {
        await reader.cancel();
        throw new ApiError('request_too_large', 413, '');
      }
      chunks.push(new Uint8Array(value));
    }
  } finally { reader.releaseLock(); }
  return { blob: new Blob(chunks, { type: contentType }), contentType };
}

export function newRequestId(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function resetApiSessionForTests() {
  csrfToken = null;
  csrfFlight = null;
}
