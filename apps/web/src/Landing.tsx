import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { FlaskMark, LanguageSwitch } from './App';
import { LabDemo, INITIAL_LAB_STATE, type LabState } from './LabDemo';

type Language = 'th' | 'en';
const copy = {
  th: {
    navLabel: 'เมนูหลัก', work: 'การทำงาน', connections: 'การเชื่อมต่อ', start: 'เริ่มต้นใช้งาน',
    eyebrow: 'YOUR SCIENTIFIC WORKSPACE', title: <>ทุกคำถามวิทยาศาสตร์<br /><em>มีพื้นที่ให้ค้นต่อ</em></>,
    intro: <>ผู้ช่วย AI สำหรับค้นหลักฐาน วิเคราะห์ข้อมูล<br />และเปลี่ยนสิ่งที่ค้นพบให้เข้าใจได้<br />ทำงานร่วมกับคุณ ตั้งแต่แผนจนถึงผลงาน</>, project: 'เริ่มโปรเจกต์วิจัย', explore: 'ลองสำรวจ lab',
    note: 'คุณกำหนดคำถาม ตรวจแผน · ติดตามและหยุดงานได้', notebookLabel: 'ตัวอย่างพื้นที่วิจัย', projectName: 'Diffusion research',
    session: 'Literature review', artifacts: 'ผลงานโปรเจกต์', files: ['literature-summary.md', 'diffusion-demo.html', 'source-notes.md'],
    question: 'อุณหภูมิมีผลต่อการแพร่ของโมเลกุลอย่างไร?', assistant: 'เริ่มจากหลักฐาน', processes: ['ค้นและตรวจแหล่งอ้างอิง', 'สร้างคำอธิบายที่สำรวจได้', 'เก็บผลไว้ในโปรเจกต์'], processNotes: ['แยกข้อค้นพบออกจากสมมติฐาน', 'ปรับตัวแปรและสังเกตผล', 'รายงาน สื่อประกอบ และแหล่งที่มา'],
    scope: 'ตัวอย่างนี้แสดงแนวทาง UI · ไม่ได้รันงานวิจัยจริง', band: ['ค้นวรรณกรรม', 'วิเคราะห์ข้อมูล', 'รันโค้ด', 'จำลองและอธิบาย', 'เขียนงานวิจัย'],
    outputFlow: 'คำถาม → ผลงาน', sessionLanguages: 'บทสนทนา',
    sections: [
      ['01 / EVIDENCE FIRST', <>ค้นให้ลึก<br />แล้วกลับมาที่หลักฐาน</>, 'จัด paper ข้อค้นพบ และแหล่งอ้างอิงไว้ด้วยกัน เปรียบเทียบวิธีวิจัยและข้อจำกัด แล้วกลับไปตรวจเอกสารต้นทางได้เสมอ', 'Paper · ข้อค้นพบ · แหล่งอ้างอิง'],
      ['02 / YOUR RESEARCH SPACE', <>หลายคำถาม<br />ต่อยอดในโปรเจกต์เดียว</>, 'แยกบทสนทนาเป็น sessions จัดไฟล์ โค้ด และผลงานตาม project พร้อมพื้นที่รันงานของแต่ละโปรเจกต์', 'Projects · Sessions · Files · Sandbox'],
      ['03 / MAKE IT UNDERSTANDABLE', <>สิ่งที่ซับซ้อน<br />สำรวจให้เห็นภาพได้</>, 'ใช้กราฟ animation และ simulation ช่วยอธิบายแนวคิด ปรับตัวแปรเพื่อสำรวจผล และเก็บสื่อไว้ข้างบทสนทนา', 'ดูผล · ปรับค่า · ตรวจสมมติฐาน'],
      ['04 / CONNECT YOUR WORK', <>เชื่อมเครื่องมือ<br />ร่วมงานกับ agents อื่น</>, 'ใช้ API และโมเดลที่คุณเลือก เปิดความสามารถผ่าน MCP และรับหรือส่งงานกับ agents ผู้เชี่ยวชาญผ่าน A2A', 'Web · REST API · MCP · A2A'],
    ],
    footerTitle: 'เริ่มจากคำถามของคุณ', footerText: 'สร้าง project แล้ววางแผนการค้นคว้าไปด้วยกัน', footerButton: 'เริ่มต้นใช้งาน', footer: 'พื้นที่ทำงานวิจัยในเครื่อง',
  },
  en: {
    navLabel: 'Main navigation', work: 'Work', connections: 'Connections', start: 'Get started',
    eyebrow: 'YOUR SCIENTIFIC WORKSPACE', title: <>Every scientific question<br /><em>has room to go further</em></>,
    intro: <>An AI research partner to find evidence, analyze data,<br />and make discoveries easier to understand.<br />Work together from the first plan to the final output.</>, project: 'Start a research project', explore: 'Explore the lab',
    note: 'You set the question, review the plan, and control each run.', notebookLabel: 'Research workspace preview', projectName: 'Diffusion research',
    session: 'Literature review', artifacts: 'PROJECT OUTPUTS', files: ['literature-summary.md', 'diffusion-demo.html', 'source-notes.md'],
    question: 'How does temperature affect molecular diffusion?', assistant: 'Start with the evidence', processes: ['Find and check sources', 'Build an explorable explanation', 'Keep results with the project'], processNotes: ['Separate findings from assumptions', 'Adjust variables and observe', 'Reports, media, and references'],
    scope: 'Interface demonstration · no research run is active', band: ['Search literature', 'Analyze data', 'Run code', 'Simulate and explain', 'Write research'],
    outputFlow: 'Question → outputs', sessionLanguages: 'Sessions',
    sections: [
      ['01 / EVIDENCE FIRST', <>Look deeper<br />and return to the evidence</>, 'Keep papers, findings, and references together. Compare methods and limitations, then return to the original source whenever you need to check the details.', 'Papers · Findings · References'],
      ['02 / YOUR RESEARCH SPACE', <>Many questions<br />in one research project</>, 'Keep conversations in separate sessions and organize files, code, and outputs by project, with a workspace for each project.', 'Projects · Sessions · Files · Sandbox'],
      ['03 / MAKE IT UNDERSTANDABLE', <>Explore complex ideas<br />through clear visuals</>, 'Use charts, animation, and simulations to explain an idea. Change variables, explore the result, and keep the media beside the conversation.', 'Observe · Adjust · Check assumptions'],
      ['04 / CONNECT YOUR WORK', <>Connect tools<br />and work with other agents</>, 'Use the APIs and models you choose, expose capabilities through MCP, and exchange tasks with specialist agents through A2A.', 'Web · REST API · MCP · A2A'],
    ],
    footerTitle: 'Start with your question', footerText: 'Create a project and plan the research together.', footerButton: 'Get started', footer: 'Local research workspace',
  },
} as const;

