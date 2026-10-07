import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import type { FileView, ProjectView, RunView, SessionView } from '../../../contracts/api-types';
import { ApiError, apiErrorMessage, refreshOwnerSession, request } from './api';
import { useAppPreferences } from './App';
import './project-original.css';

const text = (language: 'th' | 'en', en: string, th: string) => language === 'th' ? th : en;
type ProjectSummary = ProjectView & { sessions: SessionView[]; files: FileView[]; runs: RunView[]; summaryReady: boolean };

export function Projects() {
  const { language } = useAppPreferences();
  const navigate = useNavigate();
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading');
  const [error, setError] = useState('');
  const [name, setName] = useState('');
  const [creating, setCreating] = useState(false);
  const [openingChatProjectId, setOpeningChatProjectId] = useState('');
  const [dialogOpen, setDialogOpen] = useState(false);
  const [csrfRecovery, setCsrfRecovery] = useState(false);
  const [refreshingSession, setRefreshingSession] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const mutationRef = useRef<AbortController | null>(null);

  useEffect(() => () => mutationRef.current?.abort(), []);

  useEffect(() => {
    const controller = new AbortController();
    setState('loading'); setError('');
    request<ProjectView[]>('/api/v1/projects', { signal: controller.signal })
      .then(async (items) => {
        const summaries = await Promise.all(items.map(async (project): Promise<ProjectSummary> => {
          const base = `/api/v1/projects/${encodeURIComponent(project.id)}`;
          const results = await Promise.allSettled([
            request<SessionView[]>(`${base}/sessions`, { signal: controller.signal }),
            request<FileView[]>(`${base}/files`, { signal: controller.signal }),
            request<RunView[]>(`${base}/runs`, { signal: controller.signal }),
          ]);
          if (controller.signal.aborted) return { ...project, sessions: [], files: [], runs: [], summaryReady: false };
          const [sessions, files, runs] = results;
          return {
            ...project,
            sessions: sessions.status === 'fulfilled' ? sessions.value : [],
            files: files.status === 'fulfilled' ? files.value : [],
            runs: runs.status === 'fulfilled' ? runs.value : [],
            summaryReady: results.every((result) => result.status === 'fulfilled'),
          };
        }));
        if (!controller.signal.aborted) { setProjects(summaries); setState('ready'); }
      })
      .catch((reason: unknown) => { if (!controller.signal.aborted) { setError(apiErrorMessage(reason, language, 'Unable to load projects.')); setState('error'); } });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (dialogOpen && dialog.current && !dialog.current.open) dialog.current.showModal();
    if (!dialogOpen && dialog.current?.open) dialog.current.close();
  }, [dialogOpen]);

  async function createProject(event: FormEvent) {
    event.preventDefault();
    if (!name.trim() || creating) return;
    const controller = new AbortController(); mutationRef.current = controller;
    setCreating(true); setError(''); setCsrfRecovery(false);
    try {
      const project = await request<ProjectView>('/api/v1/projects', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.trim() }), signal: controller.signal });
      if (controller.signal.aborted) return;
      dialog.current?.close();
      navigate(`/projects/${project.id}`);
    } catch (reason) {
      if (!controller.signal.aborted) { setError(apiErrorMessage(reason, language, 'Unable to create project.')); setCsrfRecovery(reason instanceof ApiError && reason.status === 403); }
    } finally { if (!controller.signal.aborted) setCreating(false); }
  }

  async function refreshSession() {
    setRefreshingSession(true);
    try { await refreshOwnerSession(); setCsrfRecovery(false); setError(text(language, 'Session refreshed. Retry creating the project manually.', 'ต่ออายุเซสชันแล้ว โปรดลองสร้างโครงการอีกครั้งด้วยตนเอง')); }
    catch (reason) { setError(apiErrorMessage(reason, language, 'Unable to refresh session.')); }
    finally { setRefreshingSession(false); }
  }

  async function startProjectChat(project: ProjectSummary) {
    if (openingChatProjectId) return;
    const controller = new AbortController(); mutationRef.current = controller;
    setOpeningChatProjectId(project.id); setError(''); setCsrfRecovery(false);
    try {
      const session = await request<SessionView>(`/api/v1/projects/${encodeURIComponent(project.id)}/sessions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: text(language, 'New research session', 'เซสชันงานวิจัยใหม่') }), signal: controller.signal });
      if (!controller.signal.aborted) navigate(`/projects/${project.id}/sessions/${session.id}`);
    } catch (reason) {
      if (!controller.signal.aborted) { setError(apiErrorMessage(reason, language, 'Unable to create session.')); setCsrfRecovery(reason instanceof ApiError && reason.status === 403); }
    } finally { if (!controller.signal.aborted) setOpeningChatProjectId(''); }
  }

  const sessionCount = projects.reduce((count, project) => count + project.sessions.length, 0);
  const fileCount = projects.reduce((count, project) => count + project.files.length, 0);
  const outputCount = projects.reduce((count, project) => count + project.runs.reduce((total, run) => total + run.artifacts.length, 0), 0);
  const workspaceCountsReady = projects.every((project) => project.summaryReady);
  const recentSession = projects.flatMap((project) => project.sessions.slice(0, 1).map((session) => ({ project, session })))[0];

  return <div className="project-original">
    <header className="project-page-heading">
      <div><p className="project-eyebrow">YOUR RESEARCH SPACE</p><h1 className="route-heading" tabIndex={-1}>{text(language, 'Research starts here', 'งานวิจัยเริ่มต้นที่นี่')}</h1><p>{text(language, 'Pick up a question or open a space for what you want to explore next.', 'กลับมาต่อคำถามเดิม หรือเปิดพื้นที่สำหรับสิ่งที่อยากค้นต่อ')}</p></div>
      <button className="button button-primary" type="button" onClick={() => { setName(''); setError(''); setCsrfRecovery(false); setDialogOpen(true); }}>{text(language, '＋ Create project', '＋ สร้างโปรเจกต์')}</button>
    </header>
    {state === 'loading' && <p role="status">{text(language, 'Loading projects…', 'กำลังโหลดโครงการ…')}</p>}
    {state === 'error' && <p className="project-error" role="alert">{text(language, 'Projects could not be loaded.', 'โหลดโครงการไม่สำเร็จ')} {error}</p>}
    {state === 'ready' && projects.length === 0 && <section className="project-panel first-use-panel" aria-labelledby="first-use-title">
      <p className="project-eyebrow">START YOUR RESEARCH</p><h2 id="first-use-title">{text(language, 'Welcome to your research space', 'ยินดีต้อนรับสู่พื้นที่วิจัยของคุณ')}</h2>
      <p>{text(language, 'Connect a model, then create a project for your first question.', 'เริ่มจากเชื่อมโมเดล แล้วสร้างโปรเจกต์สำหรับคำถามแรกของคุณ')}</p>
      <div className="setup-steps">
        <article className="setup-step"><span className="step-number">01</span><div><h3>{text(language, 'Connect a language model', 'เชื่อมโมเดลภาษา')}</h3><p>{text(language, 'Choose a provider and check your connection.', 'เลือกผู้ให้บริการและตรวจสอบการเชื่อมต่อ')}</p><Link className="button button-quiet" to="/settings">{text(language, 'Configure model', 'ตั้งค่าโมเดล')}</Link></div></article>
        <article className="setup-step"><span className="step-number">02</span><div><h3>{text(language, 'Create your first project', 'สร้างโปรเจกต์แรก')}</h3><p>{text(language, 'Sessions, files, and findings stay together in a project.', 'เซสชัน ไฟล์ และข้อค้นพบจะอยู่ด้วยกันในโปรเจกต์')}</p><button className="button button-primary" type="button" onClick={() => setDialogOpen(true)}>{text(language, 'Create first project', 'สร้างโปรเจกต์แรก')}</button></div></article>
      </div>
    </section>}
    {state === 'ready' && projects.length > 0 && <>
      <div className="project-summary-strip" aria-label={text(language, 'Workspace totals', 'ภาพรวมพื้นที่วิจัย')}>
        <div><strong>{projects.length}</strong><span>{text(language, 'Projects', 'โปรเจกต์')}</span></div>
        <div><strong>{workspaceCountsReady ? sessionCount : '—'}</strong><span>{text(language, 'Sessions', 'บทสนทนา')}</span></div>
        <div><strong>{workspaceCountsReady ? fileCount + outputCount : '—'}</strong><span>{text(language, 'Files & outputs', 'ไฟล์และผลงาน')}</span></div>
      </div>
      <section className="project-card-grid" aria-label={text(language, 'Projects', 'โปรเจกต์')}>
        {projects.map((project, index) => {
          const latest = project.sessions[0];
          const outputTotal = project.runs.reduce((total, run) => total + run.artifacts.length, 0);
          return <article className="project-panel project-card" key={project.id}>
            <div className="project-card-top"><span className="project-icon" aria-hidden="true">{index % 2 === 0 ? '◈' : '◇'}</span><span className="project-pill">{text(language, 'Project', 'โปรเจกต์')}</span></div>
            <h2><Link className="project-card-title-link" to={`/projects/${project.id}`}>{project.name}</Link></h2><p className="project-card-summary">{project.instructions.trim() || text(language, 'A research space for your questions, sessions, and evidence.', 'พื้นที่สำหรับคำถาม เซสชัน และหลักฐานงานวิจัย')}</p>
            <p className="project-card-meta">{project.summaryReady ? `${project.sessions.length} ${text(language, 'sessions', 'บทสนทนา')} · ${project.files.length + outputTotal} ${text(language, 'files & outputs', 'ไฟล์และผลงาน')}` : text(language, 'Project counts are unavailable.', 'ไม่สามารถโหลดจำนวนรายการได้')}</p>
            <div className="project-card-actions">
              {latest ? <Link className="button button-primary" to={`/projects/${project.id}/sessions/${latest.id}`}>{text(language, 'Open chat →', 'เปิดแชต →')}</Link> : <button className="button button-primary" type="button" disabled={Boolean(openingChatProjectId)} onClick={() => void startProjectChat(project)}>{openingChatProjectId === project.id ? text(language, 'Creating…', 'กำลังสร้าง…') : text(language, 'Start chat →', 'เริ่มแชต →')}</button>}
              <Link className="button button-quiet" to={`/projects/${project.id}/library`}>{text(language, 'View outputs', 'ดูผลงาน')}</Link>
            </div>
          </article>;
        })}
      </section>
      {recentSession && <section className="project-panel continue-project">
        <h2>{text(language, 'Continue research', 'กลับไปทำต่อ')}</h2>
        <Link className="resume-project-row" to={`/projects/${recentSession.project.id}/sessions/${recentSession.session.id}`}><span aria-hidden="true">↗</span><span><strong>{recentSession.session.title}</strong><small>{recentSession.project.name}</small></span><span aria-hidden="true">→</span></Link>
      </section>}
    </>}
    {error && state !== 'error' && <p className="project-error" role="alert">{error} {csrfRecovery && <button type="button" disabled={refreshingSession} onClick={() => void refreshSession()}>{refreshingSession ? text(language, 'Refreshing…', 'กำลังต่ออายุ…') : text(language, 'Refresh session', 'ต่ออายุเซสชัน')}</button>}</p>}
    <dialog className="project-dialog" ref={dialog} aria-labelledby="new-project-title" onCancel={(event) => { event.preventDefault(); setDialogOpen(false); }}>
      <form onSubmit={createProject}><p className="project-eyebrow">NEW PROJECT</p><h2 id="new-project-title">{text(language, 'Make a new research space', 'เปิดพื้นที่วิจัยใหม่')}</h2>
        <label htmlFor="project-name">{text(language, 'Project name', 'ชื่อโปรเจกต์')}</label><input id="project-name" value={name} onChange={(event) => setName(event.target.value)} maxLength={200} required autoFocus />
        <p className="project-field-note">{text(language, 'You can add project instructions after it is created.', 'เพิ่มคำแนะนำประจำโปรเจกต์ได้หลังจากสร้างแล้ว')}</p>
        {error && <p role="alert">{error} {csrfRecovery && <button type="button" disabled={refreshingSession} onClick={() => void refreshSession()}>{refreshingSession ? text(language, 'Refreshing…', 'กำลังต่ออายุ…') : text(language, 'Refresh session', 'ต่ออายุเซสชัน')}</button>}</p>}
        <div className="project-dialog-actions"><button className="button button-quiet" type="button" onClick={() => setDialogOpen(false)}>{text(language, 'Cancel', 'ยกเลิก')}</button><button className="button button-primary" type="submit" disabled={creating || !name.trim()}>{creating ? text(language, 'Creating…', 'กำลังสร้าง…') : text(language, 'Create and open project', 'สร้างและเปิดโปรเจกต์')}</button></div>
      </form>
    </dialog>
  </div>;
}

export default Projects;
