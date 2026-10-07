import type { CSSProperties } from 'react';

export type LabState = { temperature: number; paused: boolean };
export const INITIAL_LAB_STATE: LabState = { temperature: 300, paused: true };

export function LabDemo({ state, onChange, language }: { state: LabState; onChange: (state: LabState) => void; language: 'th' | 'en' }) {
  const th = language === 'th';
  const spread = Math.sqrt(state.temperature / 300);
  const particles = Array.from({ length: 32 }, (_, index) => {
    const angle = index * 2.399963;
    const radius = (11 + ((index * 37) % 45)) * spread;
    const x = 180 + Math.cos(angle) * radius;
    const y = 89 + Math.sin(angle) * radius * 0.55;
    return { x, y, radius: index % 5 === 0 ? 4 : 3, delay: `${(index % 8) * -0.19}s` };
  });
  const bins = [0, 0, 0, 0, 0];
  for (const particle of particles) bins[Math.min(4, Math.floor(particle.x / 72))] += 1;
  const spreadWidth = ((Math.max(...particles.map(({ x }) => x)) - Math.min(...particles.map(({ x }) => x))) / 360 * 100).toFixed(1);
  const setTemperature = (temperature: number) => onChange({ ...state, temperature });

  return <section className="lab-card" aria-labelledby="lab-title" aria-label={th ? 'ห้องทดลองการแพร่' : 'Diffusion lab'}>
    <div className="lab-heading"><span className="notebook-tag">INTERACTIVE LAB 001</span><span className="lab-status" role="status">{state.paused ? (th ? 'หยุดชั่วคราว' : 'Paused') : (th ? 'กำลังเล่น' : 'Playing')}</span></div>
    <h3 id="lab-title">{th ? 'การแพร่ของอนุภาค' : 'Particle diffusion'}</h3>
    <p className="lab-instruction">{th ? 'ลองปรับอุณหภูมิแล้วสังเกตการกระจาย' : 'Adjust temperature and observe particle spread.'}</p>
    <div className={`lab-scene${state.paused ? ' is-paused' : ''}`}>
      <svg viewBox="0 0 360 178" role="img" aria-label={th ? 'อนุภาคกระจายออกจากจุดศูนย์กลาง' : 'Particles spread away from the center'}>
        <rect width="360" height="178" rx="12" className="lab-scene-bg" />
        <path d="M180 12v154" className="lab-axis" />
        <circle cx="180" cy="89" r="29" className="lab-center-ring" />
        {particles.map((particle, index) => <circle key={index} cx={particle.x} cy={particle.y} r={particle.radius} className="lab-particle" style={{ animationDelay: particle.delay } as CSSProperties} />)}
        <circle cx="180" cy="89" r="4" className="lab-center" />
      </svg>
    </div>
    <label className="lab-temperature" htmlFor="lab-temperature">
      <span>{th ? 'อุณหภูมิ' : 'Temperature'}</span><output id="lab-temperature-value" htmlFor="lab-temperature">{state.temperature} K</output>
      <input id="lab-temperature" type="range" min="200" max="500" step="10" value={state.temperature} aria-valuetext={`${state.temperature} K`} onChange={(event) => setTemperature(Number(event.target.value))} />
    </label>
    <div className="lab-presets" aria-label={th ? 'ค่าที่ตั้งไว้' : 'Temperature presets'}>
      {[200, 300, 500].map((value) => <button key={value} type="button" aria-pressed={state.temperature === value} onClick={() => setTemperature(value)}>{value} K</button>)}
    </div>
    <div className="lab-measures"><div><span>{th ? 'การเคลื่อนที่สัมพัทธ์' : 'Relative motion'}</span><output>{spread.toFixed(2)}×</output></div><div><span>{th ? 'ความกว้างการกระจาย' : 'Spread width'}</span><output>{spreadWidth}%</output></div></div>
    <figure className="lab-distribution"><svg viewBox="0 0 220 42" role="img" aria-label={th ? 'ฮิสโตแกรมแสดงการกระจายของอนุภาค' : 'Histogram of particle distribution'}>{bins.map((count, index) => <rect key={index} x={9 + index * 42} y={38 - count * 3} width="30" height={count * 3} rx="2" className="lab-bin"/>)}<path d="M6 39H214" className="lab-bin-axis"/></svg><figcaption>{th ? 'การกระจายของอนุภาค' : 'Particle distribution'}</figcaption></figure>
    <p className="lab-model-note">{th ? 'แบบจำลองภาพประกอบอย่างง่าย ไม่ใช่ผลการทดลอง' : 'Illustrative model only; these are not experimental results.'}</p>
    <div className="lab-controls">
      <button type="button" className="lab-play" aria-pressed={!state.paused} onClick={() => onChange({ ...state, paused: !state.paused })}>{state.paused ? (th ? 'เล่น' : 'Play') : (th ? 'หยุดชั่วคราว' : 'Pause')}</button>
      <button type="button" className="lab-reset" onClick={() => onChange({ ...INITIAL_LAB_STATE })}>{th ? 'เริ่มใหม่' : 'Reset'}</button>
    </div>
  </section>;
}
