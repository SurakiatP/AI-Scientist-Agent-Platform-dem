import type { DecisionRequiredPayload, PendingDecisionView, RunEvent, RunView } from '../../../contracts/api-types';
import { useEffect, useState } from 'react';
import { apiCodeLabel } from './api';
import { useAppPreferences } from './App';

type Language = 'th' | 'en';
const text = (language: Language, en: string, th: string) => language === 'th' ? th : en;
export type DecisionChoice = 'extend' | 'verified_result' | 'retry' | 'stop' | 'confirm_usage';

const stateLabels: Record<RunView['state'], [string, string]> = {
  planning: ['Preparing plan', 'กำลังเตรียมแผน'],
  awaiting_approval: ['Waiting approval', 'รอการอนุมัติจากคุณ'],
  queued: ['Queued to start', 'อยู่ในคิวเพื่อเริ่มงาน'],
  running: ['Running', 'กำลังทำงาน'],
  waiting_input: ['Waiting for your decision', 'รอการตัดสินใจจากคุณ'],
  recovering: ['Recovering', 'กำลังกู้คืน'],
  stopping: ['Stopping, waiting for confirmation', 'กำลังหยุด รอการยืนยัน'],
  completed: ['Completed', 'เสร็จสมบูรณ์'],
  failed: ['Failed', 'ไม่สำเร็จ'],
  canceled: ['Stopped', 'หยุดแล้ว'],
  rejected: ['Rejected', 'ถูกปฏิเสธ'],
};

const stageLabels: Record<string, [string, string]> = {
  search_literature: ['Search literature', 'ค้นหางานวิจัย'],
  verify_references: ['Verify references', 'ตรวจสอบแหล่งอ้างอิง'],
  synthesize_evidence: ['Synthesize evidence', 'สังเคราะห์หลักฐาน'],
};

export const stageLabel = (stage: string, language: Language) =>
  stageLabels[stage]?.[language === 'th' ? 1 : 0] ?? stage;

const TERMINAL = ['completed', 'failed', 'canceled', 'rejected'];

