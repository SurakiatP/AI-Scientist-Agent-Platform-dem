import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import type { ArtifactView, ConnectionView, FileView, PendingDecisionView, PlanView, RunReadinessView, RunView, ScientificBindingV2 } from '../../../contracts/api-types';
import { ApiError, apiErrorMessage, preparePlan, request, requestBlob, strictPlanView, strictReadiness, validUuid, type CsvSelection, type ResearchWorkflow } from './api';
import { useAppPreferences } from './App';
import { ArtifactCard, ArtifactViewer } from './ArtifactViewer';
import type { DecisionRequiredPayload } from '../../../contracts/api-types';
import { RunProgress, type DecisionChoice } from './RunProgress';
import { useRunEvents } from './useRunEvents';
import { PeerReleaseReview } from './PeerReleaseReview';
import './research.css';

const text = (language: 'th' | 'en', en: string, th: string) => language === 'th' ? th : en;
type MessageView = { id: string; sequence: number; role: string; content: string; created_at?: string };
const fileStateLabels: Record<FileView['state'], [string, string]> = { uploading: ['Uploading', 'กำลังอัปโหลด'], preparing: ['Preparing', 'กำลังเตรียมไฟล์'], ready: ['Ready', 'พร้อมใช้งาน'], failed: ['Failed', 'ไม่สำเร็จ'] };
const fileStateLabel = (state: FileView['state'], language: 'th' | 'en') => fileStateLabels[state][language === 'th' ? 1 : 0];
function newKey(): string { // randomUUID needs a secure context; getRandomValues does not
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16)); b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
  const h = [...b].map((x) => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}
type CrossrefMode = 'query' | 'doi';
function readDraft(key: string): { question: string; selected: string[]; workflow: ResearchWorkflow; crossrefMode: CrossrefMode; crossrefTerm: string; csvFileId: string; csvColumnsText: string } {
  try {
    const v = JSON.parse(sessionStorage.getItem(key) ?? 'null') as { question?: unknown; selected?: unknown; workflow?: unknown; crossrefMode?: unknown; crossrefTerm?: unknown; csvFileId?: unknown; csvColumnsText?: unknown } | null;
    return {
      question: typeof v?.question === 'string' ? v.question : '',
      selected: Array.isArray(v?.selected) ? v.selected.filter((x): x is string => typeof x === 'string') : [],
      workflow: v?.workflow === 'resources' || v?.workflow === 'crossref_csv' ? v.workflow : 'literature',
      crossrefMode: v?.crossrefMode === 'doi' ? 'doi' : 'query',
      crossrefTerm: typeof v?.crossrefTerm === 'string' ? v.crossrefTerm : '',
      csvFileId: typeof v?.csvFileId === 'string' ? v.csvFileId : '',
      csvColumnsText: typeof v?.csvColumnsText === 'string' ? v.csvColumnsText : '',
    };
  } catch { return { question: '', selected: [], workflow: 'literature', crossrefMode: 'query', crossrefTerm: '', csvFileId: '', csvColumnsText: '' }; }
}
const parseCsvColumns = (value: string) => value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean);
const normalizedDoi = (value: string) => value.trim().toLowerCase().replace(/^(https?:\/\/(dx\.)?doi\.org\/|doi:)/, '').trim();
const validDoi = (value: string) => /^10\.[0-9]{4,9}\//.test(value) && !/[\s<>"']/.test(value);
const TERMINAL = ['completed', 'failed', 'canceled', 'rejected'];
const json = (body: unknown): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

type DecisionBody = { decision_id: string; expected_revision: number; idempotency_key: string; choice: DecisionChoice; add_tokens?: number; add_elapsed_ms?: number; usage_tokens?: number };
const submitDecision = (runId: string, body: DecisionBody) => request<RunView>(`/api/v1/runs/${runId}/decisions`, json(body));

