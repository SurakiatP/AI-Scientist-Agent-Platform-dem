import { useEffect, useRef, useState } from 'react';
import type { ArtifactView } from '../../../contracts/api-types';
import { apiErrorMessage, requestBlob } from './api';
import { Markdown } from './Markdown';

type Language = 'th' | 'en';
const text = (language: Language, en: string, th: string) => language === 'th' ? th : en;


type Content = { type: string; body: string | null; url: string | null; truncated?: boolean };
const MAX_TEXT_BYTES = 1024 * 1024;
const MAX_ROWS = 100;
const MAX_COLUMNS = 30;

function csvRows(source: string): { rows: string[][]; truncated: boolean } {
  const rows: string[][] = [];
  let row: string[] = [], cell = '', quoted = false, truncated = false;
  const addCell = () => { if (row.length < MAX_COLUMNS) row.push(cell.slice(0, 4096)); else truncated = true; cell = ''; };
  const addRow = () => { addCell(); rows.push(row); row = []; };
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (char === '"') {
      if (quoted && source[index + 1] === '"') { if (cell.length < 4096) cell += '"'; index += 1; }
      else quoted = !quoted;
    } else if (char === ',' && !quoted) addCell();
    else if ((char === '\n' || char === '\r') && !quoted) {
      if (char === '\r' && source[index + 1] === '\n') index += 1;
      addRow();
      if (rows.length > MAX_ROWS) { truncated = index < source.length - 1; break; }
    } else if (cell.length < 4096) cell += char;
    else truncated = true;
  }
  if (rows.length <= MAX_ROWS && (cell || row.length)) addRow();
  if (quoted) throw new Error('Invalid table content.');
  return { rows, truncated };
}

function Table({ body, language }: { body: string; language: Language }) {
  const { rows, truncated } = csvRows(body);
  return <>{truncated && <p>{text(language, 'Preview limited to 100 rows and 30 columns. Download the complete file.', 'ตัวอย่างแสดงไม่เกิน 100 แถวและ 30 คอลัมน์ ดาวน์โหลดไฟล์ฉบับเต็ม')}</p>}<div className="table-scroll" role="region" aria-label={text(language, 'Output table', 'ตารางผลลัพธ์')} tabIndex={0}><table><thead><tr>{rows[0]?.map((cell, index) => <th scope="col" key={index}>{cell}</th>)}</tr></thead><tbody>{rows.slice(1, MAX_ROWS + 1).map((row, index) => <tr key={index}>{row.map((cell, column) => <td key={column}>{cell}</td>)}</tr>)}</tbody></table></div></>;
}

function Provenance({ artifact, language }: { artifact: ArtifactView; language: Language }) {
  return <p className="artifact-provenance">{text(language, 'Project', 'โครงการ')} {artifact.project_id.slice(0, 8)} · {text(language, 'Run', 'การทำงาน')} {artifact.run_id.slice(0, 8)} · sha256 {artifact.sha256.slice(0, 12)}{artifact.partial ? ` · ${text(language, 'Partial output', 'ผลลัพธ์บางส่วน')}` : ''}</p>;
}

function Report({ artifact, language }: { artifact: ArtifactView; language: Language }) {
  const [content, setContent] = useState<Content | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | null = null;
    setContent(null); setError(null);
    void requestBlob(`/api/v1/artifacts/${encodeURIComponent(artifact.artifact_id)}/content`, controller.signal).then(async ({ blob, contentType }) => {
      if (controller.signal.aborted) return;
      const media = ['image/png', 'image/jpeg', 'image/webp', 'image/gif', 'application/pdf'].includes(contentType);
      const readable = ['text/plain', 'text/markdown', 'text/csv', 'application/json'].includes(contentType);
      if (media) {
        objectUrl = URL.createObjectURL(blob);
        setContent({ type: contentType, body: null, url: objectUrl });
      } else if (readable && blob.size <= MAX_TEXT_BYTES) {
        let body = await blob.text();
        if (controller.signal.aborted) return;
        if (contentType === 'application/json') body = JSON.stringify(JSON.parse(body), null, 2);
        if (contentType === 'text/csv') csvRows(body);
        setContent({ type: contentType, body, url: null });
      } else setContent({ type: 'unsupported', body: null, url: null, truncated: readable });
    }).catch((reason) => { if (!controller.signal.aborted) setError(reason); });
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [artifact.project_id, artifact.artifact_id, artifact.sha256]);
  if (error !== null) return <p role="alert">{apiErrorMessage(error, language)}</p>;
  if (!content) return <p role="status">{text(language, 'Loading output…', 'กำลังโหลดผลลัพธ์…')}</p>;
  if (content.type.startsWith('image/') && content.url) return <img className="artifact-image" src={content.url} alt={artifact.title} />;
  if (content.type === 'application/pdf' && content.url) return <iframe className="artifact-document" src={content.url} title={artifact.title} sandbox="" />;
  if (content.type === 'text/csv' && content.body !== null) return <Table body={content.body} language={language} />;
  if (content.type === 'text/markdown' && content.body !== null) return <Markdown source={content.body} language={language} />;
  if (content.body !== null) return <pre className="artifact-text" tabIndex={0}>{content.body}</pre>;
  return <p>{content.truncated ? text(language, 'This file is too large to preview. Download the complete file.', 'ไฟล์นี้ใหญ่เกินกว่าจะแสดงตัวอย่าง ดาวน์โหลดไฟล์ฉบับเต็ม') : text(language, 'Preview unavailable for this file type.', 'ไม่สามารถแสดงตัวอย่างไฟล์ประเภทนี้ได้')}</p>;
}

export function ArtifactBody({ artifact, language }: { artifact: ArtifactView; language: Language }) {
  return <div className="artifact-content"><Provenance artifact={artifact} language={language} /><Report artifact={artifact} language={language} /><a className="button button-quiet button-small" href={`/api/v1/artifacts/${encodeURIComponent(artifact.artifact_id)}/content`} download={artifact.title}>{text(language, 'Download output', 'ดาวน์โหลดผลลัพธ์')}</a></div>;
}

export function ArtifactCard({ artifact, onExpand, language }: { artifact: ArtifactView; onExpand: (opener: HTMLElement) => void; language: Language }) {
  return <article className="artifact-card" aria-label={artifact.title}>
    <h3>{artifact.title}</h3>
    <ArtifactBody artifact={artifact} language={language} />
    <button type="button" className="button button-quiet button-small" aria-label={`${text(language, 'Expand visual', 'ขยายภาพ')}: ${artifact.title}`} onClick={(event) => onExpand(event.currentTarget)}>{text(language, 'Expand visual', 'ขยายภาพ')}</button>
  </article>;
}

export function ArtifactViewer({ artifact, onClose, language }: { artifact: ArtifactView; onClose: () => void; language: Language }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { const node = dialog.current; if (node && !node.open) node.showModal(); }, []);
  return <dialog ref={dialog} className="artifact-dialog" aria-labelledby="artifact-dialog-title" onCancel={(event) => { event.preventDefault(); onClose(); }} onClose={onClose}>
    <div className="drawer-head"><h2 id="artifact-dialog-title">{artifact.title}</h2><button type="button" className="button button-quiet button-small" onClick={onClose}>{text(language, 'Close', 'ปิด')}</button></div>
    <ArtifactBody artifact={artifact} language={language} />
  </dialog>;
}
