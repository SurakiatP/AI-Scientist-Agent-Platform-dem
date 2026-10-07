import './original-ui.css';
import Settings from './Settings';
import ResearchSetup from './ResearchSetup';
import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { Link, Outlet, Route, Routes, useLocation, useNavigationType } from 'react-router-dom';
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
const OpenNavigationContext = createContext<() => void>(() => {});
export function useAppPreferences(): Preferences {
  const preferences = useContext(PreferencesContext);
  if (!preferences) throw new Error('useAppPreferences must be used within AppShell');
  return preferences;
}

function PageFocus() {
  const location = useLocation();
  const navigationType = useNavigationType();
  const scroll = useRef(new Map<string, number>());
  const previous = useRef<{ key: string; pathname: string } | null>(null);
  useLayoutEffect(() => {
    if (previous.current) scroll.current.set(previous.current.key, window.scrollY);
    const samePage = previous.current?.pathname === location.pathname;
    const top = samePage ? window.scrollY : navigationType === 'POP' ? scroll.current.get(location.key) ?? 0 : 0;
    if (!samePage) {
      window.scrollTo(0, top);
      const restoreFocus = () => document.querySelector<HTMLElement>('main h1')?.focus({ preventScroll: true });
      restoreFocus();
      requestAnimationFrame(() => window.setTimeout(restoreFocus, 50));
    }
    previous.current = { key: location.key, pathname: location.pathname };
  }, [location.key, location.pathname, navigationType]);
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
    <Route path="projects/:projectId/research-setup" element={<ResearchSetup />} />
    <Route path="sources" element={<Library />} />
    <Route path="history" element={<RunHistory />} />
    <Route path="settings" element={<Settings />} />
    <Route path="settings/appearance" element={<AppearanceRoute />} />
    <Route path="*" element={<WorkspacePage titleKey="workspaceTitle" textKey="workspaceUnavailable" />} />
  </Route></Routes>;
}

const internalLinks = [
  ['/projects', 'projects', 'projects'],
  ['/sources', 'sources', 'sources'],
  ['/history', 'history', 'history'],
  ['/settings', 'settings', 'settings'],
] as const;

function InternalNavigation({ language, close }: { language: 'th' | 'en'; close?: () => void }) {
  const { pathname } = useLocation();
  const activePath = /^\/projects\/[^/]+\/library$/.test(pathname) ? '/sources' : /^\/projects\/[^/]+\/runs$/.test(pathname) ? '/history' : pathname;
  return <nav aria-label={t('navLabel', language)}>
    {internalLinks.map(([to, key, glyph]) => <Link key={to} to={to} onClick={close} aria-current={activePath === to || activePath.startsWith(`${to}/`) ? 'page' : undefined} className={`workspace-link${activePath === to || activePath.startsWith(`${to}/`) ? ' active' : ''}`}>
      <span className={`nav-glyph glyph-${glyph}`} aria-hidden="true" />{t(key, language)}
    </Link>)}
  </nav>;
}

