import type { DecisionRequiredPayload, RunEvent, RunView } from '../../../contracts/api-types';
import { apiCodeLabel } from './api';
import { useAppPreferences } from './App';

type Language = 'th' | 'en';
const text = (language: Language, en: string, th: string) => language === 'th' ? th : en;
export type DecisionChoice = 'extend' | 'verified_result' | 'retry' | 'stop';

const stateLabels: Record<RunView['state'], [string, string]> = {
  planning: ['Preparing a plan', 'กำลังเตรียมแผน'], awaiting_approval: ['Waiting for your approval', 'รอการอนุมัติจากคุณ'],
  queued: ['Queued to start', 'อยู่ในคิวเพื่อเริ่มงาน'], running: ['Running', 'กำลังทำงาน'], waiting_input: ['Waiting for your decision', 'รอการตัดสินใจจากคุณ'],
  recovering: ['Recovering', 'กำลังกู้คืน'], stopping: ['Stopping, waiting for confirmation', 'กำลังหยุด รอการยืนยัน'],
  completed: ['Completed', 'เสร็จสมบูรณ์'], failed: ['Failed', 'ไม่สำเร็จ'], canceled: ['Stopped', 'หยุดแล้ว'], rejected: ['Rejected', 'ถูกปฏิเสธ'],
};
const stageLabels: Record<string, [string, string]> = {
  search_literature: ['Search literature', 'ค้นหางานวิจัย'], verify_references: ['Verify references', 'ตรวจสอบแหล่งอ้างอิง'], synthesize_evidence: ['Synthesize evidence', 'สังเคราะห์หลักฐาน'],
};
export const stageLabel = (stage: string, language: Language) => stageLabels[stage]?.[language === 'th' ? 1 : 0] ?? stage;
const TERMINAL = ['completed', 'failed', 'canceled', 'rejected'];

