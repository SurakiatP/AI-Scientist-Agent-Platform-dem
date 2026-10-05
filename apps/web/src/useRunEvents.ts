import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react';
import type { RunEvent, RunView } from '../../../contracts/api-types';

const EVENT_KINDS: RunEvent['kind'][] = [
  'plan.ready',
  'run.state',
  'stage.started',
  'stage.completed',
  'artifact.ready',
  'decision.required',
  'usage.updated',
];
const TERMINAL_STATES = new Set<RunView['state']>(['completed', 'failed', 'canceled', 'rejected']);

export type RunEventsOptions = {
  retryBaseDelayMs?: number;
  maxRetryDelayMs?: number;
  maxReconnectAttempts?: number;
};

export type RunEventsState = {
  run: RunView | null;
  setRun: Dispatch<SetStateAction<RunView | null>>;
  events: RunEvent[];
  connected: boolean;
  reconnecting: boolean;
};

type EventPage = { events: RunEvent[]; latest_cursor: number };
type SnapshotOrPage = { status: number; body: unknown };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function terminal(state: string | undefined): boolean {
  return !!state && TERMINAL_STATES.has(state as RunView['state']);
}

async function getJson(path: string, signal: AbortSignal): Promise<SnapshotOrPage> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    redirect: 'error',
    headers: { Accept: 'application/json' },
    signal,
  });
  const body: unknown = await response.json().catch(() => null);
  return { status: response.status, body };
}

function parseEvent(value: unknown, runId: string, kind?: string): RunEvent | null {
  if (!isRecord(value) || value.run_id !== runId || !Number.isSafeInteger(value.sequence) || Number(value.sequence) < 1) return null;
  if (!EVENT_KINDS.includes(value.kind as RunEvent['kind']) || (kind && value.kind !== kind)) return null;
  if (!isRecord(value.payload) || typeof value.revision !== 'number' || typeof value.occurred_at !== 'string') return null;
  return value as unknown as RunEvent;
}