export function AppShell() {
  const preferences = usePreferences();
  const { language } = preferences;
  const [drawerOpen, setDrawerOpen] = useState(false);
  const drawer = useRef<HTMLDialogElement>(null);
  const location = useLocation();
  const isLanding = location.pathname === '/';
  const isChat = /^\/projects\/[^/]+\/sessions\/[^/]+$/.test(location.pathname);

  useEffect(() => {
    if (drawerOpen && drawer.current && !drawer.current.open) drawer.current.showModal();
    if (!drawerOpen && drawer.current?.open) drawer.current.close();
  }, [drawerOpen]);
  useEffect(() => { setDrawerOpen(false); }, [location.pathname]);

  return <div className={`app-frame${isLanding ? ' landing-frame' : ''}${isChat ? ' chat-frame' : ''}`}>
    <a className="skip-link" href="#main">{t('skip', language)}</a>
    {!isLanding && <header className="mobile-topbar">
      <button type="button" className="icon-button mobile-menu" aria-label={t('openNavigation', language)} onClick={() => setDrawerOpen(true)}><span aria-hidden="true">☰</span></button>
      <Link to="/projects" className="wordmark"><FlaskMark /><span>{t('brand', language)}</span></Link>
      <LanguageSwitch language={language} setLanguage={preferences.setLanguage} />
    </header>}
    {!isLanding && !isChat && <aside className="workspace-sidebar">
      <Link to="/" className="sidebar-brand"><span>AI Scientist<br />Agent Platform</span></Link>
      <p className="sidebar-section-label">YOUR RESEARCH SPACE</p>
      <InternalNavigation language={language} />
      <LanguageSwitch language={language} setLanguage={preferences.setLanguage} />
      <footer className="sidebar-footer"><span className="footer-workspace-icon" aria-hidden="true">⌂</span><span>{language === 'th' ? 'พื้นที่ทำงานในเครื่อง' : 'Local workspace'}</span></footer>
    </aside>}
    <div className="app-content">
      <main id="main" tabIndex={-1}>
        <PageFocus />
        <PreferencesContext.Provider value={preferences}><OpenNavigationContext.Provider value={() => setDrawerOpen(true)}><Outlet /></OpenNavigationContext.Provider></PreferencesContext.Provider>
      </main>
    </div>
    <dialog ref={drawer} className="navigation-drawer" aria-label={t('navDialog', language)} onClose={() => setDrawerOpen(false)}>
      <div className="drawer-head"><Link to="/projects" className="wordmark" onClick={() => drawer.current?.close()}><FlaskMark /><span>{t('brand', language)}</span></Link><button type="button" className="icon-button" aria-label={t('closeNavigation', language)} onClick={() => drawer.current?.close()}>×</button></div>
      <InternalNavigation language={language} close={() => drawer.current?.close()} />
    </dialog>
  </div>;
}

export function LanguageSwitch({ language, setLanguage }: { language: 'th' | 'en'; setLanguage: (language: 'th' | 'en') => void }) {
  return <div className="language-switch" role="group" aria-label={t('language', language)}>
    <button type="button" aria-pressed={language === 'th'} onClick={() => setLanguage('th')}>TH</button>
    <button type="button" aria-pressed={language === 'en'} onClick={() => setLanguage('en')}>EN</button>
  </div>;
}

export function FlaskMark() {
  return <svg className="flask-mark" viewBox="0 0 40 40" aria-hidden="true"><path d="M14 5h12M17 5v12L8 31q-2 5 4 5h16q6 0 4-5l-9-14V5" fill="none" stroke="currentColor" strokeWidth="3" strokeLinejoin="round"/><path d="M13 27h14l4 7H9z" fill="currentColor"/><circle cx="21" cy="23" r="2" fill="currentColor"/></svg>;
}

function LandingRoute() { const preferences = useAppPreferences(); const openNavigation = useContext(OpenNavigationContext); return <Landing language={preferences.language} setLanguage={preferences.setLanguage} onOpenNavigation={openNavigation} />; }
function AppearanceRoute() { const preferences = useAppPreferences(); const { language } = preferences; return <Page title={t('settingsTitle', language)}><AppearanceSettings {...preferences} /></Page>; }
function WorkspacePage({ titleKey, textKey }: { titleKey: 'workspaceTitle' | 'sourcesTitle' | 'historyTitle' | 'settingsTitle'; textKey: 'workspaceUnavailable' | 'settingsUnavailable' }) {
  const { language } = useAppPreferences();
  return <Page title={t(titleKey, language)}><p>{t(textKey, language)}</p></Page>;
}
function Page({ title, children }: { title: string; children: ReactNode }) {
  return <section className="workspace-placeholder"><div className="page-heading"><p className="eyebrow">AI SCIENTIST AGENT PLATFORM</p><h1 className="route-heading" tabIndex={-1}>{title}</h1></div><div className="placeholder-card">{children}</div></section>;
}
