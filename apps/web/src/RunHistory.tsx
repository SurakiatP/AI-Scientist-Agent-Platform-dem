import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import type { ProjectView, RunEvent, RunView, SessionView } from '../../../contracts/api-types';
import { ApiError, apiCodeLabel, apiErrorMessage, request } from './api';
import { useAppPreferences } from './App';
import { useRunEvents } from './useRunEvents';
import { stageLabel } from './RunProgress';
import './outputs-original.css';

const text = (language: 'th' | 'en', en: string, th: string) => language === 'th' ? th : en;

export function RunHistory() {
  const { projectId = '' } = useParams();
  const { language } = useAppPreferences();
  const [availableProjects, setAvailableProjects] = useState<ProjectView[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(!projectId);
  const [selectedProjectId, setSelectedProjectId] = useState('');
  const activeProjectId = projectId || selectedProjectId;
  const [runs, setRuns] = useState<RunView[]>([]);
  const [sessions, setSessions] = useState<SessionView[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selectedRunId, setSelectedRunId] = useState('');

  useEffect(() => {
    if (projectId) return;
    const controller = new AbortController();
    setProjectsLoading(true);
    request<ProjectView[]>('/api/v1/projects', { signal: controller.signal }).then((items) => {
      if (!controller.signal.aborted) { setAvailableProjects(items); setProjectsLoading(false); if (items.length === 1) setSelectedProjectId(items[0].id); }
    }).catch((reason: unknown) => { if (!controller.signal.aborted) { setError(apiErrorMessage(reason, language, 'Unable to load projects.')); setProjectsLoading(false); setLoading(false); } });
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

  const selectedRun = runs.find((run) => run.run_id === selectedRunId) ?? runs[0];
  const { events, connected } = useRunEvents(!loading && !error && activeProjectId ? selectedRun?.run_id ?? null : null);
  const timeline = events.filter((event): event is Extract<RunEvent, { kind: 'plan.ready' | 'run.state' | 'stage.started' | 'stage.completed' }> => event.run_id === selectedRun?.run_id && (event.kind === 'plan.ready' || event.kind === 'run.state' || event.kind === 'stage.started' || event.kind === 'stage.completed'));
  const sessionTitle = (run: RunView) => sessions.find((session) => session.id === run.session_id)?.title ?? text(language, 'Research run', 'งานวิจัย');
  return <section className="workspace-placeholder original-history">
    <div className="page-heading"><p className="eyebrow">RESEARCH / HISTORY</p><h1 className="route-heading" tabIndex={-1}>{text(language, 'Know where your research stands', 'รู้ว่างานไปถึงไหน')}</h1><p>{text(language, 'See where your research stands. Review completed, stopped, or interrupted work and choose where to continue.', 'รู้ว่างานไปถึงไหน ดูงานที่เสร็จ หยุดไว้ หรือมีปัญหา แล้วเลือกทำต่อได้')}</p>{projectId ? <Link to={`/projects/${projectId}`}>{text(language, 'Back to project', 'กลับไปยังโครงการ')}</Link> : <label>{text(language, 'Choose a project', 'เลือกโครงการ')} <select aria-label={text(language, 'Choose a project', 'เลือกโครงการ')} value={selectedProjectId} onChange={(event) => { setSelectedProjectId(event.target.value); setSelectedRunId(''); }}><option value="">{text(language, 'Select project', 'เลือกโครงการ')}</option>{availableProjects.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>}</div>
    {!projectsLoading && !loading && !error && !activeProjectId && <div className="original-empty"><h2>{text(language, 'Your research history', 'ประวัติงานวิจัยของคุณ')}</h2><p>{availableProjects.length === 0 ? text(language, 'Create a project to begin your first research run.', 'สร้างโครงการเพื่อเริ่มงานวิจัยแรก') : text(language, 'Choose a project to inspect its runs.', 'เลือกโครงการเพื่อดูประวัติการทำงาน')}</p><Link className="button button-primary" to="/projects">{text(language, 'Open projects', 'เปิดโครงการ')}</Link></div>}
    {(loading || projectsLoading) && <p role="status">{text(language, 'Loading run history…', 'กำลังโหลดประวัติ…')}</p>}
    {error && <p role="alert">{error === 'not-found' ? text(language, 'This project was not found.', 'ไม่พบโครงการนี้') : error === 'forbidden' ? text(language, 'You do not have access to this project.', 'คุณไม่มีสิทธิ์เข้าถึงโครงการนี้') : error}</p>}
    {!loading && !error && activeProjectId && runs.length === 0 && <div className="original-empty"><h2>{text(language, 'No research runs yet', 'ยังไม่มีงานวิจัย')}</h2><p>{text(language, 'Start a conversation in your project to prepare a research plan.', 'เริ่มบทสนทนาในโครงการเพื่อเตรียมแผนวิจัย')}</p><Link className="button button-primary" to={`/projects/${activeProjectId}`}>{text(language, 'Open project', 'เปิดโครงการ')}</Link></div>}
    {!loading && !error && selectedRun && <div className="history-layout">
      <nav className="history-list" aria-label={text(language, 'Research runs', 'รายการงานวิจัย')}>{runs.map((run) => <button key={run.run_id} className={`history-row${run.run_id === selectedRun.run_id ? ' selected' : ''}`} type="button" aria-pressed={run.run_id === selectedRun.run_id} onClick={() => setSelectedRunId(run.run_id)}><span className="run-state-icon" aria-hidden="true">{run.state === 'completed' ? '✓' : run.state === 'failed' ? '!' : run.state === 'canceled' ? 'Ⅱ' : '○'}</span><span><strong>{sessionTitle(run)}</strong><small>{run.run_id.slice(0, 8)}</small></span><span className="state-badge">{stateText(run.state, language)}</span></button>)}</nav>
      <article className="run-detail" aria-label={text(language, 'Run details', 'รายละเอียดงาน')}><p className="eyebrow">RUN DETAILS</p><h2>{sessionTitle(selectedRun)}</h2><p className="run-summary"><strong>{stateText(selectedRun.state, language)}</strong>{selectedRun.error_code && <> · {apiCodeLabel(selectedRun.error_code, language)}</>}</p>
        {selectedRun.stage && <p>{text(language, 'Current stage', 'ขั้นตอนปัจจุบัน')}: {stageLabel(selectedRun.stage, language)}</p>}
        {selectedRun.waiting_reason && <p className="research-note">{text(language, 'Waiting for your decision', 'รอการตัดสินใจของคุณ')}: {apiCodeLabel(selectedRun.waiting_reason, language)}</p>}
        <section className="run-timeline" aria-label={text(language, 'Run timeline', 'ลำดับการทำงาน')}>
          <h3>{text(language, 'Run timeline', 'ลำดับการทำงาน')}</h3>
          {timeline.length === 0 && <p role="status">{connected ? text(language, 'No timeline records are available for this run.', 'ยังไม่มีบันทึกลำดับการทำงานของงานนี้') : text(language, 'Timeline not confirmed. Showing the saved run status above.', 'ยังยืนยันลำดับการทำงานไม่ได้ แสดงสถานะงานที่บันทึกไว้ด้านบน')}</p>}
          {timeline.map((event) => <div key={event.sequence}><span aria-hidden="true">{event.kind === 'stage.completed' ? event.payload.outcome === 'failed' ? '!' : event.payload.outcome === 'partial' ? '◐' : '✓' : '○'}</span><span>{event.kind === 'plan.ready' ? text(language, 'Plan prepared', 'เตรียมแผนแล้ว') : event.kind === 'run.state' ? stateText(event.payload.state, language) : `${stageLabel(event.payload.stage, language)} · ${event.kind === 'stage.completed' ? event.payload.outcome === 'failed' ? text(language, 'Failed', 'ไม่สำเร็จ') : event.payload.outcome === 'partial' ? text(language, 'Partial', 'บางส่วน') : text(language, 'Completed', 'เสร็จสิ้น') : text(language, 'Started', 'เริ่มแล้ว')}`}</span></div>)}
        </section>
        <dl className="run-usage"><dt>{text(language, 'Used tokens', 'โทเคนที่ใช้')}</dt><dd>{selectedRun.usage_tokens.toLocaleString()}</dd><dt>{text(language, 'Reserved tokens', 'โทเคนที่จองไว้')}</dt><dd>{selectedRun.reserved_tokens.toLocaleString()}</dd><dt>{text(language, 'Token limit', 'ขีดจำกัดโทเคน')}</dt><dd>{selectedRun.token_limit.toLocaleString()}</dd></dl>
        <p className="run-identity">{selectedRun.run_id}</p>
        <Link className="button button-quiet" to={`/projects/${activeProjectId}/sessions/${selectedRun.session_id}`}>{text(language, 'Original session', 'เซสชันต้นฉบับ')}: {sessionTitle(selectedRun)}</Link>
        <Link className="button button-quiet" to={`/projects/${activeProjectId}/library`}>{text(language, 'View outputs', 'ดูผลงาน')}</Link>
        {selectedRun.artifacts.length > 0 && <section><h3>{text(language, 'Outputs', 'ผลงาน')}</h3><ul>{selectedRun.artifacts.map((artifact) => <li key={artifact.artifact_id}><Link to={`/projects/${activeProjectId}/library#artifact-${artifact.artifact_id}`}>{artifact.title}</Link> · {artifactKind(artifact.kind, language)}{artifact.partial && ` · ${text(language, 'Partial', 'บางส่วน')}`}</li>)}</ul></section>}
        {(selectedRun.state === 'failed' || selectedRun.state === 'canceled') && <Link className="button button-primary" to={`/projects/${activeProjectId}/sessions/${selectedRun.session_id}?retry=${encodeURIComponent(selectedRun.run_id)}`}>{text(language, 'Prepare a new run from this work', 'เตรียมงานใหม่จากงานนี้')}</Link>}
      </article>
    </div>}
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
