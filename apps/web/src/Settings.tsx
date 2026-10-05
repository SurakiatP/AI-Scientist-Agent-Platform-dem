import { useEffect, useState, type CSSProperties, type Dispatch, type FormEvent, type SetStateAction } from 'react';
import { ApiError, apiErrorMessage, request } from './api';
import { useAppPreferences } from './App';
import { AppearanceSettings } from './preferences';

type Language = 'en' | 'th';
type ErrorLabel = 'load' | 'connection_save' | 'connection_remove' | 'peer_save' | 'peer_update' | 'peer_revoke' | 'token_create' | 'token_revoke';
type StoredError = { code: string; status: number; requestId: string } | { message: string; label: ErrorLabel };
type Resource<T> = { status: 'loading' | 'ready' | 'error'; value: T; error: StoredError | null };
type Project = { id: string; name: string; revision: number; instructions: string };
type Connection = { id: string; label: string; provider: string; model: string; state: string; has_secret: boolean };
type Peer = { peer_id: string; endpoint: string; endpoint_fingerprint: string; configured: boolean; credential_configured: boolean; network_check: string };
type Delegation = {
  id: string; project_id: string; project_name?: string; peer_id: string; endpoint: string | null;
  endpoint_fingerprint: string | null; configured: boolean; actions: string[]; credential_id: string | null;
  credential_label: string | null; credential_configured: boolean; network_check: string; revoked_at: string | null;
};
type TokenGrant = { project_id: string; actions: string[] };
type AccessToken = {
  id: string; expires_at: string | null; revoked_at: string | null; created_at: string; grants: TokenGrant[];
};
type CreatedToken = { id: string; expires_at: string | null; token: string };
type Capabilities = { file_types: string[]; max_upload_bytes: number; protocols: Record<string, string> };

const empty = <T,>(): Resource<T> => ({ status: 'loading', value: undefined as T, error: null });
const errorFallbacks: Record<ErrorLabel, [string, string]> = {
  load: ['Unable to load settings.', 'โหลดการตั้งค่าไม่สำเร็จ'],
  connection_save: ['Unable to save connection.', 'บันทึกการเชื่อมต่อไม่สำเร็จ'],
  connection_remove: ['Unable to remove connection.', 'ลบการเชื่อมต่อไม่สำเร็จ'],
  peer_save: ['Unable to save peer access.', 'บันทึกสิทธิ์เพื่อนไม่สำเร็จ'],
  peer_update: ['Unable to update peer credential.', 'ปรับปรุงข้อมูลรับรองเพื่อนไม่สำเร็จ'],
  peer_revoke: ['Unable to revoke peer access.', 'เพิกถอนสิทธิ์เพื่อนไม่สำเร็จ'],
  token_create: ['Unable to create access token.', 'สร้างโทเค็นการเข้าถึงไม่สำเร็จ'],
  token_revoke: ['Unable to revoke token.', 'เพิกถอนโทเค็นไม่สำเร็จ'],
};

function storeError(error: unknown, label: ErrorLabel): StoredError {
  if (error instanceof ApiError) return { code: error.code, status: error.status, requestId: error.requestId };
  return { message: error instanceof Error ? error.message : '', label };
}

