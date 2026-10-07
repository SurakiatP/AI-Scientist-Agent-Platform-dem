import { useEffect, useRef, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import type { CitationView, FileView, ProjectView, RunView } from '../../../contracts/api-types';
import { ApiError, apiCodeLabel, apiErrorMessage, refreshOwnerSession, request, requestBlob } from './api';
import { useAppPreferences } from './App';
import { ArtifactCard, ArtifactViewer } from './ArtifactViewer';
import './outputs-original.css';

const text = (language: 'th' | 'en', en: string, th: string) => language === 'th' ? th : en;
type UploadPolicy = { file_types: string[]; max_upload_bytes: number };

export function Library() {
  const { projectId = '' } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const [tab, setTab] = useState<'report' | 'sources' | 'files'>('report');
  const [selectedArtifactId, setSelectedArtifactId] = useState('');
  const [expandedArtifactId, setExpandedArtifactId] = useState('');
  const artifactOpener = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (location.hash.startsWith('#file-') || location.hash === '#outputs-files') setTab('files');
    else if (location.hash.startsWith('#citation-') || location.hash === '#outputs-sources') setTab('sources');
    else if (location.hash.startsWith('#artifact-')) { setTab('report'); setSelectedArtifactId(location.hash.slice(10)); }
    else setTab('report');
  }, [location.hash]);
  const closeArtifact = () => { setExpandedArtifactId(''); const opener = artifactOpener.current; artifactOpener.current = null; if (opener?.isConnected) requestAnimationFrame(() => opener.focus()); };
  const { language } = useAppPreferences();
  const [availableProjects, setAvailableProjects] = useState<ProjectView[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(!projectId);
  const [selectedProjectId, setSelectedProjectId] = useState('');
  const activeProjectId = projectId || selectedProjectId;
  const [files, setFiles] = useState<FileView[]>([]);
  const [citations, setCitations] = useState<CitationView[]>([]);
  const [runs, setRuns] = useState<RunView[]>([]);
  const [policy, setPolicy] = useState<UploadPolicy | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [uploadMessage, setUploadMessage] = useState('');
  const [uploading, setUploading] = useState(false);
  const [preview, setPreview] = useState<{ file: FileView; url: string; type: string; text?: string } | null>(null);
  const [previewError, setPreviewError] = useState('');
  const [previewingId, setPreviewingId] = useState('');
  const [fileToRemove, setFileToRemove] = useState<FileView | null>(null);
  const [removingFile, setRemovingFile] = useState(false);
  const [removeError, setRemoveError] = useState('');
  const [csrfRecovery, setCsrfRecovery] = useState(false);
  const [refreshingSession, setRefreshingSession] = useState(false);
  const [pollCount, setPollCount] = useState(0);
  const activeProjectRef = useRef(activeProjectId);
  const mutationRef = useRef<AbortController | null>(null);
  const previewRef = useRef<AbortController | null>(null);
  const statusRef = useRef<AbortController | null>(null);
  const previewUrlRef = useRef<string | null>(null);
  const previewDialog = useRef<HTMLDialogElement>(null);
  const previewOpenerRef = useRef<HTMLElement | null>(null);
  const removeDialog = useRef<HTMLDialogElement>(null);
  activeProjectRef.current = activeProjectId;

  useEffect(() => {
    if (projectId) return;
    const controller = new AbortController();
    setProjectsLoading(true);
    request<ProjectView[]>('/api/v1/projects', { signal: controller.signal }).then((items) => {
      if (!controller.signal.aborted) { setAvailableProjects(items); setProjectsLoading(false); if (items.length === 1) setSelectedProjectId(items[0].id); }
    }).catch((reason: unknown) => { if (!controller.signal.aborted) { setError(apiErrorMessage(reason, language, 'Unable to load projects.')); setProjectsLoading(false); } });
    return () => controller.abort();
  }, [projectId]);

  useEffect(() => {
    setExpandedArtifactId('');
    if (!activeProjectId) { setLoading(false); return; }
    const controller = new AbortController();
    setLoading(true); setError(''); setFiles([]); setCitations([]); setRuns([]); setPolicy(null);
    mutationRef.current?.abort(); previewRef.current?.abort(); statusRef.current?.abort();
    setUploading(false); setRemovingFile(false); setUploadMessage(''); setPreviewError(''); setPreviewingId(''); setFileToRemove(null); setRemoveError(''); setPollCount(0); setCsrfRecovery(false);
    const base = `/api/v1/projects/${encodeURIComponent(activeProjectId)}`;
    Promise.all([
      request<FileView[]>(`${base}/files`, { signal: controller.signal }),
      request<CitationView[]>(`${base}/sources`, { signal: controller.signal }),
      request<RunView[]>(`${base}/runs`, { signal: controller.signal }),
      request<UploadPolicy>('/api/v1/capabilities', { signal: controller.signal }),
    ]).then(([fileList, citationList, runList, uploadPolicy]) => {
      if (controller.signal.aborted) return;
      if (!isUploadPolicy(uploadPolicy)) {
        setError(text(language, 'The server returned an invalid upload policy.', 'เซิร์ฟเวอร์ส่งนโยบายอัปโหลดที่ไม่ถูกต้อง'));
        setLoading(false);
        return;
      }
      setFiles(fileList); setCitations(citationList); setRuns(runList); setPolicy(uploadPolicy); setLoading(false);
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) { setError(reason instanceof ApiError && reason.status === 404 ? 'not-found' : reason instanceof ApiError && reason.status === 403 ? 'forbidden' : apiErrorMessage(reason, language, 'Unable to load project library.')); setLoading(false); }
    });
    return () => { controller.abort(); mutationRef.current?.abort(); previewRef.current?.abort(); statusRef.current?.abort(); closePreview(false); };
  }, [activeProjectId]);

  const acceptedTypes = policy?.file_types ?? [];
  const readableTypes = acceptedTypes.map((type) => ({ '.pdf': 'PDF', '.csv': 'CSV', '.md': 'Markdown', '.txt': 'Plain text', '.json': 'JSON', '.xlsx': 'XLSX' }[type] ?? type)).join(', ');

  async function upload(file: File | undefined) {
    setUploadMessage('');
    if (!file || !policy || uploading) return;
    const suffix = file.name.match(/\.[^.]+$/)?.[0].toLowerCase() ?? '';
    const type = file.type || inferType(file.name);
    if (!acceptedTypes.includes(suffix)) { setUploadMessage(text(language, 'This file type is not supported.', 'ไม่รองรับไฟล์ชนิดนี้')); return; }
    if (file.size > policy.max_upload_bytes) { setUploadMessage(text(language, `File exceeds the ${formatSize(policy.max_upload_bytes)} limit.`, `ไฟล์มีขนาดเกิน ${formatSize(policy.max_upload_bytes)}`)); return; }
    const targetProject = activeProjectId;
    const controller = new AbortController(); mutationRef.current = controller;
    setUploading(true); setUploadMessage(text(language, 'Sending file…', 'กำลังส่งไฟล์…'));
    try {
      const result = await request<FileView>(`/api/v1/projects/${targetProject}/files?filename=${encodeURIComponent(file.name)}`, {
        method: 'POST', body: file, headers: { 'Content-Type': type }, signal: controller.signal,
      });
      if (controller.signal.aborted || activeProjectRef.current !== targetProject) return;
      setFiles((items) => [result, ...items.filter((item) => item.id !== result.id)]);
      setPollCount(0);
      setUploadMessage(result.state === 'ready' ? text(language, 'File is ready.', 'ไฟล์พร้อมใช้งาน') : result.state === 'failed' ? text(language, `File preparation failed: ${translateFileError(result.error_code, language)}`, `เตรียมไฟล์ไม่สำเร็จ: ${translateFileError(result.error_code, language)}`) : result.state === 'preparing' ? text(language, 'Upload complete; file preparation is still in progress.', 'อัปโหลดแล้ว กำลังเตรียมไฟล์') : text(language, 'Upload accepted; waiting for the server to report file status.', 'รับไฟล์แล้ว กำลังรอสถานะจากเซิร์ฟเวอร์'));
    } catch (reason) { if (!controller.signal.aborted && activeProjectRef.current === targetProject) { setUploadMessage(apiErrorMessage(reason, language, 'Upload failed.')); setCsrfRecovery(reason instanceof ApiError && reason.status === 403); } }
    finally { if (!controller.signal.aborted && activeProjectRef.current === targetProject) setUploading(false); }
  }

  async function openPreview(file: FileView) {
    if (file.state !== 'ready') return;
    previewOpenerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const targetProject = activeProjectId;
    previewRef.current?.abort();
    const controller = new AbortController(); previewRef.current = controller;
    setPreviewingId(file.id); setPreviewError('');
    try {
      const result = await requestBlob(`/api/v1/projects/${targetProject}/files/${file.id}/content`, controller.signal);
      if (controller.signal.aborted || activeProjectRef.current !== targetProject) return;
      if (!['application/pdf', 'text/plain', 'text/markdown', 'text/csv'].includes(result.contentType)) throw new Error('Preview is unavailable for this file type.');
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
      const url = URL.createObjectURL(result.blob);
      previewUrlRef.current = url;
      if (result.contentType.startsWith('text/')) {
        const content = await result.blob.slice(0, 1024 * 1024).text();
        if (controller.signal.aborted || activeProjectRef.current !== targetProject) { URL.revokeObjectURL(url); if (previewUrlRef.current === url) previewUrlRef.current = null; return; }
        setPreview({ file, url, type: result.contentType, text: content });
      } else setPreview({ file, url, type: result.contentType });
    } catch (reason) { if (!controller.signal.aborted && activeProjectRef.current === targetProject) setPreviewError(apiErrorMessage(reason, language, 'Preview unavailable.')); }
    finally { if (!controller.signal.aborted && activeProjectRef.current === targetProject) setPreviewingId(''); }
  }

  useEffect(() => { if (preview && previewDialog.current && !previewDialog.current.open) previewDialog.current.showModal(); if (!preview && previewDialog.current?.open) previewDialog.current.close(); }, [preview]);
  useEffect(() => { if (fileToRemove && removeDialog.current && !removeDialog.current.open) removeDialog.current.showModal(); if (!fileToRemove && removeDialog.current?.open) removeDialog.current.close(); }, [fileToRemove]);

  async function refreshFiles() {
    if (!activeProjectId) return;
    const targetProject = activeProjectId;
    statusRef.current?.abort();
    const controller = new AbortController(); statusRef.current = controller;
    try {
      const next = await request<FileView[]>(`/api/v1/projects/${targetProject}/files`, { signal: controller.signal });
      if (activeProjectRef.current === targetProject) { setFiles(next); setPollCount((count) => count + 1); }
    } catch (reason) { if (!controller.signal.aborted && activeProjectRef.current === targetProject) setPreviewError(apiErrorMessage(reason, language, 'Unable to refresh file status.')); }
  }

  useEffect(() => {
    if (!activeProjectId || !files.some((file) => file.state === 'preparing' || file.state === 'uploading') || pollCount >= 10) return;
    const timer = window.setTimeout(() => { void refreshFiles(); }, 2000);
    return () => window.clearTimeout(timer);
  }, [activeProjectId, files, pollCount]);

  async function removeSharedFile() {
    if (!fileToRemove || !activeProjectId) return;
    const targetProject = activeProjectId; const file = fileToRemove;
    const controller = new AbortController(); mutationRef.current = controller; setRemoveError(''); setRemovingFile(true); setCsrfRecovery(false);
    try {
      await request<void>(`/api/v1/projects/${targetProject}/files/${file.id}`, { method: 'DELETE', signal: controller.signal });
      if (controller.signal.aborted || activeProjectRef.current !== targetProject) return;
      setFiles((items) => items.filter((item) => item.id !== file.id)); setFileToRemove(null); setRemoveError('');
    } catch (reason) { if (!controller.signal.aborted && activeProjectRef.current === targetProject) { setRemoveError(apiErrorMessage(reason, language, 'Unable to remove file.')); setCsrfRecovery(reason instanceof ApiError && reason.status === 403); } }
    finally { if (!controller.signal.aborted && activeProjectRef.current === targetProject) setRemovingFile(false); }
  }

  async function refreshSession() {
    setRefreshingSession(true);
    try { await refreshOwnerSession(); setCsrfRecovery(false); setUploadMessage(text(language, 'Session refreshed. Retry the action when ready.', 'ต่ออายุเซสชันแล้ว คุณสามารถลองดำเนินการอีกครั้ง')); }
    catch (reason) { setUploadMessage(apiErrorMessage(reason, language, 'Unable to refresh session.')); }
    finally { setRefreshingSession(false); }
  }

  function closePreview(restoreFocus = true) {
    previewRef.current?.abort();
    if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
    previewUrlRef.current = null;
    if (previewDialog.current?.open) previewDialog.current.close();
    setPreview(null);
    const opener = restoreFocus ? previewOpenerRef.current : null;
    previewOpenerRef.current = null;
    if (opener?.isConnected) requestAnimationFrame(() => opener.focus({ preventScroll: true }));
  }

  const artifacts = runs.flatMap((run) => run.artifacts.map((artifact) => ({ ...artifact, sessionId: run.session_id, runState: run.state })));
  const selectedArtifact = artifacts.find((artifact) => artifact.artifact_id === selectedArtifactId) ?? artifacts[0];
  const expandedArtifact = artifacts.find((artifact) => artifact.artifact_id === expandedArtifactId);
  return <section className="workspace-placeholder original-library">
    <div className="page-heading"><p className="eyebrow">RESEARCH / OUTPUTS</p><h1 className="route-heading" tabIndex={-1}>{text(language, 'Evidence and outputs, together', 'หลักฐานและผลงาน อยู่ด้วยกัน')}</h1><p>{text(language, 'Read the report, then trace each finding back to its sources.', 'อ่านรายงาน แล้วกลับไปตรวจแหล่งที่มาของข้อค้นพบได้')}</p>
      {projectId ? <p>{text(language, 'Project evidence for', 'หลักฐานในโครงการ')}: <Link to={`/projects/${projectId}`}>{projectId}</Link></p> : <label>{text(language, 'Choose a project', 'เลือกโครงการ')} <select aria-label={text(language, 'Choose a project', 'เลือกโครงการ')} value={selectedProjectId} onChange={(event) => setSelectedProjectId(event.target.value)}><option value="">{text(language, 'Select a project', 'เลือกโครงการ')}</option>{availableProjects.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>}
    </div>
    <div className="output-layout"><nav className="output-navigation" aria-label={text(language, 'Project outputs', 'ผลงานของโครงการ')}><p className="eyebrow">PROJECT OUTPUTS</p>{(['report', 'sources', 'files'] as const).map((key) => <button key={key} type="button" aria-pressed={tab === key} className={tab === key ? 'selected' : ''} onClick={() => navigate({ pathname: location.pathname, search: location.search, hash: `#outputs-${key}` })}><strong>{text(language, key === 'report' ? 'Reports & outputs' : key === 'sources' ? 'Sources' : 'Project files', key === 'report' ? 'รายงานและผลงาน' : key === 'sources' ? 'แหล่งอ้างอิง' : 'ไฟล์ประกอบ')}</strong><small>{key === 'report' ? 'Reports · Tables · Visuals' : key === 'sources' ? 'Citations · Evidence' : 'Code · Data · Files'}</small></button>)}</nav><div className="output-pane">
      {(loading || projectsLoading) && <p role="status">{text(language, 'Loading project library…', 'กำลังโหลดคลังโครงการ…')}</p>}
      {!loading && !projectsLoading && !error && !activeProjectId && <p>{availableProjects.length ? text(language, 'Choose a project to view its sources and outputs.', 'เลือกโครงการเพื่อดูแหล่งข้อมูลและผลงาน') : text(language, 'No projects are available yet.', 'ยังไม่มีโครงการ')} · <Link to="/projects">{text(language, 'Projects', 'โครงการ')}</Link></p>}
      {!loading && error && <div role="alert"><p>{error === 'not-found' ? text(language, 'This project was not found.', 'ไม่พบโครงการนี้') : error === 'forbidden' ? text(language, 'You do not have access to this project.', 'คุณไม่มีสิทธิ์เข้าถึงโครงการนี้') : text(language, 'Project library could not be loaded.', 'โหลดคลังโครงการไม่สำเร็จ')} {error !== 'not-found' && error !== 'forbidden' && error}</p><Link to="/projects">{text(language, 'Back to projects', 'กลับไปยังโครงการ')}</Link></div>}
      {!loading && !error && activeProjectId && <>
        <section hidden={tab !== 'files'} aria-labelledby="files-heading"><h2 id="files-heading">{text(language, 'Shared files', 'ไฟล์ที่แชร์ในโครงการ')}</h2>
          <p>{text(language, `Accepted formats: ${readableTypes || 'unavailable'} · Maximum size: ${policy ? formatSize(policy.max_upload_bytes) : 'unavailable'}`, `รูปแบบที่รับ: ${readableTypes || 'ไม่มีข้อมูล'} · ขนาดสูงสุด: ${policy ? formatSize(policy.max_upload_bytes) : 'ไม่มีข้อมูล'}`)}</p>
          <button className="button button-quiet" type="button" onClick={() => void refreshFiles()}>{text(language, 'Refresh file status', 'รีเฟรชสถานะไฟล์')}</button>
          <label htmlFor="library-upload">{text(language, 'Upload project files', 'อัปโหลดไฟล์โครงการ')}</label><input id="library-upload" type="file" disabled={!policy || uploading} accept={acceptedTypes.join(',')} onChange={(event) => { const selected = event.target.files?.[0]; event.target.value = ''; void upload(selected); }} />
          {uploadMessage && <p role="status">{uploadMessage}</p>}
          {csrfRecovery && <p role="alert">{text(language, 'Your owner session may have changed. Refresh it, then retry this action manually.', 'เซสชันเจ้าของอาจเปลี่ยนไปแล้ว โปรดต่ออายุเซสชัน แล้วลองดำเนินการนี้อีกครั้งด้วยตนเอง')} <button type="button" disabled={refreshingSession} onClick={() => void refreshSession()}>{refreshingSession ? text(language, 'Refreshing…', 'กำลังต่ออายุ…') : text(language, 'Refresh session', 'ต่ออายุเซสชัน')}</button></p>}
          {files.length === 0 ? <p>{text(language, 'No files have been added to this project.', 'ยังไม่มีไฟล์ในโครงการนี้')}</p> : <ul>{files.map((file) => <li key={file.id} id={`file-${file.id}`}>
            <strong>{file.filename}</strong> · {formatSize(file.size)} · {stateLabel(file.state, language)}
            {file.error_code && <> · {text(language, 'Issue', 'ปัญหา')}: {translateFileError(file.error_code, language)}</>}
            {file.state === 'ready' && <button className="button button-quiet" type="button" disabled={previewingId === file.id} onClick={() => void openPreview(file)}>{previewingId === file.id ? text(language, 'Opening…', 'กำลังเปิด…') : text(language, 'Preview', 'ดูตัวอย่าง')}</button>}
            <button className="button button-quiet" type="button" onClick={() => { setFileToRemove(file); setRemoveError(''); }}>{text(language, 'Remove shared file', 'นำไฟล์ที่แชร์ออก')}</button>
          </li>)}</ul>}
        </section>
        {previewError && <p role="alert">{previewError}</p>}
        <dialog ref={previewDialog} aria-labelledby="preview-heading" onCancel={(event) => { event.preventDefault(); closePreview(); }}>
          {preview && <><h2 id="preview-heading">{text(language, 'Preview', 'ตัวอย่าง')}: {preview.file.filename}</h2><button className="button button-quiet" type="button" onClick={() => closePreview()}>{text(language, 'Close preview', 'ปิดตัวอย่าง')}</button>
            {preview.text !== undefined ? <pre style={{ whiteSpace: 'pre-wrap', overflow: 'auto', maxHeight: 520 }}>{preview.text}{preview.file.size > 1024 * 1024 ? `\n\n${text(language, 'Preview truncated to 1 MiB.', 'แสดงตัวอย่างไม่เกิน 1 MiB')}` : ''}</pre> : <iframe title={`${text(language, 'Authorized preview', 'ตัวอย่างที่ได้รับอนุญาต')}: ${preview.file.filename}`} src={preview.url} style={{ width: 'min(80vw, 900px)', minHeight: '70vh', border: 0 }} />}
          </>}
        </dialog>
        <section hidden={tab !== 'report'} aria-labelledby="outputs-heading"><h2 id="outputs-heading">{text(language, 'Reports and outputs', 'รายงานและผลงาน')}</h2>
          {artifacts.length === 0 ? <div className="original-empty"><p>{text(language, 'No outputs have been created yet.', 'ยังไม่มีรายงานหรือผลงาน')}</p>{activeProjectId && <Link className="button button-quiet" to={`/projects/${activeProjectId}`}>{text(language, 'Open project', 'เปิดโครงการ')}</Link>}</div> : <>
          <label className="output-picker">{text(language, 'Choose output', 'เลือกผลงาน')}<select value={selectedArtifact?.artifact_id ?? ''} onChange={(event) => navigate({ pathname: location.pathname, search: location.search, hash: `#artifact-${event.target.value}` })}>{artifacts.map((artifact) => <option key={artifact.artifact_id} value={artifact.artifact_id}>{artifact.title}{artifact.partial ? ` · ${text(language, 'Partial', 'บางส่วน')}` : ''}</option>)}</select></label>
          {selectedArtifact && <div id={`artifact-${selectedArtifact.artifact_id}`}><span className="state-badge">{artifactKind(selectedArtifact.kind, language)}{selectedArtifact.partial ? ` · ${text(language, 'Partial', 'บางส่วน')}` : ''}</span><ArtifactCard artifact={selectedArtifact} language={language} onExpand={(opener) => { artifactOpener.current = opener; setExpandedArtifactId(selectedArtifact.artifact_id); }} /><p>{text(language, 'Created in', 'สร้างใน')} <Link to={`/projects/${activeProjectId}/sessions/${selectedArtifact.sessionId}`}>{text(language, 'Open research conversation', 'เปิดบทสนทนาวิจัย')}</Link></p></div>}
        </>}
        </section>
        <section hidden={tab !== 'sources'} aria-labelledby="citations-heading"><h2 id="citations-heading">{text(language, 'Sources', 'แหล่งอ้างอิง')}</h2>
          {citations.length === 0 ? <p>{text(language, 'No sources are available yet.', 'ยังไม่มีแหล่งอ้างอิง')}</p> : <ul>{citations.map((citation) => <li key={citation.id} id={`citation-${citation.id}`}>
            <strong>{citation.title}</strong>{citation.authors.length > 0 && <> · {citation.authors.join(', ')}</>}{citation.year && <> · {citation.year}</>}{citation.identifier && <> · {citation.identifier}</>}
            <span> · {text(language, `Access: ${citation.access ?? 'unknown'}; verification: ${citation.verification ?? 'unknown'}`, `การเข้าถึง: ${translateAccess(citation.access, language)}; การยืนยัน: ${translateVerification(citation.verification, language)}`)}</span>
            {citation.original_url && safeExternalUrl(citation.original_url) && <> · <a href={citation.original_url} target="_blank" rel="noreferrer">{text(language, 'Original source', 'แหล่งต้นฉบับ')}</a></>}
          </li>)}</ul>}
        </section>
      </>}
      </div>
    </div>
    {expandedArtifact && <ArtifactViewer artifact={expandedArtifact} onClose={closeArtifact} language={language} />}
    <dialog ref={removeDialog} aria-labelledby="remove-shared-file-title" onCancel={(event) => { event.preventDefault(); setFileToRemove(null); setRemoveError(''); }}>
      <h2 id="remove-shared-file-title">{text(language, 'Remove shared file?', 'นำไฟล์ที่แชร์ออกหรือไม่')}</h2>
      <p>{text(language, 'This hides the file from this project. Existing reports, findings, and source provenance remain.', 'ไฟล์จะถูกซ่อนจากโครงการนี้ รายงาน ข้อค้นพบ และที่มาของข้อมูลที่มีอยู่จะยังคงอยู่')}</p>
      {removeError && <p role="alert">{removeError} {csrfRecovery && <button type="button" disabled={refreshingSession} onClick={() => void refreshSession()}>{text(language, 'Refresh session', 'ต่ออายุเซสชัน')}</button>}</p>}
      <div style={{ display: 'flex', gap: 10 }}><button className="button button-quiet" type="button" disabled={removingFile} onClick={() => { setFileToRemove(null); setRemoveError(''); }}>{text(language, 'Cancel', 'ยกเลิก')}</button><button className="button button-primary" type="button" disabled={removingFile} onClick={() => void removeSharedFile()}>{removingFile ? text(language, 'Removing…', 'กำลังนำออก…') : text(language, removeError ? 'Retry removal' : 'Confirm removal', removeError ? 'ลองนำออกอีกครั้ง' : 'ยืนยันการนำออก')}</button></div>
    </dialog>
  </section>;
}

