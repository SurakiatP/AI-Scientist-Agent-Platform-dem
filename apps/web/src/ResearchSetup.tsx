import { useEffect, useRef, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import type { PreparationJobView, PreparationSubmit, ResearchProfileView, ResearchSetupView } from '../../../contracts/api-types';
import { ApiError, apiCodeLabel, apiErrorMessage, getPreparation, newRequestId, request } from './api';
import { useAppPreferences } from './App';
import './research.css';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const stages: Record<PreparationJobView['stage'], [string, string]> = {
  queued: ['Queued for preparation', 'อยู่ในคิวเตรียมงาน'], context: ['Preparing reviewed inputs', 'กำลังเตรียมข้อมูลที่ตรวจสอบแล้ว'],
  image: ['Building environment', 'กำลังสร้างสภาพแวดล้อม'], compatibility: ['Compatibility checks', 'ตรวจสอบความเข้ากันได้'],
  security: ['Security checks', 'ตรวจสอบความปลอดภัย'], license: ['License checks', 'ตรวจสอบสิทธิ์การใช้งาน'],
  isolation: ['Isolation checks', 'ตรวจสอบการแยกพื้นที่'], complete: ['Checks complete', 'ตรวจสอบเสร็จแล้ว'],
  owner_decision: ['Owner review required', 'ต้องให้เจ้าของตรวจทาน'],
};
const states = { ready: ['Ready', 'พร้อมใช้งาน'], missing: ['Setup needed', 'ต้องตั้งค่า'], preparing: ['Preparing', 'กำลังเตรียม'], blocked: ['Blocked', 'ติดข้อจำกัด'], failed: ['Failed', 'ไม่สำเร็จ'] } as const;
const validProfile = (profile: ResearchProfileView) => !!profile && typeof profile.profile_id === 'string' && profile.profile_id.length > 0 && profile.profile_id.length <= 200 && typeof profile.version === 'string' && profile.version.length > 0 && profile.version.length <= 80 && typeof profile.label === 'string' && typeof profile.purpose === 'string' && Object.hasOwn(states, profile.state) && /^[0-9a-f]{64}$/i.test(profile.manifest_sha256) && Number.isSafeInteger(profile.memory_limit_bytes) && profile.memory_limit_bytes > 0 && Number.isSafeInteger(profile.workspace_limit_bytes) && profile.workspace_limit_bytes > 0;
const matches = (job: PreparationJobView, profile: ResearchProfileView, projectId: string) => job.project_id === projectId && job.profile_id === profile.profile_id && job.version === profile.version && job.manifest_sha256 === profile.manifest_sha256 && UUID.test(job.id) && ['queued', 'building', 'checking', 'ready', 'blocked', 'failed', 'unknown'].includes(job.state) && Object.hasOwn(stages, job.stage);
const verified = (job?: PreparationJobView) => job?.state === 'ready' && job.stage === 'complete' && job.evidence_verified === true;
const active = (job: PreparationJobView) => ['queued', 'building', 'checking'].includes(job.state);
const attemptKey = (project: string, profile: ResearchProfileView) => `research-preparation-v1:${project}:${JSON.stringify([profile.profile_id, profile.version, profile.manifest_sha256])}`;
function readAttempt(key: string): string | null {
  try { const value = sessionStorage.getItem(key); return value && UUID.test(value) ? value : null; } catch { return null; }
}

export default function ResearchSetup() {
  const { projectId = '' } = useParams();
  const { language } = useAppPreferences();
  const [search] = useSearchParams();
  const tx = (en: string, th: string) => language === 'th' ? th : en;
  const session = search.get('session');
  const run = search.get('run');
  const settingsContext = new URLSearchParams({ setup_project: projectId });
  if (session && UUID.test(session)) settingsContext.set('setup_session', session);
  if (run && UUID.test(run)) settingsContext.set('setup_run', run);
  const [view, setView] = useState<ResearchSetupView | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [jobErrors, setJobErrors] = useState<Record<string, unknown>>({});
  const [uncertain, setUncertain] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const mounted = useRef(false);
  const busyRef = useRef(false);
  const epoch = useRef(0);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    const controller = new AbortController();
    const currentEpoch = ++epoch.current;
    setView(null); setError(null); setJobErrors({}); setUncertain({});
    void request<ResearchSetupView>(`/api/v1/projects/${encodeURIComponent(projectId)}/research-setup`, { signal: controller.signal }).then((next) => {
      if (controller.signal.aborted || currentEpoch !== epoch.current) return;
      if (next.project_id !== projectId || !Array.isArray(next.requirements) || !Array.isArray(next.profiles) || !Array.isArray(next.connections) || !Array.isArray(next.preparations) || next.profiles.some((profile) => !validProfile(profile))) throw new ApiError('invalid_response', 502, '');
      setView(next);
      setUncertain(Object.fromEntries(next.profiles.map((profile) => [profile.profile_id, !!readAttempt(attemptKey(projectId, profile)) && !next.preparations.some((job) => matches(job, profile, projectId))])));
    }).catch((reason) => { if (!controller.signal.aborted && currentEpoch === epoch.current) setError(reason); });
    return () => controller.abort();
  }, [projectId, refresh]);

  useEffect(() => {
    if (!view) return;
    const jobs = view.preparations.filter((job) => active(job) && view.profiles.some((profile) => matches(job, profile, projectId)));
    if (!jobs.length) return;
    const controller = new AbortController();
    const currentEpoch = epoch.current;
    const timer = window.setTimeout(() => {
      void Promise.all(jobs.map(async (job) => {
        try {
          const next = await getPreparation(projectId, job.id, controller.signal);
          const profile = view.profiles.find((item) => item.profile_id === job.profile_id)!;
          if (next.id !== job.id || !matches(next, profile, projectId)) throw new ApiError('invalid_response', 502, '');
          return { id: job.id, next, error: null };
        } catch (reason) {
          return { id: job.id, next: null, error: reason };
        }
      })).then((replies) => {
        if (controller.signal.aborted || currentEpoch !== epoch.current) return;
        // Commit the completed batch together: replacing view restarts this effect.
        const updates = new Map(replies.filter((reply) => reply.next !== null).map((reply) => [reply.id, reply.next!]));
        setView((current) => current && ({ ...current, preparations: current.preparations.map((item) => updates.get(item.id) ?? item) }));
        setJobErrors((current) => ({ ...current, ...Object.fromEntries(replies.map((reply) => [reply.id, reply.error])) }));
      });
    }, 1500);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [view, projectId]);

  async function prepare(profile: ResearchProfileView) {
    if (busyRef.current || !view || !validProfile(profile)) return;
    busyRef.current = true; setBusy(profile.profile_id); setError(null);
    const currentEpoch = epoch.current;
    const key = attemptKey(projectId, profile);
    const requestId = readAttempt(key) ?? newRequestId();
    try {
      sessionStorage.setItem(key, requestId);
      const body: PreparationSubmit = { profile_id: profile.profile_id, version: profile.version, manifest_sha256: profile.manifest_sha256, request_id: requestId };
      const next = await request<PreparationJobView>(`/api/v1/projects/${encodeURIComponent(projectId)}/preparations`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (!matches(next, profile, projectId)) throw new ApiError('invalid_response', 502, '');
      if (mounted.current && currentEpoch === epoch.current) {
        setView((current) => current && ({ ...current, preparations: [next, ...current.preparations.filter((job) => job.profile_id !== profile.profile_id)] }));
        setUncertain((current) => ({ ...current, [profile.profile_id]: false }));
      }
    } catch (reason) {
      if (mounted.current && currentEpoch === epoch.current) {
        setUncertain((current) => ({ ...current, [profile.profile_id]: true }));
        setError(reason instanceof ApiError && reason.status < 500 ? reason : new Error(tx('We could not confirm whether preparation started. Check the preparation request to reconcile the same request identity.', 'ไม่สามารถยืนยันได้ว่าการเตรียมเริ่มแล้วหรือไม่ ตรวจสอบคำขอเดิมเพื่อยืนยันโดยไม่สร้างคำขอซ้ำ')));
      }
    } finally { busyRef.current = false; if (mounted.current) setBusy(null); }
  }

  return <section className="workspace-placeholder research-setup">
    <div className="page-heading"><p className="eyebrow">{tx('Prepare your research work', 'เตรียมความพร้อมสำหรับงานวิจัย')}</p><h1 className="route-heading" tabIndex={-1}>{tx('Research Setup', 'ความพร้อมของงาน')}</h1></div>
    <p>{tx('Configure the required connections and prepare a reviewed environment. These actions do not approve a research plan or authorize an external call.', 'ตั้งค่าการเชื่อมต่อที่จำเป็นและเตรียมสภาพแวดล้อมที่ตรวจสอบแล้ว การดำเนินการเหล่านี้ไม่ได้อนุมัติแผนวิจัยหรืออนุญาตให้เรียกบริการภายนอก')}</p>
    <div className="setup-actions"><Link className="button button-quiet button-small" to={`/settings?${settingsContext}`}>{tx('Configure connections and access', 'ตั้งค่าการเชื่อมต่อและสิทธิ์')}</Link><Link className="button button-small" to={session && UUID.test(session) ? `/projects/${encodeURIComponent(projectId)}/sessions/${session}${run !== null ? `?${new URLSearchParams({ run })}` : ''}` : `/projects/${encodeURIComponent(projectId)}`}>{session && UUID.test(session) ? tx('Return to plan', 'กลับไปตรวจทานแผน') : tx('Return to project', 'กลับไปยังโครงการ')}</Link><button className="button button-quiet button-small" type="button" onClick={() => setRefresh((value) => value + 1)}>{tx('Refresh setup', 'รีเฟรชความพร้อม')}</button></div>
    {error !== null && <p role="alert">{apiErrorMessage(error, language)}</p>}
    {!view && error === null && <p role="status">{tx('Loading research requirements…', 'กำลังโหลดข้อกำหนดของงานวิจัย…')}</p>}
    {view && <SetupContents view={view} projectId={projectId} language={language} settingsPath={`/settings?${settingsContext}`} uncertain={uncertain} busy={busy} jobErrors={jobErrors} prepare={prepare} />}
  </section>;
}

function SetupContents({ view, projectId, language, settingsPath, uncertain, busy, jobErrors, prepare }: { view: ResearchSetupView; projectId: string; language: 'en' | 'th'; settingsPath: string; uncertain: Record<string, boolean>; busy: string | null; jobErrors: Record<string, unknown>; prepare: (profile: ResearchProfileView) => Promise<void> }) {
  const tx = (en: string, th: string) => language === 'th' ? th : en;
  return <>
    <section className="setup-panel" aria-label={tx('Research requirements', 'ข้อกำหนดของงานวิจัย')}><h2>{tx('Requirements', 'ข้อกำหนด')}</h2>{view.requirements.length === 0 ? <p>{tx('No additional requirements reported.', 'ไม่มีข้อกำหนดเพิ่มเติมจากบริการ')}</p> : <ul>{view.requirements.map((requirement) => <li key={requirement.id}><strong>{requirement.label}</strong><p>{requirement.purpose}</p><p>{states[requirement.state]?.[language === 'th' ? 1 : 0] ?? tx('Status unavailable', 'ไม่ทราบสถานะ')}</p>{requirement.reason && <p>{requirement.reason}</p>}{requirement.action === 'configure_connection' && <Link to={settingsPath}>{tx('Configure connection', 'ตั้งค่าการเชื่อมต่อ')}</Link>}{requirement.action === 'request_approval' && <p>{tx('Owner review is required before this preparation can continue.', 'ต้องให้เจ้าของตรวจทานก่อนดำเนินการเตรียมต่อ')}</p>}{requirement.action === 'provide_hardware' && <p>{tx('The required hardware must be available before this work can run.', 'ต้องมีฮาร์ดแวร์ที่จำเป็นก่อนดำเนินงานนี้')}</p>}</li>)}</ul>}</section>
    <section className="setup-panel" aria-label={tx('Reviewed environments', 'สภาพแวดล้อมที่ตรวจสอบแล้ว')}><h2>{tx('Reviewed environments', 'สภาพแวดล้อมที่ตรวจสอบแล้ว')}</h2>{view.profiles.length === 0 && <p>{tx('No reviewed environment is available.', 'ยังไม่มีสภาพแวดล้อมที่ตรวจสอบแล้ว')}</p>}{view.profiles.map((profile) => {
      const job = view.preparations.find((item) => matches(item, profile, projectId));
      const isReady = verified(job);
      return <article className="setup-profile" key={`${profile.profile_id}:${profile.version}:${profile.manifest_sha256}`}><h3>{profile.label}</h3><p>{profile.purpose}</p><dl><dt>{tx('Version', 'รุ่น')}</dt><dd>{profile.version}</dd><dt>{tx('Memory limit', 'ขีดจำกัดหน่วยความจำ')}</dt><dd>{Math.ceil(profile.memory_limit_bytes / 1048576)} MB</dd><dt>{tx('Workspace limit', 'ขีดจำกัดพื้นที่ทำงาน')}</dt><dd>{Math.ceil(profile.workspace_limit_bytes / 1048576)} MB</dd></dl>
        {job ? <><p role="status">{isReady ? tx('Environment ready', 'สภาพแวดล้อมพร้อมใช้งาน') : stages[job.stage][language === 'th' ? 1 : 0]}</p>{job.state === 'ready' && !isReady && <p role="alert">{tx('Preparation evidence is not verified.', 'ยังไม่มีหลักฐานยืนยันการเตรียมที่ตรวจสอบแล้ว')}</p>}{job.state === 'blocked' && <p>{tx('Preparation is blocked. Resolve the requirement before continuing.', 'การเตรียมติดข้อจำกัด โปรดดำเนินการตามข้อกำหนดก่อน')}</p>}{job.state === 'failed' && <p role="alert">{job.error_code ? apiCodeLabel(job.error_code, language) : tx('Preparation failed.', 'เตรียมไม่สำเร็จ')}</p>}{job.state === 'unknown' && <p role="alert">{tx('Preparation outcome is unknown. Check its recorded status before starting anything else.', 'ไม่ทราบผลการเตรียม โปรดตรวจสอบสถานะที่บันทึกไว้ก่อนเริ่มสิ่งอื่น')}</p>}{jobErrors[job.id] !== undefined && jobErrors[job.id] !== null && <p role="alert">{apiErrorMessage(jobErrors[job.id], language)}</p>}</> : <p>{profile.state === 'ready' ? tx('Preparation evidence is not verified.', 'ยังไม่มีหลักฐานยืนยันการเตรียมที่ตรวจสอบแล้ว') : states[profile.state][language === 'th' ? 1 : 0]}</p>}
        {profile.reason && <p>{profile.reason}</p>}
        {!job && ['missing', 'failed'].includes(profile.state) && <button type="button" className="button button-primary" disabled={busy !== null} onClick={() => void prepare(profile)}>{busy === profile.profile_id ? tx('Submitting…', 'กำลังส่งคำขอ…') : uncertain[profile.profile_id] ? tx('Check preparation request', 'ตรวจสอบคำขอเตรียม') : tx('Prepare environment', 'เตรียมสภาพแวดล้อม')}</button>}
      </article>;
    })}</section>
    <section className="setup-panel" aria-label={tx('Configured connections', 'การเชื่อมต่อที่ตั้งค่าแล้ว')}><h2>{tx('Configured connections', 'การเชื่อมต่อที่ตั้งค่าแล้ว')}</h2>{view.connections.length === 0 ? <p>{tx('No connection is configured.', 'ยังไม่ได้ตั้งค่าการเชื่อมต่อ')}</p> : <ul>{view.connections.map((connection) => <li key={connection.id}><strong>{connection.label}</strong><p>{connection.model} · {connection.provider}</p><p>{connection.state === 'ready' ? tx('Configured; verification not run', 'ตั้งค่าแล้ว; ยังไม่ได้ตรวจสอบ') : connection.state === 'invalid_credentials' ? tx('Stored credential cannot be decrypted.', 'ไม่สามารถถอดรหัสข้อมูลรับรองที่บันทึกไว้') : tx('Connection unavailable.', 'การเชื่อมต่อไม่พร้อมใช้งาน')}</p></li>)}</ul>}</section>
  </>;
}