export function useRunEvents(runId: string | null, options: RunEventsOptions = {}): RunEventsState {
  const [run, setRun] = useState<RunView | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const latestCursor = useRef(0);
  const runRef = useRef<RunView | null>(null);

  useEffect(() => { runRef.current = run; }, [run]);

  useEffect(() => {
    let disposed = false;
    let source: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let retryCount = 0;
    let busy = false;
    let cursor = 0;
    const controllers = new Set<AbortController>();
    const retryBaseDelayMs = Math.max(0, options.retryBaseDelayMs ?? 500);
    const maxRetryDelayMs = Math.max(retryBaseDelayMs, options.maxRetryDelayMs ?? 10_000);
    const maxReconnectAttempts = Math.max(0, options.maxReconnectAttempts ?? 8);
    const isCurrent = () => !disposed;

    const makeController = () => {
      const controller = new AbortController();
      controllers.add(controller);
      return controller;
    };

    const acceptSnapshot = (snapshot: RunView) => {
      if (!isCurrent() || snapshot.run_id !== runId) return;
      latestCursor.current = Math.max(latestCursor.current, snapshot.latest_cursor);
      if (runRef.current?.run_id === runId && (runRef.current.latest_cursor > snapshot.latest_cursor || runRef.current.revision > snapshot.revision)) return;
      runRef.current = snapshot;
      setRun((current) => {
        if (current?.run_id === runId && current.latest_cursor > snapshot.latest_cursor) return current;
        if (current?.run_id === runId && current.revision > snapshot.revision) return current;
        return snapshot;
      });
    };

    const acceptEvent = (event: RunEvent) => {
      if (!isCurrent() || event.run_id !== runId) return;
      cursor = Math.max(cursor, event.sequence);
      latestCursor.current = Math.max(latestCursor.current, event.sequence);
      setEvents((current) => {
        if (current.some((item) => item.sequence === event.sequence)) return current;
        return [...current, event].sort((a, b) => a.sequence - b.sequence);
      });
      const currentRun = runRef.current;
      if (currentRun?.run_id === runId && event.sequence >= currentRun.latest_cursor) {
        let next: RunView = { ...currentRun, latest_cursor: Math.max(currentRun.latest_cursor, event.sequence), revision: Math.max(currentRun.revision, event.revision) };
        if (event.kind === 'run.state') next = { ...next, state: event.payload.state };
        if (event.kind === 'stage.started') next = { ...next, stage: event.payload.stage };
        runRef.current = next;
        setRun(next);
      }
    };

    const snapshot = async (): Promise<RunView> => {
      const controller = makeController();
      try {
        const result = await getJson(`/api/v1/runs/${encodeURIComponent(runId!)}`, controller.signal);
        if (result.status < 200 || result.status >= 300 || !isRecord(result.body) || result.body.run_id !== runId) {
          throw new Error('Unable to load run snapshot');
        }
        const value = result.body as unknown as RunView;
        acceptSnapshot(value);
        return value;
      } finally {
        controllers.delete(controller);
      }
    };

    const replay = async (startAfter: number): Promise<number> => {
      let after = Math.max(0, startAfter);
      let pages = 0;
      while (isCurrent() && pages < 100) {
        pages += 1;
        const controller = makeController();
        let result: SnapshotOrPage;
        try {
          result = await getJson(`/api/v1/runs/${encodeURIComponent(runId!)}/event-page?after=${after}&limit=100`, controller.signal);
        } finally {
          controllers.delete(controller);
        }
        if (!isCurrent()) return after;
        if (result.status === 410 && isRecord(result.body) && isRecord(result.body.snapshot)) {
          acceptSnapshot(result.body.snapshot as unknown as RunView);
          cursor = 0;
          after = 0;
          continue;
        }
        if (result.status < 200 || result.status >= 300 || !isRecord(result.body) || !Array.isArray(result.body.events)) {
          throw new Error('Unable to load run events');
        }
        const page = result.body as unknown as EventPage;
        let nextAfter = after;
        for (const value of page.events) {
          const event = parseEvent(value, runId!);
          if (!event) continue;
          acceptEvent(event);
          nextAfter = Math.max(nextAfter, event.sequence);
        }
        latestCursor.current = Math.max(latestCursor.current, page.latest_cursor);
        if (nextAfter <= after) {
          if (after < page.latest_cursor) throw new Error('Run event replay made no progress');
          return after;
        }
        after = nextAfter;
        if (after >= page.latest_cursor) return after;
      }
      return after;
    };

    const closeSource = () => {
      if (source) {
        source.close();
        source = null;
      }
      setConnected(false);
    };

    const openSource = () => {
      if (!isCurrent() || !runId || terminal(runRef.current?.state)) return;
      const url = `/api/v1/runs/${encodeURIComponent(runId)}/events?after=${cursor}`;
      const active = new EventSource(url);
      source = active;
      active.addEventListener('open', () => {
        if (source !== active || !isCurrent()) return;
        retryCount = 0;
        setConnected(true);
        setReconnecting(false);
      });
      for (const kind of EVENT_KINDS) {
        active.addEventListener(kind, (raw) => {
          if (source !== active || !isCurrent()) return;
          const message = raw as MessageEvent<string>;
          let decoded: unknown;
          try { decoded = JSON.parse(message.data); } catch { return; }
          const event = parseEvent(decoded, runId, kind);
          if (!event || event.sequence <= cursor) return;
          if (event.sequence > cursor + 1) {
            void recover(false);
            return;
          }
          acceptEvent(event);
          if (terminal(runRef.current?.state)) void drainTerminal();
        });
      }
      active.addEventListener('error', () => {
        if (source !== active || !isCurrent()) return;
        closeSource();
        scheduleReconnect();
      });
    };

    const drainTerminal = async () => {
      if (busy || !isCurrent()) return;
      try {
        const latest = await snapshot();
        await replay(cursor);
        const state = terminal(latest.state) ? latest.state : runRef.current?.state;
        const targetCursor = Math.max(latest.latest_cursor, latestCursor.current);
        if (terminal(state) && cursor >= targetCursor) closeSource();
      } catch {
        closeSource();
        scheduleReconnect();
      }
    };

    const recover = async (withSnapshot: boolean) => {
      if (busy || !isCurrent()) return;
      busy = true;
      try {
        setConnected(false);
        if (withSnapshot) await snapshot();
        await replay(cursor);
        if (terminal(runRef.current?.state)) {
          const latest = await snapshot();
          await replay(cursor);
          if (cursor >= Math.max(latest.latest_cursor, latestCursor.current)) {
            closeSource();
            setReconnecting(false);
            return;
          }
        }
        if (source) source.close();
        source = null;
        setConnected(false);
        openSource();
      } catch {
        closeSource();
        scheduleReconnect();
      } finally {
        busy = false;
      }
    };

    const scheduleReconnect = () => {
      if (!isCurrent() || retryTimer) return;
      if (retryCount >= maxReconnectAttempts) {
        setReconnecting(false);
        return;
      }
      setReconnecting(true);
      const delay = Math.min(maxRetryDelayMs, retryBaseDelayMs * (2 ** retryCount));
      retryCount += 1;
      retryTimer = setTimeout(() => {
        retryTimer = null;
        void recover(true);
      }, delay);
    };

    if (!runId) {
      runRef.current = null;
      setRun(null);
      setEvents([]);
      setConnected(false);
      setReconnecting(false);
      return () => { disposed = true; };
    }
    latestCursor.current = 0;
    runRef.current = null;
    setRun(null);
    setEvents([]);
    setConnected(false);
    setReconnecting(false);
    const initialize = async () => {
      try {
        const current = await snapshot();
        if (!isCurrent()) return;
        cursor = 0;
        await replay(0);
        if (!isCurrent()) return;
        if (terminal(current.state)) {
          closeSource();
          return;
        }
        openSource();
      } catch {
        scheduleReconnect();
      }
    };
    void initialize();

    return () => {
      disposed = true;
      if (retryTimer) clearTimeout(retryTimer);
      for (const controller of controllers) controller.abort();
      controllers.clear();
      if (source) source.close();
    };
  }, [runId, options.maxReconnectAttempts, options.maxRetryDelayMs, options.retryBaseDelayMs, setRun]);

  return { run, setRun, events, connected, reconnecting };
}