function ChatSession({ pollMs }: { pollMs: number }) {
  const { projectId = '', sessionId = '' } = useParams();
  const { language } = useAppPreferences();
  const location = useLocation();
  const navigate = useNavigate();
  const requestedRun = new URLSearchParams(location.search).get('run');
  const base = `/api/v1/projects/${encodeURIComponent(projectId)}`;
  const draftKey = `research-draft:${sessionId}`; // question text and file ids only; never secrets
  const [messages, setMessages] = useState<MessageView[]>([]);
  const [files, setFiles] = useState<FileView[]>([]);
  const [connections, setConnections] = useState<ConnectionView[] | null>(null);
  const [connectionsLoading, setConnectionsLoading] = useState(true);
  const [question, setQuestion] = useState(() => readDraft(draftKey).question);
  const [selected, setSelected] = useState<string[]>(() => readDraft(draftKey).selected);
  const [workflow, setWorkflow] = useState<ResearchWorkflow>(() => readDraft(draftKey).workflow);
  const [crossrefMode, setCrossrefMode] = useState<CrossrefMode>(() => readDraft(draftKey).crossrefMode);
  const [crossrefTerm, setCrossrefTerm] = useState(() => readDraft(draftKey).crossrefTerm);
  const [csvFileId, setCsvFileId] = useState(() => readDraft(draftKey).csvFileId);
  const [csvColumnsText, setCsvColumnsText] = useState(() => readDraft(draftKey).csvColumnsText);
  const [searchTerm, setSearchTerm] = useState('');
  const [preparingPlan, setPreparingPlan] = useState(false);
  const preparationFlight = useRef(false);
  const [activeRun, setActiveRun] = useState<RunView | null>(null);
  const { run, setRun, events, connected } = useRunEvents(activeRun?.run_id ?? null, { retryBaseDelayMs: pollMs });
  const [pendingDecisions, setPendingDecisions] = useState<PendingDecisionView[]>([]);
  const [archived, setArchived] = useState<RunView[]>([]);
  const [plan, setPlan] = useState<PlanView | null>(null);
  const [planError, setPlanError] = useState<unknown>(null);
  const [runSelectionError, setRunSelectionError] = useState(false);
  const [readiness, setReadiness] = useState<RunReadinessView | null>(null);
  const [readinessError, setReadinessError] = useState<unknown>(null);
  const [approving, setApproving] = useState(false);
  const planFlight = useRef<AbortController | null>(null);
  const planSequence = useRef(0);
  const latestRunId = useRef<string | null>(null);
  latestRunId.current = run?.run_id ?? null;
  const latestRun = useRef<RunView | null>(null);
  latestRun.current = run;
  const approvalFlight = useRef(false);
  const [peerReview, setPeerReview] = useState({ key: '', ready: false });
  const peerReviewReady = useCallback((key: string, ready: boolean) => setPeerReview({ key, ready }), []);
  const [stagesText, setStagesText] = useState('');
  const [tokenLimitText, setTokenLimitText] = useState('');
  const [timeLimitText, setTimeLimitText] = useState('');
  const [planNote, setPlanNote] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [stopPending, setStopPending] = useState(false);
  const [submissions, setSubmissions] = useState<Record<string, { question: string; selected: string[] }>>({});
  const [retryOf, setRetryOf] = useState<string | null>(null);
  const expanded = new URLSearchParams(location.search).get('output');
  const expandedByClick = useRef(false);
  const attempt = useRef<{ key: string; signature: string } | null>(null);
  const opener = useRef<HTMLElement | null>(null);

  useEffect(() => { sessionStorage.setItem(draftKey, JSON.stringify({ question, selected, workflow, crossrefMode, crossrefTerm, csvFileId, csvColumnsText })); }, [draftKey, question, selected, workflow, crossrefMode, crossrefTerm, csvFileId, csvColumnsText]);
  useEffect(() => {
    const controller = new AbortController();
    const signal = controller.signal;
    // Independent loads: a missing connections route must not hide messages or files.
    void request<MessageView[]>(`/api/v1/sessions/${sessionId}/messages`, { signal }).then((m) => { if (!signal.aborted) setMessages(m); }).catch((reason: unknown) => { if (!signal.aborted) setError(apiErrorMessage(reason, language, 'Unable to load this conversation.')); });
    void request<FileView[]>(`${base}/files`, { signal }).then((f) => { if (!signal.aborted) setFiles(f); }).catch((reason: unknown) => { if (!signal.aborted) setError(apiErrorMessage(reason, language, 'Unable to load project files.')); });
    // TODO(contract): no connections route exists yet; failure leaves connections null ("Settings unavailable").
    void request<ConnectionView[]>('/api/v1/connections', { signal }).then((c) => {
      if (!signal.aborted) { setConnections(c); setConnectionsLoading(false); }
    }).catch(() => {
      if (!signal.aborted) { setConnections(null); setConnectionsLoading(false); }
    });
    return () => controller.abort();
  }, [base, sessionId]);
  useEffect(() => { // an explicit return target must not select another run
    const controller = new AbortController();
    setRunSelectionError(false);
    if (requestedRun !== null && activeRun?.run_id !== requestedRun) { setActiveRun(null); setRun(null); setPlan(null); setReadiness(null); }
    if (requestedRun !== null && !validUuid(requestedRun)) {
      setRunSelectionError(true); return () => controller.abort();
    }
    request<RunView[]>(`${base}/runs`, { signal: controller.signal }).then((all) => {
      if (!controller.signal.aborted) {
        const scoped = all.filter((r) => r.project_id === projectId && r.session_id === sessionId && validUuid(r.run_id));
        if (requestedRun !== null) {
          const selectedRun = scoped.find((item) => item.run_id === requestedRun) ?? null;
          setRunSelectionError(selectedRun === null); setActiveRun(selectedRun);
        } else setActiveRun((current) => current ?? scoped.at(-1) ?? null);
      }
    }).catch(() => { if (!controller.signal.aborted && requestedRun !== null) setRunSelectionError(true); });
    return () => controller.abort();
  }, [base, projectId, sessionId, requestedRun]);
  useEffect(() => { // refresh file readiness while any file is still being prepared
    if (!files.some((f) => f.state === 'preparing' || f.state === 'uploading')) return;
    const timer = setTimeout(() => { request<FileView[]>(`${base}/files`).then(setFiles).catch(() => undefined); }, pollMs);
    return () => clearTimeout(timer);
  }, [files, base, pollMs]);
  useEffect(() => { // plan review: owner-only plan read, bound to its shown revision/digest
    if (!run || run.state !== 'awaiting_approval') { planFlight.current?.abort(); setPlan(null); setReadiness(null); return; }
    loadPlan(run.run_id);
  }, [run?.run_id, run?.state, run?.revision]);
  useEffect(() => () => { planFlight.current?.abort(); planSequence.current += 1; }, []);
  useEffect(() => {
    let current = true;
    if (!run) {
      setPendingDecisions([]);
      return () => { current = false; };
    }
    setPendingDecisions([]);
    request<PendingDecisionView[]>(`/api/v1/runs/${run.run_id}/pending-decisions`)
      .then((decisions) => { if (current) setPendingDecisions(decisions); })
      .catch(() => { if (current) setPendingDecisions([]); });
    return () => { current = false; };
  }, [run?.run_id, run?.state, run?.revision, run?.latest_cursor]);
  useEffect(() => { if (!expanded) opener.current?.focus(); }, [expanded]);
  useEffect(() => { if (run && (TERMINAL.includes(run.state) || run.state === 'stopping')) setStopPending(false); }, [run?.state]);

  function readinessMatches(view: RunReadinessView, reviewed: PlanView, currentRun = latestRun.current) {
    return currentRun !== null && strictPlanView(reviewed, currentRun) && strictReadiness(view, currentRun) && view.run_id === reviewed.run_id && view.revision === reviewed.revision && view.plan_digest === reviewed.plan_digest
      && view.state === 'ready' && Array.isArray(view.requirements) && view.requirements.every((item) => item.state === 'ready')
      && (!reviewed.plan.scientific || (typeof view.binding_sha256 === 'string' && /^[a-f0-9]{64}$/.test(view.binding_sha256)));
  }
  async function loadPlan(runId: string, refreshRun = false): Promise<{ plan: PlanView; readiness: RunReadinessView | null; currentRun: RunView } | null> {
    planFlight.current?.abort();
    const controller = new AbortController();
    planFlight.current = controller;
    const sequence = ++planSequence.current;
    setReadiness(null); setReadinessError(null); setPlanError(null);
    const [planReply, readinessReply, runReply] = await Promise.allSettled([
      request<PlanView>(`/api/v1/runs/${runId}/plan`, { signal: controller.signal }),
      request<RunReadinessView>(`/api/v1/runs/${runId}/readiness`, { signal: controller.signal }),
      refreshRun ? request<RunView>(`/api/v1/runs/${runId}`, { signal: controller.signal }) : Promise.resolve(latestRun.current),
    ]);
    if (controller.signal.aborted || sequence !== planSequence.current || latestRunId.current !== runId) return null;
    if (planReply.status === 'rejected') { setPlan(null); setPlanError(planReply.reason); return null; }
    if (runReply.status === 'rejected') { setPlan(null); setPlanError(runReply.reason); return null; }
    const reviewed = planReply.value;
    const currentRun = refreshRun ? runReply.value : latestRun.current;
    if (!currentRun || currentRun.run_id !== runId || !validUuid(currentRun.run_id) || currentRun.project_id !== projectId || currentRun.session_id !== sessionId || currentRun.state !== 'awaiting_approval' || (latestRun.current !== null && currentRun.revision < latestRun.current.revision) || !strictPlanView(reviewed, currentRun)) { setPlan(null); setPlanError(new ApiError('invalid_response', 502, '')); return null; }
    if (refreshRun) setRun(currentRun);
    setPlan(reviewed); setStagesText(reviewed.plan.stages.join('\n'));
    setTokenLimitText(String(reviewed.plan.token_limit)); setTimeLimitText(String(reviewed.plan.elapsed_limit_ms / 1000));
    const readyView = readinessReply.status === 'fulfilled' && strictReadiness(readinessReply.value, currentRun) ? readinessReply.value : null;
    if (readinessReply.status === 'rejected') setReadinessError(readinessReply.reason);
    else if (readyView === null) setReadinessError(new ApiError('invalid_response', 502, ''));
    setReadiness(readyView);
    return { plan: reviewed, readiness: readyView, currentRun };
  }

  const ready = (id: string) => files.find((f) => f.id === id)?.state === 'ready';
  const hasModel = connections?.some((c) => c.state === 'ready') ?? false;
  const csvFile = files.find((file) => file.id === csvFileId);
  const v2Binding = plan?.plan.scientific && 'binding_version' in plan.plan.scientific && plan.plan.scientific.binding_version === 2
    ? plan.plan.scientific as ScientificBindingV2
    : null;
  const savedCrossref = v2Binding?.approved_crossref_queries?.crossref;
  const savedCsvGrant = v2Binding?.csv_describe_grants?.csv_describe;
  const csvColumns = useMemo(() => parseCsvColumns(csvColumnsText), [csvColumnsText]);
  const validCsvColumns = csvColumns.length > 0 && csvColumns.length <= 8 && new Set(csvColumns).size === csvColumns.length && csvColumns.every((column) => column.length <= 128 && !/[\u0000-\u001f\u007f]/.test(column));
  const validCrossrefTerm = crossrefMode === 'query'
    ? crossrefTerm.trim().length > 0 && crossrefTerm.trim().length <= 512 && !/[\u0000-\u001f\u007f]/.test(crossrefTerm)
    : validDoi(normalizedDoi(crossrefTerm));
  const csvSelection: CsvSelection | undefined = workflow === 'crossref_csv' && csvFile && csvFile.state === 'ready' && csvFile.content_type.split(';')[0].trim().toLowerCase() === 'text/csv' && validCrossrefTerm && validCsvColumns
    ? { crossref: { source_id: 'crossref', version: 1, access_mode: 'public_read', query: crossrefMode === 'query' ? crossrefTerm.trim() : null, doi: crossrefMode === 'doi' ? normalizedDoi(crossrefTerm) : null, limit: 10 }, csv_file_id: csvFile.id, numeric_columns: csvColumns }
      : undefined;
  const selectedInputIds = workflow === 'crossref_csv' ? (csvFileId ? [csvFileId] : []) : selected;
  const [savedInputMatches, setSavedInputMatches] = useState(false);
  useEffect(() => {
    if (!v2Binding || !savedCsvGrant || workflow !== 'crossref_csv' || !csvFile || csvFile.state !== 'ready' || savedCsvGrant.input_ref.project_id !== projectId) {
      setSavedInputMatches(false);
      return;
    }
    const controller = new AbortController();
    setSavedInputMatches(false);
    void requestBlob(`${base}/files/${encodeURIComponent(csvFile.id)}/content`, controller.signal, 1_048_576).then(async ({ blob }) => {
      const digestBytes = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer());
      const digestHex = [...new Uint8Array(digestBytes)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
      if (!controller.signal.aborted) setSavedInputMatches(blob.size === savedCsvGrant.input_ref.size && digestHex === savedCsvGrant.input_sha256);
    }).catch(() => { if (!controller.signal.aborted) setSavedInputMatches(false); });
    return () => controller.abort();
  }, [base, csvFile?.id, csvFile?.state, projectId, savedCsvGrant?.input_ref.project_id, savedCsvGrant?.input_ref.size, savedCsvGrant?.input_sha256, workflow, v2Binding]);
  const csvDraftMatchesSaved = !v2Binding || Boolean(
    workflow === 'crossref_csv' && csvSelection && savedCrossref && savedCsvGrant &&
    csvSelection.crossref.source_id === savedCrossref.source_id && csvSelection.crossref.version === savedCrossref.version &&
    csvSelection.crossref.access_mode === savedCrossref.access_mode && csvSelection.crossref.query === savedCrossref.query &&
    csvSelection.crossref.doi === savedCrossref.doi && csvSelection.crossref.limit === savedCrossref.limit &&
    csvSelection.numeric_columns.length === savedCsvGrant.numeric_columns.length &&
    csvSelection.numeric_columns.every((column, index) => column === savedCsvGrant.numeric_columns[index]) && savedInputMatches,
  );
  const csvDraftDiffers = Boolean(v2Binding && !csvDraftMatchesSaved);
  const signature = useMemo(() => JSON.stringify([question.trim(), selectedInputIds, workflow, crossrefMode, crossrefTerm.trim(), csvFileId, csvColumns]), [question, selectedInputIds, workflow, crossrefMode, crossrefTerm, csvFileId, csvColumns]);

  async function submit() {
    setError('');
    if (!question.trim()) { setError(text(language, 'Enter a research question first.', 'กรุณาพิมพ์คำถามวิจัยก่อน')); return; }
    if (!hasModel) { setError(text(language, 'No model is ready. Open Settings to choose and check one.', 'ยังไม่มีโมเดลที่พร้อมใช้ เปิดการตั้งค่าเพื่อเลือกและตรวจสอบ')); return; }
    if (workflow === 'crossref_csv' && !csvSelection) { setError(text(language, 'Choose a ready CSV, enter one Crossref query or DOI, and name up to eight numeric columns.', 'เลือกไฟล์ CSV ที่พร้อม ป้อนคำค้นหรือ DOI ของ Crossref หนึ่งรายการ และระบุคอลัมน์ตัวเลขไม่เกินแปดคอลัมน์')); return; }
    if (selectedInputIds.some((id) => !ready(id))) { setError(text(language, 'Wait for selected files to finish preparing or remove them.', 'รอให้ไฟล์ที่เลือกเตรียมเสร็จ หรือนำออก')); return; }
    if (!attempt.current || attempt.current.signature !== signature) attempt.current = { key: newKey(), signature };
    setSubmitting(true);
    try {
      const connection = connections!.find((c) => c.state === 'ready')!;
      // retry_of is NOT sent: the server rejects unknown fields (422). TODO(contract): link a retry to its predecessor once the API allows it.
      const created = await request<RunView>(`/api/v1/sessions/${sessionId}/runs`, json({ submission_key: attempt.current.key, question: question.trim(), input_ids: selectedInputIds, provider_id: connection.id, model: connection.model }));
      attempt.current = null; setRetryOf(null);
      setSubmissions((old) => ({ ...old, [created.run_id]: { question: question.trim(), selected: selectedInputIds } }));
      setMessages((old) => [...old, { id: `local-${created.run_id}`, sequence: old.length + 1, role: 'owner', content: question.trim() }]);
      setSearchTerm(question.trim().slice(0, 150));
      setQuestion(''); setSelected([]); setActiveRun(created); setRun(created);
      if (requestedRun !== null) { const search = new URLSearchParams(location.search); search.set('run', created.run_id); navigate({ search: search.toString() }, { replace: true }); }
    } catch (reason) {
      const definite = reason instanceof ApiError && reason.status < 500;
      if (definite) attempt.current = null;
      setError(definite ? apiErrorMessage(reason, language) : text(language, 'We could not confirm whether this request arrived. Submit again to reconcile; the same request key is reused so no duplicate run is created.', 'ไม่สามารถยืนยันได้ว่าคำขอถึงบริการหรือไม่ ส่งอีกครั้งเพื่อตรวจสอบ ระบบใช้รหัสคำขอเดิมจึงไม่สร้างงานซ้ำ'));
    } finally { setSubmitting(false); }
  }

  async function guarded<T>(action: () => Promise<T>): Promise<T | undefined> {
    try { return await action(); } catch (reason) { setError(apiErrorMessage(reason, language)); return undefined; }
  }
  const stop = async () => { if (!run) return; setStopPending(true); const next = await guarded(() => request<RunView>(`/api/v1/runs/${run.run_id}/stop`, json({}))); if (next) setRun(next); else setStopPending(false); };
  const decisionKeys = useRef(new Map<string, string>()); // one idempotency key per decision payload; reused on retry and double-click
  const deciding = useRef(false);
  async function refreshPendingDecisions(runId: string) {
    try {
      const current = await request<PendingDecisionView[]>(`/api/v1/runs/${runId}/pending-decisions`);
      setPendingDecisions(current);
    } catch {
      setPendingDecisions([]);
    }
  }
  const decide = async (decision: DecisionRequiredPayload, choice: DecisionChoice, usageTokens?: number) => {
    if (!run || deciding.current) return;
    const body: Omit<DecisionBody, 'idempotency_key'> = { decision_id: decision.decision_id, expected_revision: run.revision, choice };
    if (choice === 'extend') {
      if ((decision.required_tokens ?? 0) > 0) body.add_tokens = decision.required_tokens!;
      if ((decision.required_elapsed_ms ?? 0) > 0) body.add_elapsed_ms = decision.required_elapsed_ms!;
    }
    if (choice === 'confirm_usage') body.usage_tokens = usageTokens;
    const signature = JSON.stringify(body);
    const idempotency_key = decisionKeys.current.get(signature) ?? newKey();
    decisionKeys.current.set(signature, idempotency_key);
    deciding.current = true;
    try {
      const next = await submitDecision(run.run_id, { ...body, idempotency_key });
      setRun(next);
    } catch (reason) {
      setError(apiErrorMessage(reason, language));
      // A conflict or lost response can make the event-derived form stale. Re-read both authorities;
      // only another owner action may submit again, using the current decision and revision.
      const latest = await request<RunView>(`/api/v1/runs/${run.run_id}`).catch(() => undefined);
      if (latest) setRun(latest);
    } finally {
      await refreshPendingDecisions(run.run_id);
      deciding.current = false;
    }
  };
  const tokenLimit = Number(tokenLimitText);
  const timeLimitMs = Number(timeLimitText) * 1000;
  const validBudgets = tokenLimitText.trim() !== '' && timeLimitText.trim() !== '' && Number.isSafeInteger(tokenLimit) && tokenLimit >= 0 && Number.isSafeInteger(timeLimitMs) && timeLimitMs >= 0;
  const dirty = plan !== null && (stagesText.trim() !== plan.plan.stages.join('\n') || tokenLimit !== plan.plan.token_limit || timeLimitMs !== plan.plan.elapsed_limit_ms || !validBudgets);
  const reviewKey = plan ? `${plan.run_id}:${plan.revision}:${plan.plan_digest}` : '';
  const peerReviewRequired = plan?.plan.peer_releases !== undefined
    && (!Array.isArray(plan.plan.peer_releases) || plan.plan.peer_releases.length > 0);
  const releaseReady = !peerReviewRequired || (peerReview.key === reviewKey && peerReview.ready);
  const approvalReady = releaseReady && readiness !== null && plan !== null && readinessMatches(readiness, plan) && !csvDraftDiffers;
  async function savePlan() {
    if (!plan || !validBudgets) return;
    const next = await guarded(() => request<RunView>(`/api/v1/runs/${plan.run_id}/plan`, { ...json({ expected_revision: plan.revision, plan: { ...plan.plan, stages: stagesText.split('\n').map((s) => s.trim()).filter(Boolean), token_limit: tokenLimit, elapsed_limit_ms: timeLimitMs } }), method: 'PATCH' }));
    if (next) { setRun(next); loadPlan(plan.run_id); setPlanNote(text(language, 'Edits saved. Review the updated plan before approving.', 'บันทึกการแก้ไขแล้ว ตรวจสอบแผนที่อัปเดตก่อนอนุมัติ')); }
  }
  async function prepareCurrentPlan() {
    if (!plan || preparationFlight.current || approving || (workflow === 'literature' && !searchTerm.trim()) || (workflow === 'crossref_csv' && !csvSelection)) return;
    preparationFlight.current = true; setPreparingPlan(true); setError('');
    try {
      const next = await preparePlan(plan.run_id, plan.revision, workflow === 'literature' ? [searchTerm.trim().slice(0, 150)] : [], workflow, csvSelection);
      if (next.run_id !== plan.run_id || latestRunId.current !== plan.run_id) throw new ApiError('invalid_response', 502, '');
      setRun(next); await loadPlan(plan.run_id, true);
      setPlanNote(text(language, 'Plan prepared. Review the current requirements and limits before approving.', 'เตรียมแผนแล้ว ตรวจทานข้อกำหนดและขีดจำกัดปัจจุบันก่อนอนุมัติ'));
    } catch (reason) {
      setError(apiErrorMessage(reason, language)); await loadPlan(plan.run_id);
    } finally { preparationFlight.current = false; setPreparingPlan(false); }
  }
  async function approve() {
    if (!plan || dirty || !approvalReady || approvalFlight.current) return;
    const reviewedKey = reviewKey;
    approvalFlight.current = true; setApproving(true);
    try {
      const current = await loadPlan(plan.run_id, true);
      if (!current || !current.readiness || !readinessMatches(current.readiness, current.plan, current.currentRun)) return;
      if (`${current.plan.run_id}:${current.plan.revision}:${current.plan.plan_digest}` !== reviewedKey) {
        setPlanNote(text(language, 'The plan changed. Review the refreshed plan before approving.', 'แผนมีการเปลี่ยนแปลง ตรวจสอบแผนที่รีเฟรชก่อนอนุมัติ')); return;
      }
      setRun(await request<RunView>(`/api/v1/runs/${current.plan.run_id}/approve`, json({ expected_revision: current.plan.revision, plan_digest: current.plan.plan_digest })));
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === 'revision_conflict') { setPlanNote(text(language, 'The plan changed. Review the refreshed plan before approving.', 'แผนมีการเปลี่ยนแปลง ตรวจสอบแผนที่รีเฟรชก่อนอนุมัติ')); loadPlan(plan.run_id); }
      else setError(apiErrorMessage(reason, language));
    } finally { approvalFlight.current = false; setApproving(false); }
  }
  function retry() {
    if (!run) return;
    const original = submissions[run.run_id]; // TODO(contract): after a reload the question falls back to the last owner message
    setArchived((old) => [...old, run]); setQuestion(original?.question ?? messages.filter((m) => m.role !== 'assistant').at(-1)?.content ?? ''); setSelected(original?.selected ?? []); setRetryOf(run.run_id); setActiveRun(null); setRun(null); setPlan(null); attempt.current = null;
  }
  function closeOutput() {
    if (expandedByClick.current) { navigate(-1); return; }
    const search = new URLSearchParams(location.search); search.delete('output');
    navigate({ search: search.toString() }, { replace: true });
  }
  const card = (a: ArtifactView) => <ArtifactCard key={a.artifact_id} artifact={a} language={language} onExpand={(el) => {
    opener.current = el; expandedByClick.current = true;
    const search = new URLSearchParams(location.search); search.set('output', a.artifact_id);
    navigate({ search: search.toString() });
  }} />;
  const expandedArtifact = [run, ...archived].flatMap((r) => r?.artifacts ?? []).find((a) => a.artifact_id === expanded);

  return <section className="research-chat">
    <div className="page-heading"><p className="eyebrow">{text(language, 'Research conversation', 'บทสนทนาวิจัย')}</p><h1 className="route-heading" tabIndex={-1}>{text(language, 'Research chat', 'แชตวิจัย')}</h1>
      <p><Link to={`/projects/${projectId}`}>{text(language, 'Project details', 'รายละเอียดโครงการ')}</Link></p></div>
    <section aria-label={text(language, 'Conversation', 'บทสนทนา')}><ol>{messages.map((m) => <li key={m.id}><strong>{m.role === 'assistant' ? text(language, 'Assistant', 'ผู้ช่วย') : text(language, 'You', 'คุณ')}:</strong> <span style={{ whiteSpace: 'pre-wrap' }}>{m.content}</span></li>)}</ol></section>
    {runSelectionError && <p role="alert">{text(language, 'The requested run is unavailable in this project and conversation.', 'ไม่พบการทำงานที่ร้องขอในโครงการและบทสนทนานี้')}</p>}
    {planError !== null && <p role="alert">{apiErrorMessage(planError, language)}</p>}
    {run && <RunProgress run={run} events={events} pendingDecisions={pendingDecisions} connected={connected} onStop={() => void stop()} stopPending={stopPending} onDecision={(d, choice, usage) => void decide(d, choice, usage)} onRetry={retry} />}
    {plan && run?.state === 'awaiting_approval' && <section aria-label={text(language, 'Plan review', 'ตรวจทานแผน')}>
      <h2>{text(language, 'Review the plan', 'ตรวจทานแผน')}</h2>
      {workflow === 'literature' && <><label htmlFor="plan-search-term">{text(language, 'Literature search term', 'คำค้นวรรณกรรม')}</label><input id="plan-search-term" value={searchTerm} maxLength={150} onChange={(event) => setSearchTerm(event.target.value)} /></>}
      <button type="button" className="button button-quiet button-small" disabled={preparingPlan || approving || (workflow === 'literature' && !searchTerm.trim()) || (workflow === 'crossref_csv' && !csvSelection)} onClick={() => void prepareCurrentPlan()}>{preparingPlan ? text(language, 'Preparing plan…', 'กำลังเตรียมแผน…') : text(language, 'Prepare plan', 'เตรียมแผน')}</button>
      <dl><dt>{text(language, 'Model', 'โมเดล')}</dt><dd>{connections?.find((c) => c.id === plan.plan.provider_id)?.label ?? text(language, 'Selected connection', 'การเชื่อมต่อที่เลือก')} / {plan.plan.model}</dd>
        <dt>{text(language, 'Data recipients', 'ผู้รับข้อมูล')}</dt><dd>{plan.plan.data_recipients.join(', ') || text(language, 'None', 'ไม่มี')}</dd>
        <dt>{text(language, 'Packages', 'แพ็กเกจ')}</dt><dd>{plan.plan.packages.map((p) => `${p.name} ${p.version}`).join(', ') || text(language, 'None', 'ไม่มี')}</dd>
        <dt>{text(language, 'Limits', 'ขีดจำกัด')}</dt><dd>{plan.plan.token_limit} {text(language, 'tokens', 'โทเคน')} · {Math.round(plan.plan.elapsed_limit_ms / 1000)} {text(language, 's', 'วินาที')}</dd></dl>
      {v2Binding && savedCrossref && savedCsvGrant && <section className="saved-scientific-inputs" aria-label={text(language, 'Saved scientific inputs', 'ข้อมูลวิทยาศาสตร์ที่บันทึกไว้')}>
        <h3>{text(language, 'Saved scientific inputs', 'ข้อมูลวิทยาศาสตร์ที่บันทึกไว้')}</h3>
        <dl>
          <dt>{text(language, 'Approved Crossref query', 'คำค้น Crossref ที่อนุมัติ')}</dt><dd>{savedCrossref.query ?? `DOI: ${savedCrossref.doi}`}</dd>
          <dt>{text(language, 'CSV input identity', 'ข้อมูลระบุไฟล์ CSV')}</dt><dd><code>{savedCsvGrant.input_ref.key}</code><br /><code>SHA-256 {savedCsvGrant.input_sha256}</code><br />{savedCsvGrant.input_ref.size} {text(language, 'bytes', 'ไบต์')}</dd>
          <dt>{text(language, 'Numeric columns', 'คอลัมน์ตัวเลข')}</dt><dd>{savedCsvGrant.numeric_columns.join(', ')}</dd>
        </dl>
      </section>}
      {csvDraftDiffers && <p role="status">{text(language, 'The current draft differs from the saved scientific inputs. Prepare the plan again before approval.', 'ฉบับร่างปัจจุบันแตกต่างจากข้อมูลวิทยาศาสตร์ที่บันทึกไว้ โปรดเตรียมแผนใหม่ก่อนอนุมัติ')}</p>}
      {peerReviewRequired && <PeerReleaseReview key={reviewKey} reviewKey={reviewKey} releases={plan.plan.peer_releases} language={language} onReady={peerReviewReady} />}
      <div className="plan-readiness">
        <h3>{text(language, 'Research readiness', 'ความพร้อมของงานวิจัย')}</h3>
        {readinessError ? <p role="alert">{apiErrorMessage(readinessError, language)}</p> : <p role="status">{readiness && readinessMatches(readiness, plan) ? text(language, 'Current plan requirements are ready.', 'ข้อกำหนดของแผนปัจจุบันพร้อมแล้ว') : readiness ? text(language, 'This plan needs current verified setup before approval.', 'แผนนี้ต้องมีการตั้งค่าที่ตรวจสอบล่าสุดก่อนอนุมัติ') : text(language, 'Checking current plan readiness…', 'กำลังตรวจสอบความพร้อมของแผนปัจจุบัน…')}</p>}
        <Link to={`/projects/${projectId}/research-setup?session=${sessionId}&run=${plan.run_id}`}>{text(language, 'Research Setup', 'ความพร้อมของงาน')}</Link>
        <button className="button button-quiet button-small" type="button" disabled={approving} onClick={() => void loadPlan(plan.run_id)}>{text(language, 'Refresh readiness', 'รีเฟรชความพร้อม')}</button>
      </div>
      <label htmlFor="plan-stages">{text(language, 'Research stages (one per line)', 'ขั้นตอนการวิจัย (หนึ่งบรรทัดต่อหนึ่งขั้นตอน)')}</label>
      <textarea id="plan-stages" value={stagesText} onChange={(e) => setStagesText(e.target.value)} />
      <div className="setup-actions"><label htmlFor="plan-token-limit">{text(language, 'Token limit', 'ขีดจำกัดโทเคน')}<input id="plan-token-limit" type="number" min="0" step="1" value={tokenLimitText} onChange={(event) => setTokenLimitText(event.target.value)} /></label><label htmlFor="plan-time-limit">{text(language, 'Time limit (seconds)', 'ขีดจำกัดเวลา (วินาที)')}<input id="plan-time-limit" type="number" min="0" step="0.001" value={timeLimitText} onChange={(event) => setTimeLimitText(event.target.value)} /></label></div>
      {planNote && <p role="status">{planNote}</p>}
      <button type="button" className="button button-quiet button-small" disabled={!dirty || !validBudgets || preparingPlan || approving} onClick={() => void savePlan()}>{text(language, 'Save edits', 'บันทึกการแก้ไข')}</button>
      <button type="button" className="button button-small" disabled={dirty || !approvalReady || approving || preparingPlan} onClick={() => void approve()}>{approving ? text(language, 'Checking approval…', 'กำลังตรวจสอบการอนุมัติ…') : text(language, 'Approve plan', 'อนุมัติแผน')}</button>
      {dirty && <p>{text(language, 'Save your edits and review the new plan; the previous approval no longer applies.', 'บันทึกการแก้ไขและตรวจทานแผนใหม่ การอนุมัติก่อนหน้าไม่มีผลแล้ว')}</p>}
    </section>}
    {run && run.artifacts.length > 0 && <section aria-label={text(language, 'Outputs', 'ผลลัพธ์')}><h2>{text(language, 'Outputs', 'ผลลัพธ์')}</h2>{run.artifacts.map(card)}</section>}
    {archived.map((old) => <section key={old.run_id} aria-label={text(language, 'Previous run', 'การทำงานก่อนหน้า')}><h2>{text(language, 'Previous run (kept)', 'การทำงานก่อนหน้า (เก็บไว้)')}</h2>{old.artifacts.map(card)}</section>)}
    <section aria-label={text(language, 'Composer', 'ส่งคำถาม')}>
      {retryOf && <p role="status">{text(language, 'Retrying a previous run. Review the question and files, then approve a new plan; earlier outputs stay separate.', 'กำลังลองงานก่อนหน้าใหม่ ตรวจทานคำถามและไฟล์ แล้วอนุมัติแผนใหม่ ผลลัพธ์เดิมยังแยกไว้')}</p>}
      <label htmlFor="question">{text(language, 'Research question', 'คำถามวิจัย')}</label>
      <textarea id="question" value={question} onChange={(e) => setQuestion(e.target.value)} />
      <label htmlFor="research-workflow">{text(language, 'Research workflow', 'รูปแบบงานวิจัย')}</label><select id="research-workflow" value={workflow} onChange={(event) => setWorkflow(event.target.value as ResearchWorkflow)}><option value="literature">{text(language, 'Review literature', 'ตรวจทานวรรณกรรม')}</option><option value="resources">{text(language, 'Measure workspace resources', 'ตรวจทรัพยากรของงาน')}</option><option value="crossref_csv">{text(language, 'Find papers and describe a CSV', 'ค้นหาบทความและสรุปไฟล์ CSV')}</option></select>
      {workflow === 'crossref_csv' && <fieldset className="scientific-inputs">
        <legend>{text(language, 'Scientific inputs', 'ข้อมูลวิจัย')}</legend>
        <label htmlFor="crossref-mode">{text(language, 'Crossref request type', 'ชนิดคำขอ Crossref')}</label>
        <select id="crossref-mode" value={crossrefMode} onChange={(event) => setCrossrefMode(event.target.value as CrossrefMode)}><option value="query">{text(language, 'Search query', 'คำค้น')}</option><option value="doi">DOI</option></select>
        <label htmlFor="crossref-term">{crossrefMode === 'doi' ? 'DOI' : text(language, 'Crossref query', 'คำค้น Crossref')}</label>
        <input id="crossref-term" value={crossrefTerm} maxLength={crossrefMode === 'doi' ? 255 : 512} onChange={(event) => setCrossrefTerm(event.target.value)} />
        <label htmlFor="csv-input-file">{text(language, 'CSV input file', 'ไฟล์ CSV สำหรับวิเคราะห์')}</label>
        <select id="csv-input-file" value={csvFileId} onChange={(event) => setCsvFileId(event.target.value)}>
          <option value="">{text(language, 'Choose an uploaded CSV', 'เลือกไฟล์ CSV ที่อัปโหลดแล้ว')}</option>
          {files.filter((file) => file.content_type.split(';')[0].trim().toLowerCase() === 'text/csv' || file.filename.toLowerCase().endsWith('.csv')).map((file) => <option key={file.id} value={file.id} disabled={file.state !== 'ready'}>{file.filename} · {fileStateLabel(file.state, language)}</option>)}
        </select>
        <label htmlFor="csv-numeric-columns">{text(language, 'Numeric columns (comma or line separated)', 'คอลัมน์ตัวเลข (คั่นด้วยจุลภาคหรือขึ้นบรรทัดใหม่)')}</label>
        <textarea id="csv-numeric-columns" aria-label={text(language, 'Numeric columns', 'คอลัมน์ตัวเลข')} value={csvColumnsText} onChange={(event) => setCsvColumnsText(event.target.value)} />
      </fieldset>}
      <ul aria-label={text(language, 'Project files', 'ไฟล์ของโครงการ')}>{files.map((f) => <li key={f.id}>{f.filename} · {fileStateLabel(f.state, language)}
        {!selected.includes(f.id) && <button type="button" className="button button-quiet button-small" disabled={f.state !== 'ready'} aria-label={`${text(language, 'Add to question', 'เพิ่มในคำถาม')}: ${f.filename}`} onClick={() => setSelected((s) => [...s, f.id])}>{text(language, 'Add to question', 'เพิ่มในคำถาม')}</button>}</li>)}</ul>
      <div aria-label={text(language, 'Selected files', 'ไฟล์ที่เลือก')}>{selected.map((id) => { const name = files.find((f) => f.id === id)?.filename ?? id; return <span key={id} className="file-chip">{name}<button type="button" aria-label={`${text(language, 'Remove from question', 'นำออกจากคำถาม')}: ${name}`} onClick={() => setSelected((s) => s.filter((x) => x !== id))}>×</button></span>; })}</div>
      {!connectionsLoading && !hasModel && <p>{connections ? text(language, 'No model is ready.', 'ยังไม่มีโมเดลที่พร้อมใช้') : text(language, 'Settings unavailable.', 'การตั้งค่ายังไม่พร้อมใช้งาน')} <Link to="/settings">{text(language, 'Open Settings', 'เปิดการตั้งค่า')}</Link></p>}
      {error && <p role="alert">{error}</p>}
      <button type="button" className="button button-primary" disabled={submitting || connectionsLoading} onClick={() => void submit()}>{submitting ? text(language, 'Creating…', 'กำลังสร้าง…') : text(language, 'Review plan', 'ตรวจทานแผน')}</button>
    </section>
    {expandedArtifact && <ArtifactViewer artifact={expandedArtifact} onClose={closeOutput} language={language} />}
  </section>;
}
export function Chat({ pollMs = 400 }: { pollMs?: number }) {
  const { projectId = '', sessionId = '' } = useParams();
  return <ChatSession key={`${projectId}/${sessionId}`} pollMs={pollMs} />; // a new session never inherits another's state
}
export default Chat;
