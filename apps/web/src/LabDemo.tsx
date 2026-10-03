import type { CSSProperties } from 'react';
import { t, type Language } from './locales';

export type LabState = { diffusion: number; paused: boolean; seed: number };
export const INITIAL_LAB_STATE: LabState = { diffusion: 0.45, paused: true, seed: 0 };

export function LabDemo({ state, onChange, language }: { state: LabState; onChange: (state: LabState) => void; language: Language }) {
  const dots = Array.from({ length: 28 }, (_, index) => {
    const angle = index * 2.399963 + (state.seed % 1);
    const radius = 18 + ((index * 37) % 41) + state.diffusion * 22;
    const x = 160 + Math.cos(angle + state.seed) * radius;
    const y = 96 + Math.sin(angle + state.seed) * radius * 0.58;
    return <circle key={index} cx={x} cy={y} r={index % 5 === 0 ? 4 : 3} className="particle" style={{ animationDelay: `${(index % 8) * -0.21}s` } as CSSProperties} />;
  });

  return <section className="lab-card" aria-labelledby="lab-title" aria-label={t('labName', language)}>
    <div className="lab-heading">
      <div><p className="eyebrow">{t('labTitle', language)}</p><h3 id="lab-title">{t('labName', language)}</h3></div>
      <span className="lab-mark" aria-hidden="true">↗</span>
    </div>
    <p className="lab-intro">{t('labIntro', language)}</p>
    <div className={`particle-scene${state.paused ? ' is-paused' : ''}`}>
      <svg viewBox="0 0 320 192" role="img" aria-label={t('particleCaption', language)}>
        <defs><radialGradient id="field"><stop stopColor="var(--soft)" /><stop offset="1" stopColor="var(--surface)" /></radialGradient></defs>
        <rect width="320" height="192" rx="18" fill="url(#field)" />
        <path d="M160 12v168" stroke="var(--line)" strokeDasharray="3 6" />
        <circle cx="160" cy="96" r="36" fill="none" stroke="var(--line)" />
        {dots}
        <circle cx="160" cy="96" r="5" fill="var(--accent)" />
      </svg>
    </div>
    <label className="lab-range" htmlFor="diffusion">
      <span>{t('diffusion', language)}</span><output htmlFor="diffusion">{state.diffusion.toFixed(2)}</output>
      <input id="diffusion" type="range" min="0" max="1" step="0.05" value={state.diffusion} onChange={(event) => onChange({ ...state, diffusion: Number(event.target.value) })} />
    </label>
    <p className="lab-readout">{t('readout', language).replace('{value}', state.diffusion.toFixed(2))}</p>
    <div className="lab-actions">
      <button type="button" className="button button-small" onClick={() => onChange({ ...state, paused: !state.paused })}>{t(state.paused ? 'play' : 'pause', language)}</button>
      <button type="button" className="button button-quiet button-small" onClick={() => onChange({ ...INITIAL_LAB_STATE })}>{t('reset', language)}</button>
    </div>
    <p className="lab-disclaimer">{t('illustrative', language)}</p>
  </section>;
}
