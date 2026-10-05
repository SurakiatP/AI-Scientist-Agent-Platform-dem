import { useEffect, useRef, useState } from 'react';
import type { PeerReleaseSpec } from '../../../contracts/api-types';
import { request } from './api';

type Language = 'en' | 'th';
type Peer = { peer_id: string; endpoint: string; endpoint_fingerprint: string; configured: boolean };
const tx = (language: Language, en: string, th: string) => language === 'th' ? th : en;
const digest = (value: unknown) => typeof value === 'string' && /^[a-f0-9]{64}$/i.test(value);
const nonblank = (value: unknown) => typeof value === 'string' && value.trim().length > 0;
const bounded = (value: unknown, min: number, max: number) => Number.isInteger(value) && Number(value) >= min && Number(value) <= max;

function reviewable(value: unknown): value is PeerReleaseSpec {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const r = value as PeerReleaseSpec;
  return nonblank(r.release_id) && nonblank(r.peer_id) && nonblank(r.purpose) && nonblank(r.message_id)
    && digest(r.endpoint_fingerprint) && digest(r.input_snapshot_digest) && digest(r.parameters_sha256)
    && !!r.approved_parameters && typeof r.approved_parameters === 'object' && !Array.isArray(r.approved_parameters)
    && (r.method === undefined || r.method === 'SendMessage')
    && (r.allow_get_task === undefined || typeof r.allow_get_task === 'boolean')
    && bounded(r.request_bytes_limit, 1, 1048576) && bounded(r.timeout_ms, 1, 30000)
    && bounded(r.reserved_tokens, 0, 1000000) && bounded(r.reconciliation_limit, 1, 10)
    && (r.data_refs === undefined || (Array.isArray(r.data_refs) && r.data_refs.length <= 100
      && r.data_refs.every((d) => d && (d.kind === 'file' || d.kind === 'finding') && nonblank(d.record_id) && digest(d.version_digest))));
}

function endpointMatches(peer: Peer, release: PeerReleaseSpec): boolean {
  try {
    const url = new URL(peer.endpoint);
    return peer.configured === true && peer.peer_id === release.peer_id
      && peer.endpoint_fingerprint.toLowerCase() === release.endpoint_fingerprint.toLowerCase()
      && url.protocol === 'https:' && !url.username && !url.password
      && !url.search && !url.hash && url.pathname === '/';
  } catch { return false; }
}

function PayloadDialog({ payload, language, onClose }: { payload: string; language: Language; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { ref.current?.showModal(); }, []);
  return <dialog className="peer-payload-dialog" ref={ref} onClose={onClose} aria-label={tx(language, 'Exact approved payload', 'ข้อมูลที่อนุมัติให้ส่งอย่างครบถ้วน')}>
    <header><h3>{tx(language, 'Exact approved payload', 'ข้อมูลที่อนุมัติให้ส่งอย่างครบถ้วน')}</h3>
      <button className="button button-quiet button-small" onClick={() => ref.current?.close()}>{tx(language, 'Close', 'ปิด')}</button></header>
    <pre>{payload}</pre>
  </dialog>;
}

