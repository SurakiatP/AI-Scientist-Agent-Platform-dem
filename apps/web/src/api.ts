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

export async function requestBlob(path: string, signal?: AbortSignal): Promise<{ blob: Blob; contentType: string }> {
  const response = await fetch(safePath(path), { credentials: 'same-origin', redirect: 'error', headers: { Accept: 'application/pdf, text/plain, text/markdown, text/csv' }, signal });
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    throw toApiError(response.status, body);
  }
  return { blob: await response.blob(), contentType: response.headers.get('content-type')?.split(';')[0] ?? 'application/octet-stream' };
}

export function resetApiSessionForTests() {
  csrfToken = null;
  csrfFlight = null;
}