export default function Landing({ language, setLanguage, onOpenNavigation }: { language: Language; setLanguage: (language: Language) => void; onOpenNavigation: () => void }) {
  const [state, setState] = useState<LabState>(() => ({ ...INITIAL_LAB_STATE }));
  useEffect(() => {
    const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
    const pause = () => { if (motion.matches) setState((current) => ({ ...current, paused: true })); };
    pause(); motion.addEventListener('change', pause);
    return () => motion.removeEventListener('change', pause);
  }, []);
  const text = copy[language];
  return <div className="landing-page">
    <header className="science-nav">
      <button type="button" className="mobile-menu science-menu" aria-label={language === 'th' ? 'เปิดเมนูนำทาง' : 'Open navigation'} onClick={onOpenNavigation}>☰</button>
      <Link to="/" className="science-brand"><FlaskMark /><span>AI Scientist Agent Platform</span></Link>
      <nav aria-label={text.navLabel}><a href="#research">{text.work}</a><a href="#connections">{text.connections}</a></nav>
      <div className="science-nav-actions"><Link className="science-start" to="/projects">{text.start}<span aria-hidden="true"> →</span></Link><LanguageSwitch language={language} setLanguage={setLanguage} /></div>
    </header>
    <section className="science-hero" aria-labelledby="hero-title">
      <p className="science-eyebrow">{text.eyebrow}</p>
      <h1 id="hero-title" className="route-heading" tabIndex={-1} lang={language}>{text.title}</h1>
      <p className="science-hero-copy">{text.intro}</p>
      <div className="science-actions"><Link to="/projects" className="science-primary">{text.project}<span aria-hidden="true"> →</span></Link><a href="#lab-demo" className="science-secondary">{text.explore}<span aria-hidden="true"> ↓</span></a></div>
      <p className="science-note">{text.note}</p>
    </section>

    <section id="lab-demo" className="notebook-wrap" aria-label={text.notebookLabel}>
      <header className="notebook-top"><FlaskMark /><strong>{text.projectName}</strong><span>{text.notebookLabel}</span></header>
      <div className="notebook-body">
        <aside className="notebook-sidebar">
          <span className="notebook-label">PROJECTS</span><strong className="notebook-project-name">▱ {text.projectName}</strong>
          <Link className="notebook-session selected" to="/projects">{text.session}</Link><Link className="notebook-session" to="/projects">Concept simulation</Link><Link className="notebook-session" to="/projects">Research notes</Link>
          <div className="notebook-files"><span className="notebook-label">{text.artifacts}</span>{text.files.map((file) => <p key={file}><span aria-hidden="true">▤</span> {file}</p>)}</div>
          <Link className="notebook-connections" to="/settings">Connections · MCP · A2A</Link>
        </aside>
        <div className="notebook-thread">
          <span className="notebook-tag">01 / QUESTION EXPLORATION</span>
          <h2 className="notebook-question">{text.question}</h2>
          <p className="notebook-assistant"><span className="agent-dot" />Scientific assistant</p>
          <p className="notebook-intro">{text.assistant}</p>
          <ol className="notebook-process">{text.processes.map((step, index) => <li key={step}><span>0{index + 1}</span><div><strong>{step}</strong><small>{text.processNotes[index]}</small></div></li>)}</ol>
          <p className="notebook-scope">{text.scope}</p>
        </div>
        <LabDemo state={state} onChange={setState} language={language} />
      </div>
      <footer className="notebook-bottom"><span>{text.outputFlow}</span><span>{text.sessionLanguages} · TH · EN</span></footer>
    </section>

    <div className="science-band">{text.band.map((item) => <span key={item}>{item}</span>)}</div>
    <section id="research" className="science-section" aria-labelledby="research-title">
      <div className="science-section-copy"><span className="science-section-tag">{text.sections[0][0]}</span><h2 id="research-title">{text.sections[0][1]}</h2><p>{text.sections[0][2]}</p><span className="science-citation">{text.sections[0][3]}</span></div>
      <ResearchIllustration />
    </section>
    <section className="science-section reverse" aria-labelledby="project-title">
      <div className="science-section-copy"><span className="science-section-tag">{text.sections[1][0]}</span><h2 id="project-title">{text.sections[1][1]}</h2><p>{text.sections[1][2]}</p><span className="science-citation">{text.sections[1][3]}</span></div>
      <ProjectIllustration />
    </section>
    <section className="science-section" aria-labelledby="visuals-title">
      <div className="science-section-copy"><span className="science-section-tag">{text.sections[2][0]}</span><h2 id="visuals-title">{text.sections[2][1]}</h2><p>{text.sections[2][2]}</p><span className="science-citation">{text.sections[2][3]}</span></div>
      <VisualIllustration />
    </section>
    <section id="connections" className="science-section reverse" aria-labelledby="connections-title">
      <div className="science-section-copy"><span className="science-section-tag">{text.sections[3][0]}</span><h2 id="connections-title">{text.sections[3][1]}</h2><p>{text.sections[3][2]}</p><span className="science-citation">{text.sections[3][3]}</span></div>
      <ConnectionsIllustration />
    </section>
    <footer className="science-footer"><h2>{text.footerTitle}</h2><p>{text.footerText}</p><Link to="/projects">{text.footerButton}<span aria-hidden="true"> →</span></Link><small>{text.footer}</small></footer>
  </div>;
}

