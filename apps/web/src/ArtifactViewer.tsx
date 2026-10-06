import { useEffect, useRef, useState } from 'react';
import type { ArtifactView } from '../../../contracts/api-types';
import { apiErrorMessage, requestBlob } from './api';
import { Markdown } from './Markdown';

type Language = 'th' | 'en';
const text = (language: Language, en: string, th: string) => language === 'th' ? th : en;


type ResourceMeasurement = { cpu_count: number; cpu_quota_cores: number | null; memory_limit_bytes: number | null; memory_current_bytes: number | null; gpu_validation: false };
type Content = { type: string; body: string | null; url: string | null; truncated?: boolean; resources?: ResourceMeasurement | null };
const MAX_TEXT_BYTES = 1024 * 1024;
const MAX_ROWS = 100;
const MAX_COLUMNS = 30;

function resourceMeasurement(source: string, value: unknown): ResourceMeasurement | null {
  // Match the fixed canonical resource_recipe envelope, including its key order.
  // This also rejects duplicate keys without hiding fields from unrelated JSON.
  // Python may encode integral CPU quotas as 2.0; JS stringify equality would reject them.
  const match = source.match(/^\{"instruction_fingerprint":"[0-9a-f]{64}","measurement":\{"cpu_count":\d+,"cpu_quota_cores":(null|\d+(?:\.\d+)?(?:e[+-]?\d+)?),"gpu_validation":false,"memory_current_bytes":(?:null|\d+),"memory_limit_bytes":(?:null|\d+)\},"profile_id":"prof\.worker-base@py3\.14\.7","recipe_id":"get-available-resources","schema_version":1\}$/);
  if (!source.endsWith('}') || !match) return null;
  const measurement = (value as { measurement: ResourceMeasurement }).measurement;
  const { cpu_count: count, cpu_quota_cores: quota, memory_limit_bytes: limit, memory_current_bytes: current } = measurement;
  if (!Number.isInteger(count) || count < 1 || count > 4096
    || (quota !== null && (!Number.isFinite(quota) || quota <= 0 || quota > 4096))
    || (limit !== null && (!Number.isSafeInteger(limit) || limit < 1))
    || (current !== null && (!Number.isSafeInteger(current) || current < 0))
    || (limit !== null && current !== null && current > limit)) return null;
  if (quota !== null) {
    const spelling = quota >= 0.0001 ? String(quota) : quota.toExponential().replace(/e-(\d)$/, 'e-0$1');
    const spellings = Number.isInteger(quota) ? [spelling, `${spelling}.0`] : [spelling];
    if (!spellings.includes(match[1])) return null;
  }
  return measurement;
}

function Resources({ measurement, language }: { measurement: ResourceMeasurement; language: Language }) {
  const number = (value: number) => new Intl.NumberFormat(language === 'th' ? 'th-TH' : 'en-US', { maximumSignificantDigits: 21 }).format(value);
  const bytes = (value: number) => {
    const exact = `${number(value)} ${text(language, 'bytes', 'ไบต์')}`;
    const divisor = value >= 1048576 ? 1048576 : value >= 1024 ? 1024 : null;
    if (divisor === null) return exact;
    const amount = new Intl.NumberFormat(language === 'th' ? 'th-TH' : 'en-US', { maximumFractionDigits: 2 }).format(value / divisor);
    return `${amount} ${divisor === 1048576 ? 'MiB' : 'KiB'} (${exact})`;
  };
  const missing = text(language, 'Not reported', 'ไม่มีข้อมูล');
  const rows = [
    [text(language, 'Available CPU cores', 'แกนประมวลผลที่ใช้ได้'), number(measurement.cpu_count)],
    [text(language, 'CPU quota', 'โควตาการประมวลผล'), measurement.cpu_quota_cores === null ? text(language, 'No finite limit reported', 'ไม่มีข้อมูลขีดจำกัด') : `${number(measurement.cpu_quota_cores)} ${text(language, 'cores', 'แกน')}`],
    [text(language, 'Memory limit', 'หน่วยความจำสูงสุด'), measurement.memory_limit_bytes === null ? text(language, 'No finite limit reported', 'ไม่มีข้อมูลขีดจำกัด') : bytes(measurement.memory_limit_bytes)],
    [text(language, 'Memory in use', 'หน่วยความจำที่ใช้อยู่'), measurement.memory_current_bytes === null ? missing : bytes(measurement.memory_current_bytes)],
    [text(language, 'GPU availability', 'การใช้ GPU'), text(language, 'Not assessed', 'ยังไม่ได้ประเมิน')],
  ];
  return <><h4>{text(language, 'Available resources', 'ทรัพยากรที่ใช้ได้')}</h4><div className="table-scroll" role="region" aria-label={text(language, 'Resource measurement', 'ข้อมูลทรัพยากร')} tabIndex={0}><table><thead><tr><th scope="col">{text(language, 'Resource', 'ทรัพยากร')}</th><th scope="col">{text(language, 'Measured value', 'ค่าที่วัดได้')}</th></tr></thead><tbody>{rows.map(([label, value]) => <tr key={label}><th scope="row">{label}</th><td>{value}</td></tr>)}</tbody></table></div></>;
}

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
      const media = ['image/png', 'image/jpeg', 'image/webp', 'image/gif', 'image/svg+xml', 'application/pdf'].includes(contentType);
      const readable = ['text/plain', 'text/markdown', 'text/csv', 'application/json'].includes(contentType);
      if (media) {
        objectUrl = URL.createObjectURL(blob);
        setContent({ type: contentType, body: null, url: objectUrl });
      } else if (readable && blob.size <= MAX_TEXT_BYTES) {
        let body = await blob.text();
        if (controller.signal.aborted) return;
        let resources: ResourceMeasurement | null = null;
        if (contentType === 'application/json') {
          const value: unknown = JSON.parse(body);
          resources = resourceMeasurement(body, value);
          body = JSON.stringify(value, null, 2);
        }
        if (contentType === 'text/csv') csvRows(body);
        setContent({ type: contentType, body, url: null, resources });
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
  if (content.resources) return <Resources measurement={content.resources} language={language} />;
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
