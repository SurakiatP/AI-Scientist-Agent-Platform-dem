import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { Link, NavLink, Outlet, Route, Routes, useLocation, useNavigationType } from 'react-router-dom';
import Projects from './Projects';
import ProjectDetails from './ProjectDetails';
import Library from './Library';
import RunHistory from './RunHistory';
import Chat from './Chat';
import Landing from './Landing';
import { AppearanceSettings, usePreferences } from './preferences';
import { t } from './locales';

type Preferences = ReturnType<typeof usePreferences>;
const PreferencesContext = createContext<Preferences | null>(null);
export function useAppPreferences(): Preferences {
  const preferences = useContext(PreferencesContext);
  if (!preferences) throw new Error('useAppPreferences must be used within AppShell');
  return preferences;
}

function PageFocus() {
  const location = useLocation();
  const navigationType = useNavigationType();
  const scroll = useRef(new Map<string, number>());
  const previous = useRef<{ key: string; top: number } | null>(null);
  useLayoutEffect(() => {
    if (previous.current) scroll.current.set(previous.current.key, window.scrollY);
    const top = navigationType === 'POP' ? scroll.current.get(location.key) ?? 0 : 0;
    window.scrollTo(0, top);
    requestAnimationFrame(() => document.querySelector<HTMLElement>('main h1')?.focus({ preventScroll: true }));
    previous.current = { key: location.key, top };
  }, [location.key, navigationType]);
  return null;
}

export default function App() {
  return <Routes><Route element={<AppShell />}>
    <Route index element={<LandingRoute />} />
    <Route path="projects" element={<Projects />} />
    <Route path="projects/:projectId" element={<ProjectDetails />} />
    <Route path="projects/:projectId/library" element={<Library />} />
    <Route path="projects/:projectId/sessions/:sessionId" element={<Chat />} />
    <Route path="projects/:projectId/runs" element={<RunHistory />} />
    <Route path="sources" element={<Library />} />
    <Route path="history" element={<RunHistory />} />
    <Route path="settings" element={<WorkspacePage titleKey="settingsTitle" textKey="settingsUnavailable" />} />
    <Route path="settings/appearance" element={<AppearanceRoute />} />
    <Route path="*" element={<WorkspacePage titleKey="workspaceTitle" textKey="workspaceUnavailable" />} />
  </Route></Routes>;
}

export function AppShell() {
  const preferences = usePreferences();
  const { language } = preferences;
  const [drawerOpen, setDrawerOpen] = useState(false);
  const drawer = useRef<HTMLDialogElement>(null);
  const location = useLocation();

  useEffect(() => {
    if (drawerOpen && drawer.current && !drawer.current.open) drawer.current.showModal();
    if (!drawerOpen && drawer.current?.open) drawer.current.close();
  }, [drawerOpen]);
  useEffect(() => { setDrawerOpen(false); }, [location.pathname]);

  const links = [
    ['/', 'home'], ['/projects', 'projects'], ['/sources', 'sources'], ['/history', 'history'], ['/settings/appearance', 'settings'],
  ] as const;
  const navigation = (onNavigate?: () => void) => <nav aria-label={t('navLabel', language)}>
    {links.map(([to, key]) => <NavLink key={to} to={to} end={to === '/'} onClick={onNavigate} className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}>
      <span className={`nav-glyph glyph-${key}`} aria-hidden="true" />{t(key, language)}
    </NavLink>)}
  </nav>;

  return <>
    <a className="skip-link" href="#main">{t('skip', language)}</a>
    <header className="app-header">
      <button type="button" className="icon-button mobile-menu" aria-label={t('openNavigation', language)} onClick={() => setDrawerOpen(true)}><span aria-hidden="true">☰</span></button>
      <Link to="/" className="wordmark"><span className="wordmark-icon" aria-hidden="true"><i /><i /><i /></span><span>{t('brand', language)}</span></Link>
      <div className="desktop-nav">{navigation()}</div>
      <div className="header-tools">
        <div className="language-switch" role="group" aria-label={t('language', language)}>
          <button type="button" aria-pressed={language === 'th'} onClick={() => preferences.setLanguage('th')}>TH</button>
          <button type="button" aria-pressed={language === 'en'} onClick={() => preferences.setLanguage('en')}>EN</button>
        </div>
        <Link to="/projects" className="header-cta">{t('getStarted', language)}<span aria-hidden="true">↗</span></Link>
      </div>
    </header>

    <main id="main" tabIndex={-1}>
      <PageFocus />
      <PreferencesContext.Provider value={preferences}><Outlet /></PreferencesContext.Provider>
    </main>
    <dialog ref={drawer} className="navigation-drawer" aria-label={t('navDialog', language)} onClose={() => setDrawerOpen(false)}>
      <div className="drawer-head"><Link to="/" className="wordmark" onClick={() => drawer.current?.close()}><span className="wordmark-icon" aria-hidden="true"><i /><i /><i /></span><span>{t('brand', language)}</span></Link><button type="button" className="icon-button" aria-label={t('closeNavigation', language)} onClick={() => drawer.current?.close()}>×</button></div>
      {navigation(() => drawer.current?.close())}
    </dialog>
  </>;
}

function LandingRoute() { const { language } = useAppPreferences(); return <Landing language={language} />; }
function AppearanceRoute() { const preferences = useAppPreferences(); const { language } = preferences; return <Page title={t('settingsTitle', language)}><AppearanceSettings {...preferences} /></Page>; }
function WorkspacePage({ titleKey, textKey }: { titleKey: 'workspaceTitle' | 'sourcesTitle' | 'historyTitle' | 'settingsTitle'; textKey: 'workspaceUnavailable' | 'settingsUnavailable' }) {
  const { language } = useAppPreferences();
  return <Page title={t(titleKey, language)}><p>{t(textKey, language)}</p></Page>;
}

function Page({ title, children }: { title: string; children: ReactNode }) {
  return <section className="workspace-placeholder"><div className="page-heading"><p className="eyebrow">AI SCIENTIST AGENT PLATFORM</p><h1 className="route-heading" tabIndex={-1}>{title}</h1></div><div className="placeholder-card">{children}</div></section>;
}
