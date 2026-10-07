import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import type { CitationView, FileView, FindingView, ProjectView, RunView, SessionView } from '../../../contracts/api-types';
import { ApiError, apiErrorMessage, refreshOwnerSession, request } from './api';
import { useAppPreferences } from './App';
import './project-original.css';

const text = (language: 'th' | 'en', en: string, th: string) => language === 'th' ? th : en;
type LoadState = 'loading' | 'ready' | 'error';

export function ProjectDetails() {
  const { projectId = '' } = useParams();
  const { language } = useAppPreferences();
  const navigate = useNavigate();
  const [project, setProject] = useState<ProjectView | null>(null);
  const [sessions, setSessions] = useState<SessionView[]>([]);
  const [files, setFiles] = useState<FileView[]>([]);
  const [findings, setFindings] = useState<FindingView[]>([]);
  const [citations, setCitations] = useState<CitationView[]>([]);
  const [runs, setRuns] = useState<RunView[]>([]);
  const [state, setState] = useState<LoadState>('loading');
  const [error, setError] = useState('');
  const [instructions, setInstructions] = useState('');
  const [sessionTitle, setSessionTitle] = useState('');
  const [busy, setBusy] = useState(false);
  const [csrfRecovery, setCsrfRecovery] = useState(false);
  const [refreshingSession, setRefreshingSession] = useState(false);
  const [removing, setRemoving] = useState<FindingView | null>(null);
  const [removeError, setRemoveError] = useState('');
  const dialog = useRef<HTMLDialogElement>(null);
  const mutationRef = useRef<AbortController | null>(null);
  const projectIdentityRef = useRef(projectId);
  projectIdentityRef.current = projectId;

  useEffect(() => {
    const controller = new AbortController();
    mutationRef.current?.abort();
    setState('loading'); setError(''); setRemoveError(''); setProject(null); setSessions([]); setFiles([]); setFindings([]); setCitations([]); setRuns([]); setInstructions(''); setSessionTitle(''); setBusy(false); setRemoving(null); setCsrfRecovery(false);
    const base = `/api/v1/projects/${encodeURIComponent(projectId)}`;
    Promise.all([
      request<ProjectView>(base, { signal: controller.signal }),
      request<SessionView[]>(`${base}/sessions`, { signal: controller.signal }),
      request<FileView[]>(`${base}/files`, { signal: controller.signal }),
      request<FindingView[]>(`${base}/findings`, { signal: controller.signal }),
      request<CitationView[]>(`${base}/sources`, { signal: controller.signal }),
      request<RunView[]>(`${base}/runs`, { signal: controller.signal }),
    ]).then(([p, s, f, findingsResult, citationResult, runResult]) => {
      if (controller.signal.aborted) return;
      setProject(p); setInstructions(p.instructions); setSessions(s); setFiles(f); setFindings(findingsResult); setCitations(citationResult); setRuns(runResult); setState('ready');
    }).catch((reason: unknown) => {
      if (controller.signal.aborted) return;
      setError(reason instanceof ApiError && reason.status === 404 ? 'not-found' : reason instanceof ApiError && reason.status === 403 ? 'forbidden' : apiErrorMessage(reason, language, 'Unable to load project.'));
      setState('error');
    });
    return () => { controller.abort(); mutationRef.current?.abort(); };
  }, [projectId]);

  async function saveInstructions(event: FormEvent) {
    event.preventDefault(); if (!project || busy) return;
    const targetProject = project.id; const controller = new AbortController(); mutationRef.current = controller;
    setBusy(true); setError(''); setRemoveError(''); setCsrfRecovery(false);
    try {
      const updated = await request<ProjectView>(`/api/v1/projects/${targetProject}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ instructions, revision: project.revision }), signal: controller.signal });
      if (controller.signal.aborted || projectIdentityRef.current !== targetProject) return;
      setProject(updated); setInstructions(updated.instructions);
    } catch (reason) { if (!controller.signal.aborted && projectIdentityRef.current === targetProject) { setError(apiErrorMessage(reason, language, 'Unable to save instructions.')); setCsrfRecovery(reason instanceof ApiError && reason.status === 403); } }
    finally { if (!controller.signal.aborted && projectIdentityRef.current === targetProject) setBusy(false); }
  }

  async function createSession(event: FormEvent) {
    event.preventDefault(); if (!sessionTitle.trim() || busy) return;
    const targetProject = projectId; const controller = new AbortController(); mutationRef.current = controller;
    setBusy(true); setError(''); setRemoveError(''); setCsrfRecovery(false);
    try {
      const session = await request<SessionView>(`/api/v1/projects/${targetProject}/sessions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: sessionTitle.trim() }), signal: controller.signal });
      if (controller.signal.aborted || projectIdentityRef.current !== targetProject) return;
      navigate(`/projects/${targetProject}/sessions/${session.id}`);
    } catch (reason) { if (!controller.signal.aborted && projectIdentityRef.current === targetProject) { setError(apiErrorMessage(reason, language, 'Unable to create session.')); setCsrfRecovery(reason instanceof ApiError && reason.status === 403); } }
    finally { if (!controller.signal.aborted && projectIdentityRef.current === targetProject) setBusy(false); }
  }

  async function confirmRemove() {
    if (!removing || busy) return;
    const targetProject = projectId; const controller = new AbortController(); mutationRef.current = controller;
    setBusy(true); setRemoveError(''); setCsrfRecovery(false);
    try {
      await request<void>(`/api/v1/projects/${targetProject}/findings/${removing.id}`, { method: 'DELETE', signal: controller.signal });
      if (controller.signal.aborted || projectIdentityRef.current !== targetProject) return;
      setFindings((items) => items.filter((item) => item.id !== removing.id));
      setRemoving(null); setRemoveError(''); dialog.current?.close();
    } catch (reason) { if (!controller.signal.aborted && projectIdentityRef.current === targetProject) { setRemoveError(apiErrorMessage(reason, language, 'Unable to remove finding.')); setCsrfRecovery(reason instanceof ApiError && reason.status === 403); } }
    finally { if (!controller.signal.aborted && projectIdentityRef.current === targetProject) setBusy(false); }
  }

  async function refreshSession() {
    setRefreshingSession(true);
    try { await refreshOwnerSession(); setCsrfRecovery(false); const message = text(language, 'Session refreshed. Retry the action manually.', 'ต่ออายุเซสชันแล้ว โปรดลองดำเนินการอีกครั้งด้วยตนเอง'); if (removing) setRemoveError(message); else setError(message); }
    catch (reason) { if (removing) setRemoveError(apiErrorMessage(reason, language, 'Unable to refresh session.')); else setError(apiErrorMessage(reason, language, 'Unable to refresh session.')); }
    finally { setRefreshingSession(false); }
  }

  useEffect(() => {
    if (removing && dialog.current && !dialog.current.open) dialog.current.showModal();
    if (!removing && dialog.current?.open) dialog.current.close();
  }, [removing]);

  if (state === 'loading') return <Page title={text(language, 'Project', 'โครงการ')}><p role="status">{text(language, 'Loading project…', 'กำลังโหลดโครงการ…')}</p></Page>;
  if (state === 'error') return <Page title={error === 'not-found' ? text(language, 'Project not found', 'ไม่พบโครงการ') : text(language, 'Project unavailable', 'ไม่สามารถเปิดโครงการได้')}><p role="alert">{error === 'forbidden' ? text(language, 'You do not have access to this project.', 'คุณไม่มีสิทธิ์เข้าถึงโครงการนี้') : error === 'not-found' ? text(language, 'This project may have been removed or the address is incorrect.', 'โครงการนี้อาจถูกลบหรือที่อยู่ไม่ถูกต้อง') : error}</p><Link to="/projects">{text(language, 'Back to projects', 'กลับไปยังโครงการ')}</Link></Page>;
  if (!project) return null;

  const citationById = new Map(citations.map((citation) => [citation.id, citation]));
  const latestSession = sessions[0];
  return <Page title={project.name} eyebrow="YOUR RESEARCH SPACE" className="project-detail-page">
    <p className="project-detail-intro">{text(language, 'Sessions, files, and saved findings are shared across this project. Each session keeps its own conversation history.', 'เซสชัน ไฟล์ และข้อค้นพบที่บันทึกไว้จะแชร์ภายในโครงการนี้ แต่ละเซสชันมีประวัติการสนทนาแยกกัน')}</p>
    <nav className="project-detail-nav" aria-label={text(language, 'Project workspace', 'พื้นที่ทำงานโครงการ')}>
      <Link to="/projects">{text(language, 'All projects', 'ทุกโปรเจกต์')}</Link>
      <Link to={`/projects/${projectId}/library`}>{text(language, 'Sources & outputs', 'แหล่งข้อมูลและผลงาน')}</Link>
      <Link to={`/projects/${projectId}/runs`}>{text(language, 'Run history', 'ประวัติการทำงาน')}</Link>
    </nav>
    {error && <p role="alert">{error} {csrfRecovery && <button type="button" disabled={refreshingSession} onClick={() => void refreshSession()}>{refreshingSession ? text(language, 'Refreshing…', 'กำลังต่ออายุ…') : text(language, 'Refresh session', 'ต่ออายุเซสชัน')}</button>}</p>}
    <div className="project-detail-grid">
    <section className="project-panel detail-panel" aria-labelledby="sessions-title"><div className="detail-panel-heading"><div><p className="project-eyebrow">PROJECT SESSIONS</p><h2 id="sessions-title">{text(language, 'Sessions', 'เซสชัน')}</h2></div>{latestSession && <Link className="button button-primary" to={`/projects/${projectId}/sessions/${latestSession.id}`}>{text(language, 'Continue chat →', 'คุยต่อ →')}</Link>}</div>
      {sessions.length === 0 ? <p>{text(language, 'No sessions yet. Create one to begin a separate conversation.', 'ยังไม่มีเซสชัน สร้างเซสชันเพื่อเริ่มการสนทนาใหม่')}</p> : <ul className="project-item-list">{sessions.map((session) => <li key={session.id}><Link to={`/projects/${projectId}/sessions/${session.id}`}>{session.title}</Link><span>→</span></li>)}</ul>}
      <form className="project-session-form" onSubmit={createSession}>
        <label htmlFor="session-title">{text(language, 'New session', 'เซสชันใหม่')}</label><input id="session-title" value={sessionTitle} onChange={(event) => setSessionTitle(event.target.value)} maxLength={200} required />
        <button className="button button-quiet" disabled={busy || !sessionTitle.trim()}>{text(language, 'Create session', 'สร้างเซสชัน')}</button>
      </form>
    </section>
    <form className="project-panel detail-panel project-instructions-form" onSubmit={saveInstructions}><p className="project-eyebrow">SHARED GUIDANCE</p><h2>{text(language, 'Project instructions', 'คำแนะนำประจำโปรเจกต์')}</h2>
      <label htmlFor="project-instructions">{text(language, 'Instructions shared with sessions in this project', 'คำแนะนำที่แชร์กับทุกเซสชันในโปรเจกต์')}</label><textarea id="project-instructions" rows={5} maxLength={100000} value={instructions} onChange={(event) => setInstructions(event.target.value)} />
      <button className="button button-primary" disabled={busy}>{text(language, 'Save instructions', 'บันทึกคำแนะนำ')}</button>
    </form>
    <section className="project-panel detail-panel" aria-labelledby="project-files-title"><p className="project-eyebrow">PROJECT FILES</p><h2 id="project-files-title">{text(language, 'Shared files', 'ไฟล์ที่แชร์ในโครงการ')}</h2>
      {files.length === 0 ? <p>{text(language, 'No files have been added.', 'ยังไม่มีไฟล์')}</p> : <ul className="project-item-list">{files.map((file) => <li key={file.id}><Link to={`/projects/${projectId}/library#file-${file.id}`}>{file.filename}</Link><span>{stateLabel(file.state, language)}</span></li>)}</ul>}
      <Link className="detail-text-link" to={`/projects/${projectId}/library`}>{text(language, 'Manage files and outputs →', 'จัดการไฟล์และผลงาน →')}</Link>
    </section>
    <section className="project-panel detail-panel" aria-labelledby="findings-title"><p className="project-eyebrow">SAVED EVIDENCE</p><h2 id="findings-title">{text(language, 'Saved findings', 'ข้อค้นพบที่บันทึกไว้')}</h2>
      {findings.length === 0 ? <p>{text(language, 'No findings have been saved.', 'ยังไม่มีข้อค้นพบที่บันทึกไว้')}</p> : findings.map((finding) => <article key={finding.id} id={`finding-${finding.id}`} className="saved-finding">
        <p>{finding.text}</p><p>{text(language, 'Saved from', 'บันทึกจาก')} <Link to={`/projects/${projectId}/sessions/${finding.session_id}`}>{sessions.find((session) => session.id === finding.session_id)?.title ?? finding.session_id}</Link></p>
        {finding.artifact_id && <p>{text(language, 'Report', 'รายงาน')}: <Link to={`/projects/${projectId}/library#artifact-${finding.artifact_id}`}>{runs.flatMap((run) => run.artifacts).find((artifact) => artifact.artifact_id === finding.artifact_id)?.title ?? finding.artifact_id}</Link></p>}
        {finding.citation_ids.length > 0 && <ul aria-label={text(language, 'Finding sources', 'แหล่งที่มาของข้อค้นพบ')}>{finding.citation_ids.map((id) => {
          const citation = citationById.get(id);
          return <li key={id}><Link to={`/projects/${projectId}/library#citation-${id}`}>{citation?.title ?? id}</Link>{citation?.identifier && <> · {citation.identifier}</>}{citation?.verification && <> · {text(language, citation.verification, citation.verification === 'verified' ? 'ยืนยันแล้ว' : citation.verification === 'contradictory' ? 'ข้อมูลขัดแย้ง' : 'ยังไม่ยืนยัน')}</>}{citation?.access && <> · {citationAccess(citation.access, language)}</>}</li>;
        })}</ul>}
        <button className="button button-quiet" type="button" aria-label={`${text(language, 'Remove finding', 'นำข้อค้นพบออก')}: ${finding.text}`} onClick={() => { setRemoving(finding); setRemoveError(''); setCsrfRecovery(false); }}>{text(language, 'Remove saved finding', 'นำข้อค้นพบที่บันทึกไว้ออก')}</button>
      </article>)}</section>
    </div>
    <dialog className="project-dialog" ref={dialog} aria-labelledby="remove-finding-title" onCancel={(event) => { event.preventDefault(); setRemoving(null); }}>
      <h2 id="remove-finding-title">{text(language, 'Remove saved finding?', 'นำข้อค้นพบที่บันทึกไว้ออกหรือไม่')}</h2>
      <p>{text(language, 'This removes the saved finding from the project. Its source file and report remain available.', 'การดำเนินการนี้นำข้อค้นพบออกจากโครงการ ไฟล์ต้นทางและรายงานยังคงอยู่')}</p>
      {removeError && <p role="alert">{removeError} {csrfRecovery && <button type="button" disabled={refreshingSession} onClick={() => void refreshSession()}>{refreshingSession ? text(language, 'Refreshing…', 'กำลังต่ออายุ…') : text(language, 'Refresh session', 'ต่ออายุเซสชัน')}</button>}</p>}
      <div style={{ display: 'flex', gap: 10 }}><button className="button button-quiet" type="button" onClick={() => { setRemoving(null); setRemoveError(''); }}>{text(language, 'Cancel', 'ยกเลิก')}</button><button className="button button-primary" type="button" disabled={busy} onClick={confirmRemove}>{text(language, removeError ? 'Retry removal' : 'Confirm removal', removeError ? 'ลองนำออกอีกครั้ง' : 'ยืนยันการนำออก')}</button></div>
    </dialog>
  </Page>;
}