export function RunProgress({ run, events, connected, onStop, stopPending = false, onDecision, onRetry }: {
  run: RunView; events: RunEvent[]; connected: boolean; onStop: () => void; stopPending?: boolean;
  onDecision?: (decision: DecisionRequiredPayload, choice: DecisionChoice) => void; onRetry?: () => void;
}) {
  const { language } = useAppPreferences();
  const terminal = TERMINAL.includes(run.state);
  const stopping = !terminal && (run.state === 'stopping' || stopPending);
  const completed = new Set(events.flatMap((e) => e.kind === 'stage.completed' && e.payload.outcome !== 'failed' ? [e.payload.stage] : []));
  const started = events.flatMap((e) => e.kind === 'stage.started' ? [e.payload.stage] : []);
  const stages = [...new Set([...started, ...completed])];
  const failedStage = events.flatMap((e) => e.kind === 'stage.completed' && e.payload.outcome === 'failed' ? [e.payload.stage] : []).at(-1) ?? run.stage;
  const current = run.state === 'running' ? run.stage ?? started.filter((s) => !completed.has(s)).at(-1) : null;
  const decision = events.filter((e) => e.kind === 'decision.required').at(-1)?.payload as DecisionRequiredPayload | undefined;
  const label = stateLabels[run.state][language === 'th' ? 1 : 0];

  return <section className="run-progress" role="region" aria-label={text(language, 'Research progress', 'ความคืบหน้าการวิจัย')}>
    <p className={`connection-badge${connected ? '' : ' is-offline'}`}>{connected ? text(language, 'Connected', 'เชื่อมต่อแล้ว') : text(language, 'Connection lost. Showing the last confirmed status; the run may still be active.', 'การเชื่อมต่อขาดหาย แสดงสถานะล่าสุดที่ยืนยันแล้ว งานอาจยังทำงานอยู่')}</p>
    <h2>{stopping ? stateLabels.stopping[language === 'th' ? 1 : 0] : label}</h2>
    <p role="status">{text(language, `${completed.size} stages completed`, `เสร็จสิ้น ${completed.size} ขั้นตอน`)}{current ? ` · ${text(language, 'Current stage', 'ขั้นตอนปัจจุบัน')}: ${stageLabel(current, language)}` : ''}</p>
    {stages.length > 0 && <ol>{stages.map((s) => <li key={s}>{stageLabel(s, language)} · {completed.has(s) ? text(language, 'completed', 'เสร็จสิ้น') : text(language, 'in progress', 'กำลังทำ')}</li>)}</ol>}
    {run.state === 'waiting_input' && !stopping && <div role="group" aria-label={text(language, 'Decision needed', 'ต้องตัดสินใจ')}>
      {decision?.reason === 'budget_exhausted' && <><p>{text(language, 'The approved usage limit has been reached.', 'ถึงขีดจำกัดการใช้งานที่อนุมัติแล้ว')}
        {(decision.required_tokens ?? 0) > 0 && <> {text(language, `Required to continue: ${decision.required_tokens} more tokens.`, `ต้องเพิ่มอีก ${decision.required_tokens} โทเคนจึงจะดำเนินการต่อได้`)}</>}
        {(decision.required_elapsed_ms ?? 0) > 0 && <> {text(language, `Required: ${Math.ceil(decision.required_elapsed_ms! / 1000)} more seconds.`, `ต้องเพิ่มอีก ${Math.ceil(decision.required_elapsed_ms! / 1000)} วินาที`)}</>}</p>
        {((decision.required_tokens ?? 0) > 0 || (decision.required_elapsed_ms ?? 0) > 0) && <button type="button" className="button button-small" onClick={() => onDecision?.(decision, 'extend')}>{text(language, 'Extend limit and continue', 'ขยายขีดจำกัดและดำเนินการต่อ')}</button>}</>}
      {decision?.reason === 'unknown_outcome' && <><p>{text(language, `An external step may or may not have completed. ${run.reserved_tokens} reserved tokens are retained until this is resolved.`, `ขั้นตอนภายนอกอาจเสร็จหรือไม่เสร็จก็ได้ โทเคนที่สำรองไว้ ${run.reserved_tokens} จะถูกกันไว้จนกว่าจะแก้ไข`)}</p>
        <button type="button" className="button button-small" disabled aria-describedby="verified-result-note">{text(language, 'Use a verified result', 'ใช้ผลลัพธ์ที่ตรวจสอบแล้ว')}</button>
        <span id="verified-result-note"> {text(language, 'Choosing a verified result is not available yet.', 'การเลือกผลลัพธ์ที่ตรวจสอบแล้วยังไม่พร้อมใช้งาน')}</span>
        <button type="button" className="button button-quiet button-small" onClick={() => onDecision?.(decision, 'retry')}>{text(language, 'Retry (may duplicate cost)', 'ลองใหม่ (อาจมีค่าใช้จ่ายซ้ำ)')}</button>
        <button type="button" className="button button-quiet button-small" onClick={() => onDecision?.(decision, 'stop')}>{text(language, 'Stop without retrying', 'หยุดโดยไม่ลองใหม่')}</button></>}
      {decision && decision.reason !== 'budget_exhausted' && decision.reason !== 'unknown_outcome' && <p>{text(language, 'Your review is needed before work can continue.', 'ต้องได้รับการตรวจสอบจากคุณก่อนจึงจะทำต่อได้')}</p>}
    </div>}
    {terminal && run.state !== 'completed' && <p role="alert">{failedStage ? `${stageLabel(failedStage, language)}: ` : ''}{run.error_code ? apiCodeLabel(run.error_code, language) : label}{run.artifacts.length > 0 ? ` · ${text(language, 'Partial outputs are kept below.', 'เก็บผลลัพธ์บางส่วนไว้ด้านล่าง')}` : ''}</p>}
    {!terminal && <button type="button" className="button button-quiet button-small" disabled={stopping} onClick={onStop}>{stopping ? text(language, 'Stopping…', 'กำลังหยุด…') : text(language, 'Stop', 'หยุด')}</button>}
    {(run.state === 'failed' || run.state === 'canceled') && onRetry && <button type="button" className="button button-small" onClick={onRetry}>{text(language, 'Review and retry', 'ตรวจทานและลองใหม่')}</button>}
  </section>;
}
