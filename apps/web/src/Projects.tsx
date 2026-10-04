import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import type { ProjectView } from '../../../contracts/api-types';
import { ApiError, apiErrorMessage, refreshOwnerSession, request } from './api';
import { useAppPreferences } from './App';

const text = (language: 'th' | 'en', en: string, th: string) => language === 'th' ? th : en;

export function Projects() {
  const { language } = useAppPreferences();
  const navigate = useNavigate();
  const [projects, setProjects] = useState<ProjectView[]>([]);
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading');
  const [error, setError] = useState('');
  const [name, setName] = useState('');
  const [creating, setCreating] = useState(false);
  const [csrfRecovery, setCsrfRecovery] = useState(false);
  const [refreshingSession, setRefreshingSession] = useState(false);
  const mutationRef = useRef<AbortController | null>(null);

  useEffect(() => () => mutationRef.current?.abort(), []);

  useEffect(() => {
    const controller = new AbortController();
    setState('loading'); setError('');
    request<ProjectView[]>('/api/v1/projects', { signal: controller.signal })
      .then((result) => { if (!controller.signal.aborted) { setProjects(result); setState('ready'); } })
      .catch((reason: unknown) => { if (!controller.signal.aborted) { setError(apiErrorMessage(reason, language, 'Unable to load projects.')); setState('error'); } });
    return () => controller.abort();
  }, []);

  async function createProject(event: FormEvent) {
    event.preventDefault();
    if (!name.trim() || creating) return;
    const controller = new AbortController(); mutationRef.current = controller;
    setCreating(true); setError(''); setCsrfRecovery(false);
    try {
      const project = await request<ProjectView>('/api/v1/projects', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.trim() }), signal: controller.signal });
      if (controller.signal.aborted) return;
      navigate(`/projects/${project.id}`);
    } catch (reason) { if (!controller.signal.aborted) { setError(apiErrorMessage(reason, language, 'Unable to create project.')); setCsrfRecovery(reason instanceof ApiError && reason.status === 403); } }
    finally { if (!controller.signal.aborted) setCreating(false); }
  }

  async function refreshSession() {
    setRefreshingSession(true);
    try { await refreshOwnerSession(); setCsrfRecovery(false); setError(text(language, 'Session refreshed. Retry creating the project manually.', 'ต่ออายุเซสชันแล้ว โปรดลองสร้างโครงการอีกครั้งด้วยตนเอง')); }
    catch (reason) { setError(apiErrorMessage(reason, language, 'Unable to refresh session.')); }
    finally { setRefreshingSession(false); }
  }

  return <section className="workspace-placeholder">
    <div className="page-heading"><p className="eyebrow">AI SCIENTIST AGENT PLATFORM</p><h1 className="route-heading" tabIndex={-1}>{text(language, 'Projects', 'โครงการ')}</h1></div>
    <div className="placeholder-card" style={{ maxWidth: 900 }}>
      <h2>{text(language, 'Your research spaces', 'พื้นที่งานวิจัยของคุณ')}</h2>
      {state === 'loading' && <p role="status">{text(language, 'Loading projects…', 'กำลังโหลดโครงการ…')}</p>}
      {state === 'error' && <p role="alert">{text(language, 'Projects could not be loaded.', 'โหลดโครงการไม่สำเร็จ')} {error}</p>}
      {state === 'ready' && projects.length === 0 && <p>{text(language, 'No projects yet. Create one to organize sessions, files, and saved findings.', 'ยังไม่มีโครงการ สร้างโครงการเพื่อจัดระเบียบเซสชัน ไฟล์ และข้อค้นพบที่บันทึกไว้')}</p>}
      {state === 'ready' && projects.length > 0 && <ul aria-label={text(language, 'Projects', 'โครงการ')}>
        {projects.map((project) => <li key={project.id}><Link to={`/projects/${project.id}`}>{project.name}</Link></li>)}
      </ul>}
      <form onSubmit={createProject} style={{ display: 'grid', gap: 12, maxWidth: 520, marginTop: 24 }}>
        <label htmlFor="project-name">{text(language, 'New project name', 'ชื่อโครงการใหม่')}</label>
        <input id="project-name" value={name} onChange={(event) => setName(event.target.value)} maxLength={200} required />
        <button className="button button-primary" type="submit" disabled={creating || !name.trim()}>{creating ? text(language, 'Creating…', 'กำลังสร้าง…') : text(language, 'Create project', 'สร้างโครงการ')}</button>
      </form>
      {error && state !== 'error' && <p role="alert">{error} {csrfRecovery && <button type="button" disabled={refreshingSession} onClick={() => void refreshSession()}>{refreshingSession ? text(language, 'Refreshing…', 'กำลังต่ออายุ…') : text(language, 'Refresh session', 'ต่ออายุเซสชัน')}</button>}</p>}
    </div>
  </section>;
}

export default Projects;
