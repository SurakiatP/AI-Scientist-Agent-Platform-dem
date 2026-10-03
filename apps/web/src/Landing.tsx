import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { t, type Language } from './locales';
import { INITIAL_LAB_STATE, LabDemo, type LabState } from './LabDemo';

export default function Landing({ language }: { language: Language }) {
  const [state, setState] = useState<LabState>(() => ({ ...INITIAL_LAB_STATE }));
  useEffect(() => {
    const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
    const pause = () => { if (motion.matches) setState((current) => ({ ...current, paused: true })); };
    pause(); motion.addEventListener('change', pause);
    return () => motion.removeEventListener('change', pause);
  }, []);

  return <div className="landing-page">
    <section className="hero" aria-labelledby="hero-title">
      <div className="hero-copy">
        <p className="eyebrow"><span className="eyebrow-dot" />{t('landingEyebrow', language)}</p>
        <h1 id="hero-title" className="route-heading" tabIndex={-1}>{t('heroTitle', language)}</h1>
        <p className="hero-description">{t('heroText', language)}</p>
        <div className="hero-actions"><Link className="button button-primary" to="/projects">{t('getStarted', language)}<span aria-hidden="true">→</span></Link><a className="text-link" href="#lab">{t('exploreLab', language)}<span aria-hidden="true">↓</span></a></div>
      </div>
      <div className="hero-art" aria-hidden="true">
        <div className="orbit orbit-one" /><div className="orbit orbit-two" /><div className="orbit orbit-three" />
        <div className="orbit-core"><span>?</span></div>
        <span className="orbit-node node-one" /><span className="orbit-node node-two" /><span className="orbit-node node-three" />
        <span className="art-label art-label-top">QUESTION</span><span className="art-label art-label-bottom">EVIDENCE → INSIGHT</span>
      </div>
    </section>
    <section className="workflow-section" aria-labelledby="workflow-title">
      <div className="section-heading"><div><p className="eyebrow">{t('workflowEyebrow', language)}</p><h2 id="workflow-title">{t('workflowTitle', language)}</h2></div><p>{t('workflowIntro', language)}</p></div>
      <div className="workflow-grid">
        <article className="workflow-step"><span className="step-index">01</span><h3>{t('stepQuestion', language)}</h3><p>{t('stepQuestionText', language)}</p></article>
        <article className="workflow-step"><span className="step-index">02</span><h3>{t('stepEvidence', language)}</h3><p>{t('stepEvidenceText', language)}</p></article>
        <article className="workflow-step"><span className="step-index">03</span><h3>{t('stepSynthesis', language)}</h3><p>{t('stepSynthesisText', language)}</p></article>
      </div>
    </section>
    <section id="lab" className="lab-section" aria-labelledby="lab-section-title">
      <div className="lab-section-copy"><p className="eyebrow">{t('labEyebrow', language)}</p><h2 id="lab-section-title">{t('labTitle', language)}</h2><p>{t('labIntro', language)}</p></div>
      <LabDemo state={state} onChange={setState} language={language} />
    </section>
    <footer className="site-footer"><span>{t('brand', language)}</span><span>{t('footerLine', language)}</span></footer>
  </div>;
}