function errorText(error: StoredError, language: Language): string {
  if ('code' in error) return apiErrorMessage(new ApiError(error.code, error.status, error.requestId), language);
  if (language === 'th') return errorFallbacks[error.label][1];
  return error.message || errorFallbacks[error.label][0];
}
const grantChoices = [
  ['project:read', 'Read project data', 'อ่านข้อมูลโครงการ'],
  ['file:attach', 'Attach project files', 'แนบไฟล์โครงการ'],
  ['work:submit', 'Submit research work', 'ส่งงานวิจัย'],
  ['result:read', 'Read research results', 'อ่านผลการวิจัย'],
  ['work:cancel', 'Cancel research work', 'ยกเลิกงานวิจัย'],
] as const;
const panelStyle: CSSProperties = { padding: '24px', marginBottom: '22px', border: '1px solid var(--line)', borderRadius: 'var(--radius-card)', background: 'var(--surface)' };
const formStyle: CSSProperties = { display: 'grid', gap: '12px', marginTop: '20px', paddingTop: '18px', borderTop: '1px solid var(--line)' };
const fieldStyle: CSSProperties = { display: 'grid', gap: '5px', fontWeight: 500 };
const controlStyle: CSSProperties = { width: '100%', minHeight: '44px', padding: '8px 10px', border: '1px solid var(--line)', borderRadius: 'var(--radius-field)', color: 'var(--ink)', background: 'var(--surface)' };
const listStyle: CSSProperties = { display: 'grid', gap: '12px', margin: '14px 0 0', padding: 0, listStyle: 'none' };
const itemStyle: CSSProperties = { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '18px', padding: '14px 0', borderTop: '1px solid var(--line)' };
const secondaryTextStyle: CSSProperties = { color: 'var(--muted)', fontSize: '13px' };

function load<T>(path: string, set: Dispatch<SetStateAction<Resource<T>>>, signal: AbortSignal) {
  void request<T>(path, { signal }).then((value) => {
    if (!signal.aborted) set({ status: 'ready', value, error: null });
  }).catch((error: unknown) => {
    if (!signal.aborted) set({ status: 'error', value: undefined as T, error: storeError(error, 'load') });
  });
}

function ResourceMessage<T>({ resource, loading, emptyText, language }: { resource: Resource<T[]>; loading: string; emptyText: string; language: Language }) {
  if (resource.status === 'loading') return <p role="status">{loading}</p>;
  if (resource.status === 'error') return <p role="alert">{resource.error ? errorText(resource.error, language) : errorText({ message: '', label: 'load' }, language)}</p>;
  if (!resource.value.length) return <p>{emptyText}</p>;
  return null;
}

function formatDate(value: string | null, language: Language) {
  if (!value) return language === 'th' ? 'ไม่มีวันหมดอายุ' : 'No expiration date';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return language === 'th' ? 'วันหมดอายุไม่พร้อมใช้งาน' : 'Expiration date unavailable';
  return new Intl.DateTimeFormat(language === 'th' ? 'th-TH' : 'en-US', { dateStyle: 'long' }).format(date);
}

