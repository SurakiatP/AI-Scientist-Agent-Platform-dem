import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import type { ArtifactView, ConnectionView, FileView, PendingDecisionView, PlanView, RunView } from '../../../contracts/api-types';
import { ApiError, apiErrorMessage, request } from './api';
import { useAppPreferences } from './App';
import { ArtifactCard, ArtifactViewer, INITIAL_VISUAL_STATE, type VisualState } from './ArtifactViewer';
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
function readDraft(key: string): { question: string; selected: string[] } {
  try {
    const v = JSON.parse(sessionStorage.getItem(key) ?? 'null') as { question?: unknown; selected?: unknown } | null;
    return { question: typeof v?.question === 'string' ? v.question : '', selected: Array.isArray(v?.selected) ? v.selected.filter((x): x is string => typeof x === 'string') : [] };
  } catch { return { question: '', selected: [] }; }
}
const TERMINAL = ['completed', 'failed', 'canceled', 'rejected'];
const json = (body: unknown): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

type DecisionBody = { decision_id: string; expected_revision: number; idempotency_key: string; choice: DecisionChoice; add_tokens?: number; add_elapsed_ms?: number; usage_tokens?: number };
const submitDecision = (runId: string, body: DecisionBody) => request<RunView>(`/api/v1/runs/${runId}/decisions`, json(body));