export function RunProgress({
  run,
  events,
  pendingDecisions,
  connected,
  onStop,
  stopPending = false,
  onDecision,
  onRetry,
}: {
  run: RunView;
  events: RunEvent[];
  pendingDecisions: PendingDecisionView[];
  connected: boolean;
  onStop: () => void;
  stopPending?: boolean;
  onDecision?: (decision: DecisionRequiredPayload, choice: DecisionChoice, usageTokens?: number) => void;
  onRetry?: () => void;
}) {
  const { language } = useAppPreferences();
  const terminal = TERMINAL.includes(run.state);
  const stopping = !terminal && (run.state === 'stopping' || stopPending);
  const completed = new Set(events.flatMap((event) =>
    event.kind === 'stage.completed' && event.payload.outcome !== 'failed' ? [event.payload.stage] : []));
  const started = events.flatMap((event) => event.kind === 'stage.started' ? [event.payload.stage] : []);
  const stages = [...new Set([...started, ...completed])];
  const failedStage = events.flatMap((event) =>
    event.kind === 'stage.completed' && event.payload.outcome === 'failed' ? [event.payload.stage] : []).at(-1) ?? run.stage;
  const current = run.state === 'running' ? run.stage ?? started.filter((stage) => !completed.has(stage)).at(-1) : null;
  // The event stream is history. The owner endpoint supplies the currently actionable decision.
  const decision = pendingDecisions[0];
  const [usage, setUsage] = useState('');
  const [usageAcknowledged, setUsageAcknowledged] = useState(false);
  const usageCap = decision?.operation_reserved_tokens ?? 0;
  const usageValue = Number(usage);
  const usageValid = /^\d+$/.test(usage) && usageValue <= usageCap;
  const confirmable = run.state === 'canceled'
    && decision?.reason === 'unknown_outcome'
    && decision.operation_reserved_tokens !== null
    && decision.operation_reserved_tokens !== undefined;

  useEffect(() => {
    setUsage('');
    setUsageAcknowledged(false);
  }, [decision?.decision_id]);

  const label = stateLabels[run.state][language === 'th' ? 1 : 0];

  return (
    <section className="run-progress" role="region" aria-label={text(language, 'Research progress', 'ความคืบหน้าการวิจัย')}>
      <p className={`connection-badge${connected ? '' : ' is-offline'}`}>
        {connected ? text(language, 'Connected', 'เชื่อมต่อแล้ว') : text(language, 'Connection lost. Showing last confirmed status; the run may still be active.', 'การเชื่อมต่อขาดหาย แสดงสถานะล่าสุดที่ยืนยันแล้ว งานอาจยังทำงานอยู่')}
      </p>
      <h2>{stopping ? stateLabels.stopping[language === 'th' ? 1 : 0] : label}</h2>
      <p role="status">
        {text(language, `${completed.size} stages completed`, `เสร็จสิ้น ${completed.size} ขั้นตอน`)}
        {current ? ` · ${text(language, 'Current stage', 'ขั้นตอนปัจจุบัน')}: ${stageLabel(current, language)}` : ''}
      </p>
      {stages.length > 0 && (
        <ol>{stages.map((stage) => (
          <li key={stage}>
            {stageLabel(stage, language)} · {completed.has(stage) ? text(language, 'completed', 'เสร็จสิ้น') : text(language, 'in progress', 'กำลังทำ')}
          </li>
        ))}</ol>
      )}

      {run.state === 'waiting_input' && !stopping && decision && (
        <div role="group" aria-label={text(language, 'Decision needed', 'ต้องตัดสินใจ')}>
          {decision.reason === 'budget_exhausted' && (
            <>
              <p>
                {text(language, 'The approved usage limit has been reached.', 'ถึงขีดจำกัดการใช้งานที่อนุมัติแล้ว')}
                {(decision.required_tokens ?? 0) > 0 && <> {text(language, `Required to continue: ${decision.required_tokens} more tokens.`, `ต้องเพิ่มอีก ${decision.required_tokens} โทเคนจึงจะดำเนินการต่อได้`)}</>}
                {(decision.required_elapsed_ms ?? 0) > 0 && <> {text(language, `Required: ${Math.ceil(decision.required_elapsed_ms! / 1000)} more seconds.`, `ต้องเพิ่มอีก ${Math.ceil(decision.required_elapsed_ms! / 1000)} วินาที`)}</>}
              </p>
              {((decision.required_tokens ?? 0) > 0 || (decision.required_elapsed_ms ?? 0) > 0) && (
                <button type="button" className="button button-small" onClick={() => onDecision?.(decision, 'extend')}>
                  {text(language, 'Extend limit and continue', 'ขยายขีดจำกัดและดำเนินการต่อ')}
                </button>
              )}
            </>
          )}
          {decision.reason === 'unknown_outcome' && (
            <>
              <p>{text(language, `An external operation may or may not have completed. ${run.reserved_tokens} reserved tokens are retained until this decision is resolved.`, `ขั้นตอนภายนอกอาจเสร็จหรือไม่เสร็จก็ได้ โทเคนที่สำรองไว้ ${run.reserved_tokens} จะถูกกันไว้จนกว่าจะแก้ไข`)}</p>
              <button type="button" className="button button-small" disabled aria-describedby="verified-result-note">
                {text(language, 'Use a verified result', 'ใช้ผลลัพธ์ที่ตรวจสอบแล้ว')}
              </button>
              <span id="verified-result-note">{text(language, 'Choosing a verified result is not available yet.', 'การเลือกผลลัพธ์ที่ตรวจสอบแล้วยังไม่พร้อมใช้งาน')}</span>
              <button type="button" className="button button-quiet button-small" onClick={() => onDecision?.(decision, 'retry')}>
                {text(language, 'Retry (may duplicate cost)', 'ลองใหม่ (อาจมีค่าใช้จ่ายซ้ำ)')}
              </button>
              <button type="button" className="button button-quiet button-small" onClick={() => onDecision?.(decision, 'stop')}>
                {text(language, 'Stop without retrying', 'หยุดโดยไม่ลองใหม่')}
              </button>
            </>
          )}
          {decision.reason !== 'budget_exhausted' && decision.reason !== 'unknown_outcome' && (
            <p>{text(language, 'Your input is needed before the work can continue.', 'ต้องได้รับการตรวจสอบจากคุณก่อนจึงจะทำต่อได้')}</p>
          )}
        </div>
      )}

      {confirmable && decision && (
        <div role="group" aria-label={text(language, 'Confirm usage', 'ยืนยันการใช้งาน')}>
          <p>
            {text(language,
              `This operation has ${usageCap} reserved tokens. Check the provider usage records for this operation and enter its actual usage.`,
              `ขั้นตอนนี้สำรองไว้ ${usageCap} โทเคน โปรดตรวจสอบบันทึกการใช้งานของผู้ให้บริการสำหรับขั้นตอนนี้ แล้วระบุจำนวนที่ใช้จริง`)}
          </p>
          <label>
            {text(language, 'Tokens actually used', 'โทเคนที่ใช้จริง')}
            <input
              type="number"
              min={0}
              max={usageCap}
              step={1}
              value={usage}
              onChange={(event) => {
                setUsage(event.target.value);
                setUsageAcknowledged(false);
              }}
            />
          </label>
          <label>
            <input
              type="checkbox"
              checked={usageAcknowledged}
              onChange={(event) => setUsageAcknowledged(event.target.checked)}
            />
            {text(language,
              'I checked the provider records for this operation and confirm the amount above is actual usage.',
              'ฉันตรวจสอบบันทึกของผู้ให้บริการสำหรับขั้นตอนนี้แล้ว และยืนยันว่าจำนวนข้างต้นคือการใช้งานจริง')}
          </label>
          <button
            type="button"
            className="button button-small"
            disabled={!usageValid || !usageAcknowledged}
            onClick={() => onDecision?.(decision, 'confirm_usage', usageValue)}
          >
            {text(language, 'Confirm usage', 'ยืนยันการใช้งานจริง')}
          </button>
        </div>
      )}

      {terminal && run.state !== 'completed' && (
        <p role="alert">
          {failedStage ? `${stageLabel(failedStage, language)}: ` : ''}
          {run.error_code ? apiCodeLabel(run.error_code, language) : label}
          {run.artifacts.length > 0 ? ` · ${text(language, 'Partial outputs are kept below.', 'เก็บผลลัพธ์บางส่วนไว้ด้านล่าง')}` : ''}
        </p>
      )}
      {!terminal && (
        <button type="button" className="button button-quiet button-small" disabled={stopping} onClick={onStop}>
          {stopping ? text(language, 'Stopping…', 'กำลังหยุด…') : text(language, 'Stop', 'หยุด')}
        </button>
      )}
      {(run.state === 'failed' || run.state === 'canceled') && onRetry && (
        <button type="button" className="button button-small" onClick={onRetry}>
          {text(language, 'Review and retry', 'ตรวจทานและลองใหม่')}
        </button>
      )}
    </section>
  );
}