export default function Settings() {
  const { language, ...preferences } = useAppPreferences();
  const tx = (english: string, thai: string) => language === 'th' ? thai : english;
  const [projects, setProjects] = useState<Resource<Project[]>>(empty());
  const [connections, setConnections] = useState<Resource<Connection[]>>(empty());
  const [peers, setPeers] = useState<Resource<Peer[]>>(empty());
  const [delegations, setDelegations] = useState<Resource<Delegation[]>>(empty());
  const [tokens, setTokens] = useState<Resource<AccessToken[]>>(empty());
  const [capabilities, setCapabilities] = useState<Resource<Capabilities>>({ status: 'loading', value: undefined as unknown as Capabilities, error: null });
  const [providerId, setProviderId] = useState('');
  const [connectionName, setConnectionName] = useState('');
  const [model, setModel] = useState('');
  const [providerSecret, setProviderSecret] = useState('');
  const [connectionError, setConnectionError] = useState<StoredError | null>(null);
  const [connectionBusy, setConnectionBusy] = useState(false);
  const [delegationProject, setDelegationProject] = useState('');
  const [delegationPeer, setDelegationPeer] = useState('');
  const [peerCredentialName, setPeerCredentialName] = useState('');
  const [peerSecret, setPeerSecret] = useState('');
  const [delegationError, setDelegationError] = useState<StoredError | null>(null);
  const [delegationBusy, setDelegationBusy] = useState(false);
  const [tokenProject, setTokenProject] = useState('');
  const [tokenActions, setTokenActions] = useState<string[]>([]);
  const [tokenError, setTokenError] = useState<StoredError | null>(null);
  const [tokenBusy, setTokenBusy] = useState(false);
  const [oneTimeToken, setOneTimeToken] = useState<CreatedToken | null>(null);
  const [copyStatus, setCopyStatus] = useState<'copied' | 'copy_failed' | null>(null);
  const [tokenActionError, setTokenActionError] = useState<StoredError | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    load('/api/v1/projects', setProjects, controller.signal);
    load('/api/v1/connections', setConnections, controller.signal);
    load('/api/v1/peers', setPeers, controller.signal);
    load('/api/v1/peer-delegations', setDelegations, controller.signal);
    load('/api/v1/access-tokens', setTokens, controller.signal);
    void request<Capabilities>('/api/v1/capabilities', { signal: controller.signal }).then((value) => {
      if (!controller.signal.aborted) setCapabilities({ status: 'ready', value, error: null });
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setCapabilities({ status: 'error', value: undefined as unknown as Capabilities, error: storeError(error, 'load') });
    });
    return () => controller.abort();
  }, []);

  async function saveConnection(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setConnectionBusy(true);
    setConnectionError(null);
    try {
      const connection = await request<Connection>('/api/v1/connections', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider_id: providerId.trim(), label: connectionName.trim(), model: model.trim(), secret: providerSecret }),
      });
      setConnections((current) => ({ status: 'ready', error: null, value: [connection, ...(current.status === 'ready' ? current.value.filter((item) => item.id !== connection.id) : [])] }));
      setProviderId('');
      setConnectionName('');
      setModel('');
    } catch (error) {
      setConnectionError(storeError(error, 'connection_save'));
    } finally {
      setProviderSecret('');
      setConnectionBusy(false);
    }
  }

  async function removeConnection(id: string) {
    setConnectionError(null);
    try {
      await request<void>(`/api/v1/connections/${encodeURIComponent(id)}`, { method: 'DELETE' });
      setConnections((current) => current.status === 'ready' ? { ...current, value: current.value.filter((item) => item.id !== id) } : current);
    } catch (error) {
      setConnectionError(storeError(error, 'connection_remove'));
    }
  }

  async function grantPeer(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setDelegationBusy(true);
    setDelegationError(null);
    try {
      const delegation = await request<Delegation>('/api/v1/peer-delegations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_id: delegationProject, peer_id: delegationPeer, credential_label: peerCredentialName.trim(), credential: peerSecret }),
      });
      setDelegations((current) => ({ status: 'ready', error: null, value: [delegation, ...(current.status === 'ready' ? current.value.filter((item) => item.id !== delegation.id) : [])] }));
      setDelegationProject('');
      setDelegationPeer('');
      setPeerCredentialName('');
    } catch (error) {
      setDelegationError(storeError(error, 'peer_save'));
    } finally {
      setPeerSecret('');
      setDelegationBusy(false);
    }
  }

  async function updatePeerCredential(event: FormEvent<HTMLFormElement>, delegation: Delegation) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const credential_label = String(data.get('credential_label') ?? '').trim();
    const credential = String(data.get('credential') ?? '');
    setDelegationError(null);
    try {
      const updated = await request<{ credential_id: string; credential_label: string }>(`/api/v1/peer-delegations/${encodeURIComponent(delegation.id)}/credential`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ credential_label, credential }),
      });
      form.reset();
      setDelegations((current) => current.status === 'ready' ? { ...current, value: current.value.map((item) => item.id === delegation.id ? { ...item, credential_id: updated.credential_id, credential_label: updated.credential_label, credential_configured: true } : item) } : current);
    } catch (error) {
      setDelegationError(storeError(error, 'peer_update'));
    } finally {
      const secretField = form.elements.namedItem('credential');
      if (secretField instanceof HTMLInputElement) secretField.value = '';
    }
  }

  async function revokeDelegation(id: string) {
    setDelegationError(null);
    try {
      await request<void>(`/api/v1/peer-delegations/${encodeURIComponent(id)}`, { method: 'DELETE' });
      setDelegations((current) => current.status === 'ready' ? { ...current, value: current.value.map((item) => item.id === id ? { ...item, revoked_at: new Date().toISOString(), credential_configured: false, credential_id: null, credential_label: null } : item) } : current);
    } catch (error) {
      setDelegationError(storeError(error, 'peer_revoke'));
    }
  }

  async function createScopedToken(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setTokenBusy(true);
    setTokenError(null);
    setTokenActionError(null);
    setCopyStatus(null);
    try {
      const grant = { project_id: tokenProject, actions: [...tokenActions] };
      const created = await request<CreatedToken>('/api/v1/access-tokens', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ grants: [grant] }),
      });
      setOneTimeToken(created);
      const metadata: AccessToken = { id: created.id, expires_at: created.expires_at, revoked_at: null, created_at: new Date().toISOString(), grants: [grant] };
      setTokens((current) => ({ status: 'ready', error: null, value: [metadata, ...(current.status === 'ready' ? current.value.filter((item) => item.id !== created.id) : [])] }));
      setTokenProject('');
      setTokenActions([]);
    } catch (error) {
      setTokenError(storeError(error, 'token_create'));
    } finally {
      setTokenBusy(false);
    }
  }

  async function copyToken() {
    if (!oneTimeToken) return;
    try {
      await navigator.clipboard.writeText(oneTimeToken.token);
      setCopyStatus('copied');
    } catch {
      setCopyStatus('copy_failed');
    }
  }

  async function revokeToken(id: string) {
    setTokenActionError(null);
    try {
      await request<void>(`/api/v1/access-tokens/${encodeURIComponent(id)}`, { method: 'DELETE' });
      setTokens((current) => current.status === 'ready' ? { ...current, value: current.value.map((item) => item.id === id ? { ...item, revoked_at: new Date().toISOString() } : item) } : current);
    } catch (error) {
      setTokenActionError(storeError(error, 'token_revoke'));
    }
  }

  function toggleAction(action: string) {
    setTokenActions((current) => current.includes(action) ? current.filter((item) => item !== action) : [...current, action]);
  }

  const projectOptions = projects.status === 'ready' ? projects.value : [];
  const availablePeers = peers.status === 'ready' ? peers.value.filter((peer) => peer.configured) : [];

  return <section className="workspace-placeholder settings-workspace">
    <div className="page-heading">
      <p className="eyebrow">AI SCIENTIST AGENT PLATFORM</p>
      <h1 className="route-heading" tabIndex={-1}>{tx('Settings', 'ตั้งค่า')}</h1>
    </div>

    <div className="settings-group settings-preferences">
      <h2>{tx('Appearance and language', 'รูปลักษณ์และภาษา')}</h2>
      <AppearanceSettings language={language} {...preferences} />
    </div>

    <section className="settings-group settings-section" style={panelStyle} aria-labelledby="connections-title">
      <h2 id="connections-title">{tx('Connections', 'การเชื่อมต่อ')}</h2>
      <p>{tx('Credentials are encrypted by the service. Saving a credential does not test the provider.', 'บริการจะเข้ารหัสข้อมูลรับรอง การบันทึกไม่ได้ทดสอบผู้ให้บริการ')}</p>
      <ResourceMessage resource={connections} loading={tx('Loading connections…', 'กำลังโหลดการเชื่อมต่อ…')} emptyText={tx('No connections configured.', 'ยังไม่มีการตั้งค่าการเชื่อมต่อ')} language={language} />
      {connections.status === 'ready' && <ul className="settings-list" style={listStyle}>
        {connections.value.map((connection) => <li className="settings-item" style={itemStyle} key={connection.id}>
          <div><strong>{connection.label}</strong><p style={secondaryTextStyle}>{connection.model} · {connection.provider}</p>
            <span role="status">{connection.state === 'unavailable_provider' ? tx('Provider is not configured.', 'ยังไม่ได้ตั้งค่าผู้ให้บริการ') : connection.state === 'invalid_credentials' ? tx('Stored credential cannot be decrypted.', 'ไม่สามารถถอดรหัสข้อมูลรับรองที่บันทึกไว้') : tx('Configured; verification not run', 'ตั้งค่าแล้ว; ยังไม่ได้ตรวจสอบ')}</span>
          </div>
          <button className="button button-quiet button-small" type="button" onClick={() => void removeConnection(connection.id)}>{tx('Remove', 'ลบ')}</button>
        </li>)}
      </ul>}
      {connectionError && <p role="alert">{errorText(connectionError, language)}</p>}
      <form className="settings-form" style={formStyle} onSubmit={(event) => void saveConnection(event)}>
        <h3>{tx('Add a model connection', 'เพิ่มการเชื่อมต่อโมเดล')}</h3>
        <p>{tx('Enter the configured provider ID. Do not enter an endpoint URL.', 'ป้อนรหัสผู้ให้บริการที่กำหนดไว้ ห้ามป้อน URL ปลายทาง')}</p>
        <label style={fieldStyle}>{tx('Configured provider ID', 'รหัสผู้ให้บริการที่กำหนดไว้')}<input style={controlStyle} required value={providerId} onChange={(event) => setProviderId(event.target.value)} autoComplete="off" /></label>
        <label style={fieldStyle}>{tx('Connection name', 'ชื่อการเชื่อมต่อ')}<input style={controlStyle} required value={connectionName} onChange={(event) => setConnectionName(event.target.value)} maxLength={200} /></label>
        <label style={fieldStyle}>{tx('Model', 'โมเดล')}<input style={controlStyle} required value={model} onChange={(event) => setModel(event.target.value)} maxLength={200} /></label>
        <label style={fieldStyle}>{tx('Provider credential', 'ข้อมูลรับรองผู้ให้บริการ')}<input style={controlStyle} required type="password" autoComplete="new-password" value={providerSecret} onChange={(event) => setProviderSecret(event.target.value)} /></label>
        <button className="button button-primary" type="submit" disabled={connectionBusy}>{connectionBusy ? tx('Saving…', 'กำลังบันทึก…') : tx('Save connection', 'บันทึกการเชื่อมต่อ')}</button>
      </form>
    </section>

    <section className="settings-group settings-section" style={panelStyle} aria-labelledby="peer-title">
      <h2 id="peer-title">{tx('A2A peer access', 'สิทธิ์การเข้าถึงเพื่อน A2A')}</h2>
      <p>{tx('A project delegation allows this peer to receive only the approved per-run release. Every release needs an approved purpose and data scope.', 'การมอบสิทธิ์ระดับโครงการอนุญาตให้เพื่อนรับเฉพาะข้อมูลที่อนุมัติในแต่ละงาน ทุกรายการต้องมีวัตถุประสงค์และขอบเขตข้อมูลที่อนุมัติ')}</p>
      <ResourceMessage resource={peers} loading={tx('Loading configured peers…', 'กำลังโหลดเพื่อนที่กำหนดไว้…')} emptyText={tx('No peer endpoints are configured.', 'ยังไม่มีการกำหนดปลายทางเพื่อน')} language={language} />
      {peers.status === 'ready' && <ul className="settings-list" style={listStyle}>
        {peers.value.map((peer) => <li className="settings-item" style={itemStyle} key={peer.peer_id}>
          <div><strong>{peer.peer_id}</strong><p style={secondaryTextStyle}>{peer.endpoint}</p>
            <span>{peer.credential_configured ? tx('A credential is stored.', 'บันทึกข้อมูลรับรองแล้ว') : tx('No project credential stored.', 'ยังไม่มีข้อมูลรับรองของโครงการ')}</span>
            <p>{tx('Network check: NOT RUN', 'การตรวจสอบเครือข่าย: NOT RUN')}</p>
          </div>
        </li>)}
      </ul>}
      {peers.status === 'error' && <p role="alert">{peers.error ? errorText(peers.error, language) : errorText({ message: '', label: 'load' }, language)}</p>}
      <ResourceMessage resource={projects} loading={tx('Loading projects…', 'กำลังโหลดโครงการ…')} emptyText={tx('Create a project before granting peer access.', 'สร้างโครงการก่อนมอบสิทธิ์ให้เพื่อน')} language={language} />
      {delegationError && <p role="alert">{errorText(delegationError, language)}</p>}
      {projects.status === 'ready' && peers.status === 'ready' && projectOptions.length > 0 && availablePeers.length > 0 && <form className="settings-form" style={formStyle} onSubmit={(event) => void grantPeer(event)}>
        <h3>{tx('Grant project access', 'มอบสิทธิ์เข้าถึงโครงการ')}</h3>
        <label style={fieldStyle}>{tx('Project for peer delegation', 'โครงการสำหรับมอบสิทธิ์เพื่อน')}<select style={controlStyle} required value={delegationProject} onChange={(event) => setDelegationProject(event.target.value)}><option value="">{tx('Choose a project', 'เลือกโครงการ')}</option>{projectOptions.map((project) => <option value={project.id} key={project.id}>{project.name}</option>)}</select></label>
        <label style={fieldStyle}>{tx('Configured peer', 'เพื่อนที่กำหนดไว้')}<select style={controlStyle} required value={delegationPeer} onChange={(event) => setDelegationPeer(event.target.value)}><option value="">{tx('Choose a peer', 'เลือกเพื่อน')}</option>{availablePeers.map((peer) => <option value={peer.peer_id} key={peer.peer_id}>{peer.peer_id} · {peer.endpoint}</option>)}</select></label>
        <label style={fieldStyle}>{tx('Peer credential name', 'ชื่อข้อมูลรับรองเพื่อน')}<input style={controlStyle} required value={peerCredentialName} onChange={(event) => setPeerCredentialName(event.target.value)} maxLength={200} /></label>
        <label style={fieldStyle}>{tx('Peer credential', 'ข้อมูลรับรองเพื่อน')}<input style={controlStyle} required type="password" autoComplete="new-password" value={peerSecret} onChange={(event) => setPeerSecret(event.target.value)} /></label>
        <p>{tx('Delegation action: peer release only.', 'การดำเนินการที่มอบสิทธิ์: ส่งข้อมูลที่อนุมัติให้เพื่อนเท่านั้น')}</p>
        <button className="button button-primary" type="submit" disabled={delegationBusy}>{delegationBusy ? tx('Saving…', 'กำลังบันทึก…') : tx('Grant peer access', 'มอบสิทธิ์ให้เพื่อน')}</button>
      </form>}
      <h3>{tx('Project delegations', 'การมอบสิทธิ์โครงการ')}</h3>
      <ResourceMessage resource={delegations} loading={tx('Loading delegations…', 'กำลังโหลดการมอบสิทธิ์…')} emptyText={tx('No project peer delegations.', 'ยังไม่มีการมอบสิทธิ์เพื่อนในโครงการ')} language={language} />
      {delegations.status === 'ready' && <ul className="settings-list" style={listStyle}>
        {delegations.value.map((delegation) => <li className="settings-item" style={itemStyle} key={delegation.id}>
          <div><strong>{delegation.project_name ?? projectOptions.find((project) => project.id === delegation.project_id)?.name ?? delegation.project_id}</strong>
            <p>{delegation.peer_id} · {delegation.endpoint ?? tx('Endpoint unavailable', 'ปลายทางไม่พร้อมใช้งาน')}</p>
            <p>{tx('Delegation action:', 'การดำเนินการที่มอบสิทธิ์:')} {delegation.actions.join(', ')}</p>
            {delegation.credential_configured && <p>{tx('Stored credential:', 'ข้อมูลรับรองที่บันทึกไว้:')} {delegation.credential_label}</p>}
            <p>{tx('Network check: NOT RUN', 'การตรวจสอบเครือข่าย: NOT RUN')}</p>
            {delegation.revoked_at && <span>{tx('Revoked', 'เพิกถอนแล้ว')}</span>}
          </div>
          {!delegation.revoked_at && <div className="settings-actions">
            <form className="settings-form" style={formStyle} onSubmit={(event) => void updatePeerCredential(event, delegation)}>
              <h4>{tx('Replace peer credential', 'เปลี่ยนข้อมูลรับรองเพื่อน')}</h4>
              <label style={fieldStyle}>{tx('New credential name', 'ชื่อข้อมูลรับรองใหม่')}<input style={controlStyle} required name="credential_label" maxLength={200} defaultValue={delegation.credential_label ?? ''} /></label>
              <label style={fieldStyle}>{tx('New peer credential', 'ข้อมูลรับรองเพื่อนใหม่')}<input style={controlStyle} required name="credential" type="password" autoComplete="new-password" /></label>
              <button className="button button-quiet button-small" type="submit">{tx('Update credential', 'ปรับปรุงข้อมูลรับรอง')}</button>
            </form>
            <button className="button button-quiet button-small" type="button" onClick={() => void revokeDelegation(delegation.id)}>{tx('Revoke peer access', 'เพิกถอนสิทธิ์เพื่อน')}</button>
          </div>}
        </li>)}
      </ul>}
    </section>

    <section className="settings-group settings-section" style={panelStyle} aria-labelledby="tokens-title">
      <h2 id="tokens-title">{tx('Scoped access tokens', 'โทเค็นการเข้าถึงแบบจำกัดขอบเขต')}</h2>
      <p>{tx('Tokens grant only the selected project actions and expire after 30 days. Copy a new token now; it will not be shown again.', 'โทเค็นให้สิทธิ์เฉพาะการดำเนินการที่เลือกในโครงการและหมดอายุใน 30 วัน คัดลอกโทเค็นใหม่ตอนนี้ เพราะจะแสดงเพียงครั้งเดียว')}</p>
      <ResourceMessage resource={projects} loading={tx('Loading projects…', 'กำลังโหลดโครงการ…')} emptyText={tx('Create a project before issuing a token.', 'สร้างโครงการก่อนออกโทเค็น')} language={language} />
      {tokenError && <p role="alert">{errorText(tokenError, language)}</p>}
      {tokenActionError && <p role="alert">{errorText(tokenActionError, language)}</p>}
      {oneTimeToken && <div className="one-time-token" role="alert" style={{ ...panelStyle, marginTop: '18px' }}>
        <h3>{tx('Copy this token now', 'คัดลอกโทเค็นนี้ตอนนี้')}</h3>
        <p>{tx('This secret is shown only once. Closing or refreshing this page clears it.', 'ข้อมูลลับนี้จะแสดงครั้งเดียว เมื่อปิดหรือโหลดหน้านี้ใหม่ โทเค็นจะหายไป')}</p>
        <code>{oneTimeToken.token}</code>
        <p>{tx('Expires', 'หมดอายุ')} {formatDate(oneTimeToken.expires_at, language)}</p>
        <button className="button button-primary" type="button" onClick={() => void copyToken()}>{tx('Copy token', 'คัดลอกโทเค็น')}</button>
        <button className="button button-quiet button-small" type="button" onClick={() => { setOneTimeToken(null); setCopyStatus(null); }}>{tx('Hide token', 'ซ่อนโทเค็น')}</button>
        {copyStatus && <p role="status">{copyStatus === 'copied' ? tx('Token copied.', 'คัดลอกโทเค็นแล้ว') : tx('Copy failed. Select and copy the token manually.', 'คัดลอกไม่สำเร็จ โปรดเลือกและคัดลอกโทเค็นด้วยตนเอง')}</p>}
      </div>}
      {projects.status === 'ready' && projectOptions.length > 0 && <form className="settings-form" onSubmit={(event) => void createScopedToken(event)}>
        <h3>{tx('Create a project token', 'สร้างโทเค็นสำหรับโครงการ')}</h3>
        <label style={fieldStyle}>{tx('Token project', 'โครงการของโทเค็น')}<select style={controlStyle} required value={tokenProject} onChange={(event) => setTokenProject(event.target.value)}><option value="">{tx('Choose a project', 'เลือกโครงการ')}</option>{projectOptions.map((project) => <option value={project.id} key={project.id}>{project.name}</option>)}</select></label>
        <fieldset className="token-actions"><legend>{tx('Allowed actions', 'การดำเนินการที่อนุญาต')}</legend>
          {grantChoices.map(([action, english, thai]) => <label className="choice" key={action}><input type="checkbox" checked={tokenActions.includes(action)} onChange={() => toggleAction(action)} /><span>{tx(english, thai)}</span></label>)}
        </fieldset>
        <button className="button button-primary" type="submit" disabled={tokenBusy || !tokenProject || !tokenActions.length}>{tokenBusy ? tx('Creating…', 'กำลังสร้าง…') : tx('Create scoped token', 'สร้างโทเค็นแบบจำกัดขอบเขต')}</button>
      </form>}
      <h3>{tx('Issued tokens', 'โทเค็นที่ออกแล้ว')}</h3>
      <ResourceMessage resource={tokens} loading={tx('Loading tokens…', 'กำลังโหลดโทเค็น…')} emptyText={tx('No access tokens issued.', 'ยังไม่มีการออกโทเค็นการเข้าถึง')} language={language} />
      {tokens.status === 'ready' && <ul className="settings-list" style={listStyle}>
        {tokens.value.map((token) => <li className="settings-item" style={itemStyle} key={token.id}>
          <div><strong>{token.id}</strong>
            <p>{tx('Expires', 'หมดอายุ')} {formatDate(token.expires_at, language)}</p>
            <ul>{token.grants.map((grant) => <li key={grant.project_id}>{projectOptions.find((project) => project.id === grant.project_id)?.name ?? grant.project_id}: {grant.actions.join(', ')}</li>)}</ul>
            {token.revoked_at && <span>{tx('Revoked', 'เพิกถอนแล้ว')}</span>}
          </div>
          {!token.revoked_at && <button className="button button-quiet button-small" type="button" aria-label={`${tx('Revoke token', 'เพิกถอนโทเค็น')} ${token.id}`} onClick={() => void revokeToken(token.id)}>{tx('Revoke token', 'เพิกถอนโทเค็น')}</button>}
        </li>)}
      </ul>}
    </section>

    <section className="settings-group settings-section" style={panelStyle} aria-labelledby="capabilities-title">
      <h2 id="capabilities-title">{tx('Workspace capabilities', 'ความสามารถของพื้นที่ทำงาน')}</h2>
      {capabilities.status === 'loading' && <p role="status">{tx('Loading capabilities…', 'กำลังโหลดความสามารถ…')}</p>}
      {capabilities.status === 'error' && <p role="alert">{capabilities.error ? errorText(capabilities.error, language) : errorText({ message: '', label: 'load' }, language)}</p>}
      {capabilities.status === 'ready' && <>
        <p>{tx('Supported files', 'ไฟล์ที่รองรับ')}: {capabilities.value.file_types.join(', ')}</p>
        <p>{tx('Maximum file size', 'ขนาดไฟล์สูงสุด')}: {Math.round(capabilities.value.max_upload_bytes / (1024 * 1024))} MB</p>
        <ul>{Object.entries(capabilities.value.protocols).map(([protocol, state]) => <li key={protocol}>{protocol.toUpperCase()}: {state}</li>)}</ul>
      </>}
    </section>
  </section>;
}