function ChatSession({ pollMs }: { pollMs: number }) {
  const { projectId = '', sessionId = '' } = useParams();
  const { language } = useAppPreferences();
  const base = `/api/v1/projects/${encodeURIComponent(projectId)}`;
  const draftKey = `research-draft:${sessionId}`; // question text and file ids only; never secrets
  const [messages, setMessages] = useState<MessageView[]>([]);
  const [files, setFiles] = useState<FileView[]>([]);
  const [connections, setConnections] = useState<ConnectionView[] | null>(null);
  const [question, setQuestion] = useState(() => readDraft(draftKey).question);
  const [selected, setSelected] = useState<string[]>(() => readDraft(draftKey).selected);
  const [activeRun, setActiveRun] = useState<RunView | null>(null);
  const { run, setRun, events, connected } = useRunEvents(activeRun?.run_id ?? null, { retryBaseDelayMs: pollMs });
  const [pendingDecisions, setPendingDecisions] = useState<PendingDecisionView[]>([]);
  const [archived, setArchived] = useState<RunView[]>([]);
  const [plan, setPlan] = useState<PlanView | null>(null);
  const [peerReview, setPeerReview] = useState({ key: '', ready: false });
  const peerReviewReady = useCallback((key: string, ready: boolean) => setPeerReview({ key, ready }), []);
  const [stagesText, setStagesText] = useState('');
  const [planNote, setPlanNote] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [stopPending, setStopPending] = useState(false);
  const [submissions, setSubmissions] = useState<Record<string, { question: string; selected: string[] }>>({});
  const [retryOf, setRetryOf] = useState<string | null>(null);
  const [visuals, setVisuals] = useState<Record<string, VisualState>>({});
  const [expanded, setExpanded] = useState<string | null>(null);
  const attempt = useRef<{ key: string; signature: string } | null>(null);
  const opener = useRef<HTMLElement | null>(null);

  useEffect(() => { sessionStorage.setItem(draftKey, JSON.stringify({ question, selected })); }, [draftKey, question, selected]);
  useEffect(() => {
    const controller = new AbortController();
    const signal = controller.signal;
    // Independent loads: a missing connections route must not hide messages or files.
    void request<MessageView[]>(`/api/v1/sessions/${sessionId}/messages`, { signal }).then((m) => { if (!signal.aborted) setMessages(m); }).catch((reason: unknown) => { if (!signal.aborted) setError(apiErrorMessage(reason, language, 'Unable to load this conversation.')); });
    void request<FileView[]>(`${base}/files`, { signal }).then((f) => { if (!signal.aborted) setFiles(f); }).catch((reason: unknown) => { if (!signal.aborted) setError(apiErrorMessage(reason, language, 'Unable to load project files.')); });
    // TODO(contract): no connections route exists yet; failure leaves connections null ("Settings unavailable").
    void request<ConnectionView[]>('/api/v1/connections', { signal }).then((c) => { if (!signal.aborted) setConnections(c); }).catch(() => { if (!signal.aborted) setConnections(null); });
    return () => controller.abort();
  }, [base, sessionId]);
  useEffect(() => { // resume the latest run of this session
    const controller = new AbortController();
    request<RunView[]>(`${base}/runs`, { signal: controller.signal }).then((all) => {
      if (!controller.signal.aborted) {
        const latest = all.filter((r) => r.session_id === sessionId).at(-1) ?? null;
        setActiveRun((current) => current ?? latest);
      }
    }).catch(() => undefined);
    return () => controller.abort();
  }, [base, sessionId]);
  useEffect(() => { // refresh file readiness while any file is still being prepared
    if (!files.some((f) => f.state === 'preparing' || f.state === 'uploading')) return;
    const timer = setTimeout(() => { request<FileView[]>(`${base}/files`).then(setFiles).catch(() => undefined); }, pollMs);
    return () => clearTimeout(timer);
  }, [files, base, pollMs]);
  useEffect(() => { // plan review: owner-only plan read, bound to its shown revision/digest
    if (!run || run.state !== 'awaiting_approval') { setPlan(null); return; }
    loadPlan(run.run_id);
  }, [run?.run_id, run?.state]);
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
  useEffect(() => { // controlled, shared play state; reduced motion never auto-plays
    if (!Object.values(visuals).some((v) => !v.paused) || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const timer = setInterval(() => setVisuals((all) => Object.fromEntries(Object.entries(all).map(([id, v]) => [id, v.paused ? v : { ...v, time: v.time + 1 }]))), 250);
    return () => clearInterval(timer);
  }, [visuals]);
  useEffect(() => { if (!expanded) opener.current?.focus(); }, [expanded]);
  useEffect(() => { if (run && (TERMINAL.includes(run.state) || run.state === 'stopping')) setStopPending(false); }, [run?.state]);

  function loadPlan(runId: string) {
    request<PlanView>(`/api/v1/runs/${runId}/plan`).then((view) => { setPlan(view); setStagesText(view.plan.stages.join('\n')); })
      .catch((reason: unknown) => setPlanNote(apiErrorMessage(reason, language)));
  }

  const ready = (id: string) => files.find((f) => f.id === id)?.state === 'ready';
  const hasModel = connections?.some((c) => c.state === 'ready') ?? false;
  const signature = useMemo(() => JSON.stringify([question.trim(), selected]), [question, selected]);

  async function submit() {
    setError('');
    if (!question.trim()) { setError(text(language, 'Enter a research question first.', 'กรุณาพิมพ์คำถามวิจัยก่อน')); return; }
    if (!hasModel) { setError(text(language, 'No model is ready. Open Settings to choose and check one.', 'ยังไม่มีโมเดลที่พร้อมใช้ เปิดการตั้งค่าเพื่อเลือกและตรวจสอบ')); return; }
    if (selected.some((id) => !ready(id))) { setError(text(language, 'Wait for selected files to finish preparing or remove them.', 'รอให้ไฟล์ที่เลือกเตรียมเสร็จ หรือนำออก')); return; }
    if (!attempt.current || attempt.current.signature !== signature) attempt.current = { key: newKey(), signature };
    setSubmitting(true);
    try {
      const connection = connections!.find((c) => c.state === 'ready')!;
      // retry_of is NOT sent: the server rejects unknown fields (422). TODO(contract): link a retry to its predecessor once the API allows it.
      const created = await request<RunView>(`/api/v1/sessions/${sessionId}/runs`, json({ submission_key: attempt.current.key, question: question.trim(), input_ids: selected, provider_id: connection.id, model: connection.model }));
      attempt.current = null; setRetryOf(null);
      setSubmissions((old) => ({ ...old, [created.run_id]: { question: question.trim(), selected } }));
      setMessages((old) => [...old, { id: `local-${created.run_id}`, sequence: old.length + 1, role: 'owner', content: question.trim() }]);
      setQuestion(''); setSelected([]); setActiveRun(created); setRun(created);
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
  const dirty = plan !== null && stagesText.trim() !== plan.plan.stages.join('\n');
  const reviewKey = plan ? `${plan.run_id}:${plan.revision}:${plan.plan_digest}` : '';
  const peerReviewRequired = plan?.plan.peer_releases !== undefined
    && (!Array.isArray(plan.plan.peer_releases) || plan.plan.peer_releases.length > 0);
  const approvalReady = !peerReviewRequired || (peerReview.key === reviewKey && peerReview.ready);
  async function savePlan() {
    if (!plan) return;
    const next = await guarded(() => request<RunView>(`/api/v1/runs/${plan.run_id}/plan`, { ...json({ expected_revision: plan.revision, plan: { ...plan.plan, stages: stagesText.split('\n').map((s) => s.trim()).filter(Boolean) } }), method: 'PATCH' }));
    if (next) { setRun(next); loadPlan(plan.run_id); setPlanNote(text(language, 'Edits saved. Review the updated plan before approving.', 'บันทึกการแก้ไขแล้ว ตรวจสอบแผนที่อัปเดตก่อนอนุมัติ')); }
  }
  async function approve() {
    if (!plan || dirty || !approvalReady) return;
    try {
      setRun(await request<RunView>(`/api/v1/runs/${plan.run_id}/approve`, json({ expected_revision: plan.revision, plan_digest: plan.plan_digest })));
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === 'revision_conflict') { setPlanNote(text(language, 'The plan changed. Review the refreshed plan before approving.', 'แผนมีการเปลี่ยนแปลง ตรวจสอบแผนที่รีเฟรชก่อนอนุมัติ')); loadPlan(plan.run_id); }
      else setError(apiErrorMessage(reason, language));
    }
  }
  function retry() {
    if (!run) return;
    const original = submissions[run.run_id]; // TODO(contract): after a reload the question falls back to the last owner message
    setArchived((old) => [...old, run]); setQuestion(original?.question ?? messages.filter((m) => m.role !== 'assistant').at(-1)?.content ?? ''); setSelected(original?.selected ?? []); setRetryOf(run.run_id); setActiveRun(null); setRun(null); setPlan(null); attempt.current = null;
  }
  const visualOf = (a: ArtifactView) => visuals[a.artifact_id] ?? INITIAL_VISUAL_STATE;
  const setVisual = (a: ArtifactView) => (next: VisualState) => setVisuals((all) => ({ ...all, [a.artifact_id]: next }));
  const card = (a: ArtifactView) => <ArtifactCard key={a.artifact_id} artifact={a} state={visualOf(a)} onStateChange={setVisual(a)} language={language} onExpand={(el) => { opener.current = el; setExpanded(a.artifact_id); }} />;
  const expandedArtifact = [run, ...archived].flatMap((r) => r?.artifacts ?? []).find((a) => a.artifact_id === expanded);

  return <section className="research-chat">
    <div className="page-heading"><p className="eyebrow">{text(language, 'Research conversation', 'บทสนทนาวิจัย')}</p><h1 className="route-heading" tabIndex={-1}>{text(language, 'Research chat', 'แชตวิจัย')}</h1>
      <p><Link to={`/projects/${projectId}`}>{text(language, 'Project details', 'รายละเอียดโครงการ')}</Link></p></div>
    <section aria-label={text(language, 'Conversation', 'บทสนทนา')}><ol>{messages.map((m) => <li key={m.id}><strong>{m.role === 'assistant' ? text(language, 'Assistant', 'ผู้ช่วย') : text(language, 'You', 'คุณ')}:</strong> <span style={{ whiteSpace: 'pre-wrap' }}>{m.content}</span></li>)}</ol></section>
    {run && <RunProgress run={run} events={events} pendingDecisions={pendingDecisions} connected={connected} onStop={() => void stop()} stopPending={stopPending} onDecision={(d, choice, usage) => void decide(d, choice, usage)} onRetry={retry} />}
    {plan && run?.state === 'awaiting_approval' && <section aria-label={text(language, 'Plan review', 'ตรวจทานแผน')}>
      <h2>{text(language, 'Review the plan', 'ตรวจทานแผน')}</h2>
      <dl><dt>{text(language, 'Model', 'โมเดล')}</dt><dd>{connections?.find((c) => c.id === plan.plan.provider_id)?.label ?? text(language, 'Selected connection', 'การเชื่อมต่อที่เลือก')} / {plan.plan.model}</dd>
        <dt>{text(language, 'Data recipients', 'ผู้รับข้อมูล')}</dt><dd>{plan.plan.data_recipients.join(', ') || text(language, 'None', 'ไม่มี')}</dd>
        <dt>{text(language, 'Packages', 'แพ็กเกจ')}</dt><dd>{plan.plan.packages.map((p) => `${p.name} ${p.version}`).join(', ') || text(language, 'None', 'ไม่มี')}</dd>
        <dt>{text(language, 'Limits', 'ขีดจำกัด')}</dt><dd>{plan.plan.token_limit} {text(language, 'tokens', 'โทเคน')} · {Math.round(plan.plan.elapsed_limit_ms / 1000)} {text(language, 's', 'วินาที')}</dd></dl>
      {peerReviewRequired && <PeerReleaseReview key={reviewKey} reviewKey={reviewKey} releases={plan.plan.peer_releases} language={language} onReady={peerReviewReady} />}
      <label htmlFor="plan-stages">{text(language, 'Research stages (one per line)', 'ขั้นตอนการวิจัย (หนึ่งบรรทัดต่อหนึ่งขั้นตอน)')}</label>
      <textarea id="plan-stages" value={stagesText} onChange={(e) => setStagesText(e.target.value)} />
      {planNote && <p role="status">{planNote}</p>}
      <button type="button" className="button button-quiet button-small" disabled={!dirty} onClick={() => void savePlan()}>{text(language, 'Save edits', 'บันทึกการแก้ไข')}</button>
      <button type="button" className="button button-small" disabled={dirty || !approvalReady} onClick={() => void approve()}>{text(language, 'Approve plan', 'อนุมัติแผน')}</button>
      {dirty && <p>{text(language, 'Save your edits and review the new plan; the previous approval no longer applies.', 'บันทึกการแก้ไขและตรวจทานแผนใหม่ การอนุมัติก่อนหน้าไม่มีผลแล้ว')}</p>}
    </section>}
    {run && run.artifacts.length > 0 && <section aria-label={text(language, 'Outputs', 'ผลลัพธ์')}><h2>{text(language, 'Outputs', 'ผลลัพธ์')}</h2>{run.artifacts.map(card)}</section>}
    {archived.map((old) => <section key={old.run_id} aria-label={text(language, 'Previous run', 'การทำงานก่อนหน้า')}><h2>{text(language, 'Previous run (kept)', 'การทำงานก่อนหน้า (เก็บไว้)')}</h2>{old.artifacts.map(card)}</section>)}
    <section aria-label={text(language, 'Composer', 'ส่งคำถาม')}>
      {retryOf && <p role="status">{text(language, 'Retrying a previous run. Review the question and files, then approve a new plan; earlier outputs stay separate.', 'กำลังลองงานก่อนหน้าใหม่ ตรวจทานคำถามและไฟล์ แล้วอนุมัติแผนใหม่ ผลลัพธ์เดิมยังแยกไว้')}</p>}
      <label htmlFor="question">{text(language, 'Research question', 'คำถามวิจัย')}</label>
      <textarea id="question" value={question} onChange={(e) => setQuestion(e.target.value)} />
      <ul aria-label={text(language, 'Project files', 'ไฟล์ของโครงการ')}>{files.map((f) => <li key={f.id}>{f.filename} · {fileStateLabel(f.state, language)}
        {!selected.includes(f.id) && <button type="button" className="button button-quiet button-small" disabled={f.state !== 'ready'} aria-label={`${text(language, 'Add to question', 'เพิ่มในคำถาม')}: ${f.filename}`} onClick={() => setSelected((s) => [...s, f.id])}>{text(language, 'Add to question', 'เพิ่มในคำถาม')}</button>}</li>)}</ul>
      <div aria-label={text(language, 'Selected files', 'ไฟล์ที่เลือก')}>{selected.map((id) => { const name = files.find((f) => f.id === id)?.filename ?? id; return <span key={id} className="file-chip">{name}<button type="button" aria-label={`${text(language, 'Remove from question', 'นำออกจากคำถาม')}: ${name}`} onClick={() => setSelected((s) => s.filter((x) => x !== id))}>×</button></span>; })}</div>
      {!hasModel && <p>{connections ? text(language, 'No model is ready.', 'ยังไม่มีโมเดลที่พร้อมใช้') : text(language, 'Settings unavailable.', 'การตั้งค่ายังไม่พร้อมใช้งาน')} <Link to="/settings">{text(language, 'Open Settings', 'เปิดการตั้งค่า')}</Link></p>}
      {error && <p role="alert">{error}</p>}
      <button type="button" className="button button-primary" disabled={submitting} onClick={() => void submit()}>{submitting ? text(language, 'Creating…', 'กำลังสร้าง…') : text(language, 'Review plan', 'ตรวจทานแผน')}</button>
    </section>
    {expandedArtifact && <ArtifactViewer artifact={expandedArtifact} state={visualOf(expandedArtifact)} onStateChange={setVisual(expandedArtifact)} onClose={() => setExpanded(null)} language={language} />}
  </section>;
}
export function Chat({ pollMs = 400 }: { pollMs?: number }) {
  const { projectId = '', sessionId = '' } = useParams();
  return <ChatSession key={`${projectId}/${sessionId}`} pollMs={pollMs} />; // a new session never inherits another's state
}
export default Chat;
