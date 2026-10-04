import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import type { ProjectView, RunView, SessionView } from '../../../contracts/api-types';
import { ApiError, apiCodeLabel, apiErrorMessage, request } from './api';
import { useAppPreferences } from './App';

const text = (language: 'th' | 'en', en: string, th: string) => language === 'th' ? th : en;

export function RunHistory() {
  const { projectId = '' } = useParams();
  const { language } = useAppPreferences();
  const [availableProjects, setAvailableProjects] = useState<ProjectView[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState('');
  const activeProjectId = projectId || selectedProjectId;
  const [runs, setRuns] = useState<RunView[]>([]);
  const [sessions, setSessions] = useState<SessionView[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (projectId) return;
    const controller = new AbortController();
    request<ProjectView[]>('/api/v1/projects', { signal: controller.signal }).then((items) => {
      if (!controller.signal.aborted) { setAvailableProjects(items); if (items.length === 1) setSelectedProjectId(items[0].id); }
    }).catch((reason: unknown) => { if (!controller.signal.aborted) { setError(apiErrorMessage(reason, language, 'Unable to load projects.')); setLoading(false); } });
    return () => controller.abort();
  }, [projectId]);

  useEffect(() => {
    if (!activeProjectId) { setLoading(false); return; }
    const controller = new AbortController();
    setLoading(true); setError(''); setRuns([]); setSessions([]);
    const base = `/api/v1/projects/${encodeURIComponent(activeProjectId)}`;
    Promise.all([
      request<RunView[]>(`${base}/runs`, { signal: controller.signal }),
      request<SessionView[]>(`${base}/sessions`, { signal: controller.signal }),
    ]).then(([runList, sessionList]) => {
      if (!controller.signal.aborted) { setRuns(runList); setSessions(sessionList); setLoading(false); }
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) { setError(reason instanceof ApiError && reason.status === 404 ? 'not-found' : reason instanceof ApiError && reason.status === 403 ? 'forbidden' : apiErrorMessage(reason, language, 'Unable to load run history.')); setLoading(false); }
    });
    return () => controller.abort();
  }, [activeProjectId]);

  return <section className="workspace-placeholder">
    <div className="page-heading"><p className="eyebrow">AI SCIENTIST AGENT PLATFORM</p><h1 className="route-heading" tabIndex={-1}>{text(language, 'Run history', 'ประวัติการทำงาน')}</h1>{projectId ? <p>{text(language, 'Research runs for project', 'งานวิจัยในโครงการ')}: <Link to={`/projects/${projectId}`}>{projectId}</Link></p> : <label>{text(language, 'Choose a project', 'เลือกโครงการ')} <select aria-label={text(language, 'Choose a project', 'เลือกโครงการ')} value={selectedProjectId} onChange={(event) => setSelectedProjectId(event.target.value)}><option value="">{text(language, 'Select a project', 'เลือกโครงการ')}</option>{availableProjects.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>}</div>
    <div className="placeholder-card" style={{ maxWidth: 960 }}>
      {loading && <p role="status">{text(language, 'Loading run history…', 'กำลังโหลดประวัติ…')}</p>}
      {!loading && !error && !activeProjectId && <p>{availableProjects.length ? text(language, 'Choose a project to view its run history.', 'เลือกโครงการเพื่อดูประวัติการทำงาน') : text(language, 'No projects are available yet.', 'ยังไม่มีโครงการ')} · <Link to="/projects">{text(language, 'Projects', 'โครงการ')}</Link></p>}
      {!loading && error && <p role="alert">{error === 'not-found' ? text(language, 'This project was not found.', 'ไม่พบโครงการนี้') : error === 'forbidden' ? text(language, 'You do not have access to this project.', 'คุณไม่มีสิทธิ์เข้าถึงโครงการนี้') : text(language, 'Run history could not be loaded.', 'โหลดประวัติไม่สำเร็จ')} {error !== 'not-found' && error !== 'forbidden' && error}</p>}
      {!loading && !error && activeProjectId && runs.length === 0 && <p>{text(language, 'No research runs have been started in this project.', 'ยังไม่มีงานวิจัยในโครงการนี้')}</p>}
      {!loading && !error && activeProjectId && runs.map((run) => {
        const session = sessions.find((item) => item.id === run.session_id);
        return <article key={run.run_id} style={{ paddingBlock: 18, borderBottom: '1px solid var(--line)' }}>
          <h2>{text(language, 'Research run', 'งานวิจัย')} · {run.run_id}</h2>
          <p>{text(language, 'State', 'สถานะ')}: <strong>{stateText(run.state, language)}</strong>{run.error_code && <> · {text(language, 'Issue', 'ปัญหา')}: {apiCodeLabel(run.error_code, language)}</>}</p>
          <p>{text(language, 'Original session', 'เซสชันต้นฉบับ')}: <Link to={`/projects/${activeProjectId}/sessions/${run.session_id}`}>{session?.title ?? run.session_id}</Link></p>
          {run.artifacts.length > 0 && <div><h3>{text(language, 'Outputs', 'ผลงาน')}</h3><ul>{run.artifacts.map((artifact) => <li key={artifact.artifact_id}>
            <Link to={`/projects/${activeProjectId}/library#artifact-${artifact.artifact_id}`}>{artifact.title}</Link> · {artifact.partial ? text(language, 'Partial output', 'ผลงานบางส่วน') : text(language, 'Complete output', 'ผลงานสมบูรณ์')} · {artifactKind(artifact.kind, language)}
          </li>)}</ul></div>}
          {(run.state === 'failed' || run.state === 'canceled') && <p><Link to={`/projects/${activeProjectId}/sessions/${run.session_id}?retry=${encodeURIComponent(run.run_id)}`}>{text(language, 'Review and retry', 'ตรวจทานและลองใหม่')}</Link></p>}
        </article>;
      })}
    </div>
  </section>;
}

function stateText(state: RunView['state'], language: 'th' | 'en') {
  const labels: Record<RunView['state'], [string, string]> = {
    planning: ['Planning', 'กำลังวางแผน'], awaiting_approval: ['Awaiting approval', 'รอการอนุมัติ'], queued: ['Queued', 'อยู่ในคิว'], running: ['Running', 'กำลังทำงาน'], waiting_input: ['Waiting for input', 'รอข้อมูล'], recovering: ['Recovering', 'กำลังกู้คืน'], stopping: ['Stopping', 'กำลังหยุด'], completed: ['Completed', 'เสร็จสมบูรณ์'], failed: ['Failed', 'ไม่สำเร็จ'], canceled: ['Canceled', 'ยกเลิกแล้ว'], rejected: ['Rejected', 'ปฏิเสธ'],
  };
  return text(language, labels[state][0], labels[state][1]);
}

function artifactKind(kind: string, language: 'th' | 'en') { const labels: Record<string, [string, string]> = { report: ['Report', 'รายงาน'], table: ['Table', 'ตาราง'], plot: ['Plot', 'กราฟ'], file: ['File', 'ไฟล์'] }; const pair = labels[kind]; return pair ? text(language, pair[0], pair[1]) : text(language, 'Output', 'ผลงาน'); }

export default RunHistory;