function isUploadPolicy(value: unknown): value is UploadPolicy {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const policy = value as Partial<UploadPolicy>;
  return Array.isArray(policy.file_types) && policy.file_types.length > 0 && policy.file_types.every((type) => typeof type === 'string' && /^\.[a-z0-9]+$/i.test(type)) && typeof policy.max_upload_bytes === 'number' && Number.isSafeInteger(policy.max_upload_bytes) && policy.max_upload_bytes > 0;
}
function inferType(filename: string) { const ext = filename.split('.').pop()?.toLowerCase(); return ({ pdf: 'application/pdf', csv: 'text/csv', md: 'text/markdown', txt: 'text/plain', json: 'application/json', xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' } as Record<string, string>)[ext ?? ''] ?? ''; }
function stateLabel(state: FileView['state'], language: 'th' | 'en') { const values = { uploading: ['Uploading', 'กำลังอัปโหลด'], preparing: ['Preparing', 'กำลังเตรียมไฟล์'], ready: ['Ready', 'พร้อมใช้งาน'], failed: ['Failed', 'ไม่สำเร็จ'] }; return text(language, values[state][0], values[state][1]); }
function translateFileError(value: string | null | undefined, language: 'th' | 'en') { return value ? apiCodeLabel(value, language) : text(language, 'Unknown file issue', 'ปัญหาไฟล์ที่ไม่ทราบสาเหตุ'); }
function formatSize(size: number) { if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toLocaleString(undefined, { maximumFractionDigits: 1 })} MiB`; return `${Math.ceil(size / 1024)} KiB`; }
function safeExternalUrl(value: string) { try { const url = new URL(value); return url.protocol === 'https:' || url.protocol === 'http:'; } catch { return false; } }
function translateVerification(value: CitationView['verification'], language: 'th' | 'en') { if (language === 'en') return value ?? 'unknown'; return value === 'verified' ? 'ยืนยันแล้ว' : value === 'contradictory' ? 'ข้อมูลขัดแย้ง' : value === 'unverified' ? 'ยังไม่ยืนยัน' : 'ไม่ทราบ'; }
function translateAccess(value: CitationView['access'], language: 'th' | 'en') { if (language === 'en') return value ?? 'unknown'; return value === 'full_text' ? 'ฉบับเต็ม' : value === 'abstract' ? 'บทคัดย่อ' : value === 'metadata' ? 'ข้อมูลบรรณานุกรม' : value === 'unavailable' ? 'เข้าถึงไม่ได้' : 'ไม่ทราบ'; }

function artifactKind(kind: string, language: 'th' | 'en') { const labels: Record<string, [string, string]> = { report: ['Report', 'รายงาน'], table: ['Table', 'ตาราง'], plot: ['Plot', 'กราฟ'], file: ['File', 'ไฟล์'] }; const pair = labels[kind]; return pair ? text(language, pair[0], pair[1]) : text(language, 'Output', 'ผลงาน'); }

export default Library;
