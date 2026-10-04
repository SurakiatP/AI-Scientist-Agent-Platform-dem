import { useEffect, useId, useRef, useState } from 'react';
import type { ArtifactView } from '../../../contracts/api-types';
import { requestBlob } from './api';
import { Markdown } from './Markdown';

type Language = 'th' | 'en';
const text = (language: Language, en: string, th: string) => language === 'th' ? th : en;

export type VisualState = { diffusion: number; paused: boolean; time: number };
export const INITIAL_VISUAL_STATE: VisualState = { diffusion: 0.45, paused: true, time: 0 };

function profile(state: VisualState): string {
  const variance = 2 * (0.1 + state.diffusion) * (1 + state.time / 4);
  return Array.from({ length: 41 }, (_, i) => {
    const x = -10 + i * 0.5;
    return `${(150 + x * 13).toFixed(1)},${(130 - 100 * Math.exp(-(x * x) / (2 * variance * 4))).toFixed(1)}`;
  }).join(' ');
}

function Provenance({ artifact, language }: { artifact: ArtifactView; language: Language }) {
  return <p className="artifact-provenance">{text(language, 'Project', 'โครงการ')} {artifact.project_id.slice(0, 8)} · {text(language, 'Run', 'การทำงาน')} {artifact.run_id.slice(0, 8)} · sha256 {artifact.sha256.slice(0, 12)}{artifact.partial ? ` · ${text(language, 'Partial output', 'ผลลัพธ์บางส่วน')}` : ''}</p>;
}

function Report({ artifact, language }: { artifact: ArtifactView; language: Language }) {
  const [body, setBody] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    requestBlob(`/api/v1/artifacts/${artifact.artifact_id}/content`, controller.signal)
      .then(({ blob }) => blob.text()).then((value) => { if (!controller.signal.aborted) setBody(value); })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [artifact.project_id, artifact.artifact_id]);
  if (failed) return <p role="alert">{text(language, 'This output could not be loaded.', 'ไม่สามารถโหลดผลลัพธ์นี้ได้')}</p>;
  return body === null ? <p role="status">{text(language, 'Loading output…', 'กำลังโหลดผลลัพธ์…')}</p> : <Markdown source={body} language={language} />;
}

export function ArtifactBody({ artifact, state, onStateChange, language }: { artifact: ArtifactView; state: VisualState; onStateChange: (state: VisualState) => void; language: Language }) {
  const id = useId();
  if (artifact.kind === 'report') return <><Provenance artifact={artifact} language={language} /><Report artifact={artifact} language={language} /></>;
  if (artifact.kind !== 'plot') return <Provenance artifact={artifact} language={language} />;
  const label = text(language, 'Diffusion', 'การแพร่');
  const description = text(language, 'Concentration profile broadens as the setting increases.', 'โปรไฟล์ความเข้มข้นกว้างขึ้นเมื่อเพิ่มค่าตั้งต้น');
  return <div className="artifact-visual">
    <Provenance artifact={artifact} language={language} />
    <svg viewBox="0 0 300 170" role="img" aria-label={description}>
      <path d="M20 130H290M150 20V130" stroke="var(--line)" />
      <polyline points={profile(state)} fill="none" stroke="var(--accent)" strokeWidth="2" />
      <text x="290" y="148" textAnchor="end" fontSize="9" fill="var(--muted)">{text(language, 'Position (mm)', 'ตำแหน่ง (มม.)')}</text>
      <text x="24" y="16" fontSize="9" fill="var(--muted)">{text(language, 'Concentration (a.u.)', 'ความเข้มข้น (หน่วยสัมพัทธ์)')}</text>
    </svg>
    <p className="artifact-legend"><span aria-hidden="true" className="legend-swatch" /> {text(language, 'Illustrative model profile', 'โปรไฟล์จากแบบจำลองเพื่อประกอบความเข้าใจ')}</p>
    <label htmlFor={`${id}-d`}>{label} <output htmlFor={`${id}-d`}>{state.diffusion.toFixed(2)}</output></label>
    <input id={`${id}-d`} type="range" min="0" max="1" step="0.05" value={state.diffusion} onChange={(event) => onStateChange({ ...state, diffusion: Number(event.target.value) })} />
    <div className="lab-actions">
      <button type="button" className="button button-small" onClick={() => onStateChange({ ...state, paused: !state.paused })}>{state.paused ? text(language, 'Play', 'เล่น') : text(language, 'Pause', 'หยุดชั่วคราว')}</button>
      <button type="button" className="button button-quiet button-small" onClick={() => onStateChange({ ...INITIAL_VISUAL_STATE })}>{text(language, 'Reset', 'เริ่มใหม่')}</button>
    </div>
  </div>;
}

export function ArtifactCard({ artifact, state, onStateChange, onExpand, language }: { artifact: ArtifactView; state: VisualState; onStateChange: (state: VisualState) => void; onExpand: (opener: HTMLElement) => void; language: Language }) {
  return <article className="artifact-card" aria-label={artifact.title}>
    <h3>{artifact.title}</h3>
    <ArtifactBody artifact={artifact} state={state} onStateChange={onStateChange} language={language} />
    <button type="button" className="button button-quiet button-small" aria-label={`${text(language, 'Expand visual', 'ขยายภาพ')}: ${artifact.title}`} onClick={(event) => onExpand(event.currentTarget)}>{text(language, 'Expand visual', 'ขยายภาพ')}</button>
  </article>;
}

export function ArtifactViewer({ artifact, state, onStateChange, onClose, language }: { artifact: ArtifactView; state: VisualState; onStateChange: (state: VisualState) => void; onClose: () => void; language: Language }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { const node = dialog.current; if (node && !node.open) node.showModal(); }, []);
  return <dialog ref={dialog} className="artifact-dialog" aria-labelledby="artifact-dialog-title" onCancel={(event) => { event.preventDefault(); onClose(); }} onClose={onClose}>
    <div className="drawer-head"><h2 id="artifact-dialog-title">{artifact.title}</h2><button type="button" className="button button-quiet button-small" onClick={onClose}>{text(language, 'Close', 'ปิด')}</button></div>
    <ArtifactBody artifact={artifact} state={state} onStateChange={onStateChange} language={language} />
  </dialog>;
}