function stateLabel(state: FileView['state'], language: 'th' | 'en') {
  const labels: Record<FileView['state'], [string, string]> = { uploading: ['Uploading', 'กำลังอัปโหลด'], preparing: ['Preparing', 'กำลังเตรียมไฟล์'], ready: ['Ready', 'พร้อมใช้งาน'], failed: ['Failed', 'ไม่สำเร็จ'] };
  return text(language, labels[state][0], labels[state][1]);
}

function citationAccess(access: NonNullable<CitationView['access']>, language: 'th' | 'en') {
  const labels = { full_text: ['Full text', 'ฉบับเต็ม'], abstract: ['Abstract', 'บทคัดย่อ'], metadata: ['Metadata', 'ข้อมูลบรรณานุกรม'], unavailable: ['Unavailable', 'เข้าถึงไม่ได้'] } as const;
  return text(language, labels[access][0], labels[access][1]);
}

function Page({ title, eyebrow = 'YOUR RESEARCH SPACE', className = '', children }: { title: string; eyebrow?: string; className?: string; children: React.ReactNode }) {
  return <section className={`workspace-placeholder project-original ${className}`}><div className="page-heading project-detail-heading"><p className="project-eyebrow">{eyebrow}</p><h1 className="route-heading" tabIndex={-1}>{title}</h1></div>{children}</section>;
}

export default ProjectDetails;