export function PeerReleaseReview({ releases, reviewKey, language, onReady }: {
  releases: unknown; reviewKey: string; language: Language; onReady: (key: string, ready: boolean) => void;
}) {
  const [metadata, setMetadata] = useState<{ key: string; peers: Peer[] | null; failed: boolean }>({ key: '', peers: null, failed: false });
  const [expanded, setExpanded] = useState<string | null>(null);
  const items = Array.isArray(releases) ? releases : [];
  const valid = Array.isArray(releases) && items.length > 0 && items.length <= 20 && items.every(reviewable);
  const current = metadata.key === reviewKey;
  const ready = valid && current && !!metadata.peers && items.every((r: PeerReleaseSpec) => metadata.peers!.some((p) => endpointMatches(p, r)));
  useEffect(() => {
    const controller = new AbortController();
    setExpanded(null);
    void request<Peer[]>('/api/v1/peers', { signal: controller.signal }).then((peers) => {
      if (!controller.signal.aborted) setMetadata({ key: reviewKey, peers: Array.isArray(peers) ? peers : null, failed: !Array.isArray(peers) });
    }).catch(() => { if (!controller.signal.aborted) setMetadata({ key: reviewKey, peers: null, failed: true }); });
    return () => controller.abort();
  }, [reviewKey]);
  useEffect(() => { onReady(reviewKey, ready); }, [reviewKey, ready, onReady]);
  const failed = !valid || (current && (metadata.failed || (metadata.peers !== null && !ready)));
  return <section className="peer-release-review" aria-label={tx(language, 'Peer data release review', 'ตรวจทานการส่งข้อมูลให้ผู้ร่วมวิจัย')}>
    <h3>{tx(language, 'Data shared with research peers', 'ข้อมูลที่จะส่งให้ผู้ร่วมวิจัย')}</h3>
    <p>{tx(language, 'Approval permits only the payload and limits shown below. Credentials are handled separately.', 'การอนุมัติให้สิทธิ์เฉพาะข้อมูลและขีดจำกัดที่แสดงด้านล่าง ข้อมูลรับรองจัดการแยกต่างหาก')}</p>
    {failed ? <p role="alert">{tx(language, 'Peer details could not be verified. Refresh the plan and check peer settings before approving.', 'ไม่สามารถยืนยันข้อมูลผู้ร่วมวิจัยได้ โปรดโหลดแผนใหม่และตรวจสอบการตั้งค่าก่อนอนุมัติ')}</p>
      : !ready && <p role="status">{tx(language, 'Verifying recipient details…', 'กำลังตรวจสอบข้อมูลผู้รับ…')}</p>}
    {valid && items.map((r: PeerReleaseSpec) => {
      const peer = current ? metadata.peers?.find((p) => endpointMatches(p, r)) : undefined;
      const payload = JSON.stringify(r.approved_parameters, null, 2);
      return <article className="peer-release-card" key={r.release_id}>
        <h4>{r.purpose}</h4>
        <dl><dt>{tx(language, 'Recipient', 'ผู้รับ')}</dt><dd><span>{peer?.endpoint ?? tx(language, 'Unverified recipient', 'ผู้รับยังไม่ได้รับการยืนยัน')}</span><small>{r.peer_id}</small></dd>
          <dt>{tx(language, 'Endpoint fingerprint', 'Fingerprint ของปลายทาง')}</dt><dd className="peer-digest">{r.endpoint_fingerprint}</dd>
          <dt>{tx(language, 'Input snapshot', 'เวอร์ชันข้อมูลนำเข้า')}</dt><dd className="peer-digest">{r.input_snapshot_digest}</dd>
          <dt>{tx(language, 'Allowed request', 'คำขอที่อนุญาต')}</dt><dd>SendMessage · {r.allow_get_task ? tx(language, 'GetTask status reads allowed; no automatic resubmission', 'อนุญาต GetTask เพื่ออ่านสถานะ โดยไม่ส่งงานซ้ำอัตโนมัติ') : tx(language, 'No automatic status reads', 'ไม่อ่านสถานะอัตโนมัติ')}</dd>
          <dt>{tx(language, 'Limits', 'ขีดจำกัด')}</dt><dd>{r.request_bytes_limit} {tx(language, 'bytes', 'ไบต์')} · {r.timeout_ms} {tx(language, 'ms', 'มิลลิวินาที')} · {r.reserved_tokens} {tx(language, 'tokens', 'โทเคน')} · {r.allow_get_task ? r.reconciliation_limit : 0} {tx(language, 'status reads', 'ครั้งในการอ่านสถานะ')}</dd>
          <dt>{tx(language, 'Message identity', 'รหัสข้อความ')}</dt><dd>{r.message_id}</dd>
          <dt>{tx(language, 'Payload SHA-256', 'SHA-256 ของข้อมูลที่ส่ง')}</dt><dd className="peer-digest">{r.parameters_sha256}</dd></dl>
        <h5>{tx(language, 'Data versions', 'เวอร์ชันข้อมูลที่ส่ง')}</h5>
        {(r.data_refs ?? []).length === 0 ? <p>{tx(language, 'No file or finding references.', 'ไม่มีการอ้างอิงไฟล์หรือข้อค้นพบ')}</p> : <ul>{r.data_refs!.map((d) => <li key={`${d.kind}:${d.record_id}`}>
          {d.kind === 'file' ? tx(language, 'File', 'ไฟล์') : tx(language, 'Finding', 'ข้อค้นพบ')} · {d.record_id}<code className="peer-digest">{d.version_digest}</code>
        </li>)}</ul>}
        <details><summary>{tx(language, 'Exact approved payload', 'ข้อมูลที่อนุมัติให้ส่งอย่างครบถ้วน')}</summary><pre>{payload}</pre></details>
        <button className="button button-quiet button-small" onClick={() => setExpanded(r.release_id)}>{tx(language, 'Expand payload', 'ขยายข้อมูลที่ส่ง')}</button>
        {expanded === r.release_id && <PayloadDialog payload={payload} language={language} onClose={() => setExpanded(null)} />}
      </article>;
    })}
  </section>;
}