function ResearchIllustration() {
  return <svg className="science-art" viewBox="0 0 360 270" aria-hidden="true"><rect className="art-soft art-stroke" x="63" y="52" width="143" height="162" rx="13" transform="rotate(-10 134 133)"/><rect className="art-paper art-stroke" x="88" y="39" width="143" height="177" rx="13"/><rect className="art-mint" x="110" y="59" width="89" height="14" rx="5"/><path d="M110 90h89m-89 12h73m-73 12h85m-85 12h61" className="art-stroke" fill="none" opacity=".4"/><path d="M112 183l20-16 23 5 22-33 25 13" className="art-accent-stroke" fill="none"/><circle className="art-soft art-stroke" cx="244" cy="164" r="47"/><circle className="art-paper art-accent-stroke" cx="244" cy="164" r="33"/><path d="M277 198l27 29q9 10 17 1t-1-18l-27-28" className="art-yellow art-stroke"/><path d="M230 166l9 9 22-24" className="art-accent-stroke" fill="none"/><circle className="art-blue" cx="281" cy="59" r="9"/></svg>;
}
function ProjectIllustration() {
  return <svg className="science-art" viewBox="0 0 360 270" aria-hidden="true"><path className="art-mint art-stroke" d="M52 94V61q0-12 12-12h70l19 24h131q13 0 13 13v126H52z"/><rect className="art-paper art-stroke" x="83" y="68" width="79" height="116" rx="8" transform="rotate(-8 122 126)"/><path d="M96 100h48m-47 13h39m-38 13h42" className="art-accent-stroke" fill="none"/><rect className="art-paper art-stroke" x="181" y="71" width="89" height="105" rx="9" transform="rotate(8 225 123)"/><path d="M198 137l12-25 14 11 26-27" className="art-accent-stroke" fill="none"/><path className="art-soft art-stroke" d="M46 130h257l-17 85q-2 13-15 13H73q-13 0-16-13z"/><rect className="art-accent" x="124" y="155" width="87" height="39" rx="9"/><path d="M149 167l-9 7 9 7m35-14 9 7-9 7m-15-16-6 19" stroke="white" strokeWidth="3" fill="none" strokeLinecap="round"/><circle className="art-yellow" cx="301" cy="53" r="11"/></svg>;
}
function VisualIllustration() {
  return <svg className="science-art" viewBox="0 0 360 270" aria-hidden="true"><rect className="art-paper art-stroke" x="40" y="33" width="275" height="196" rx="16"/><path d="M59 61h237" className="art-stroke" opacity=".2"/><circle className="art-accent" cx="62" cy="49" r="3"/><path d="M65 173H202M73 86v87" className="art-stroke" opacity=".3"/><path d="M74 161C101 164 108 98 136 99S172 161 198 161" className="art-accent-stroke" fill="none"/><circle className="art-blue art-stroke" cx="240" cy="104" r="14"/><circle className="art-mint art-stroke" cx="266" cy="135" r="10"/><circle className="art-yellow art-stroke" cx="235" cy="159" r="12"/><path d="M70 204h176" className="art-stroke" opacity=".2"/><circle className="art-accent" cx="154" cy="204" r="7"/><path d="M279 198v12m-6-6h12" className="art-accent-stroke"/></svg>;
}
function ConnectionsIllustration() {
  return <div className="connections-illustration" aria-hidden="true"><div className="agent-orbit"><span className="agent-circle">MCP</span><span className="agent-connector"/><span className="agent-circle center">AI<br />Platform</span><span className="agent-connector"/><span className="agent-circle">A2A</span></div></div>;
}
