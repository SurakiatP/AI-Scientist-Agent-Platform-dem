import { useState } from 'react';

// Single seam for report rendering. Plain-text fallback: no HTML, no links, no remote images.
// ponytail: swap the body for react-markdown + remark-math + rehype-katex (trust=false) once approved.
type Language = 'th' | 'en';
const text = (language: Language, en: string, th: string) => language === 'th' ? th : en;

export function CopyCode({ code, language }: { code: string; language: Language }) {
  const [copied, setCopied] = useState(false);
  return <div className="code-block">
    <button type="button" className="button button-quiet button-small" onClick={() => { void navigator.clipboard.writeText(code).then(() => setCopied(true), () => setCopied(false)); }}>{text(language, 'Copy code', 'คัดลอกโค้ด')}</button>
    <span role="status">{copied ? text(language, 'Copied', 'คัดลอกแล้ว') : ''}</span>
    <pre tabIndex={0}><code>{code}</code></pre>
  </div>;
}

function Block({ source, language }: { source: string; language: Language }) {
  return <>{source.split(/\n{2,}/).filter((part) => part.trim()).map((part, index) => {
    const math = /^\$\$([\s\S]*)\$\$$/.exec(part.trim());
    if (math) return <figure key={index} className="math-block"><pre tabIndex={0} aria-label={text(language, 'Equation source', 'ต้นฉบับสมการ')}><code>{math[1].trim()}</code></pre></figure>;
    const heading = /^#{1,6}\s+(.*)$/.exec(part.trim());
    if (heading && !part.includes('\n')) return <h4 key={index}>{heading[1]}</h4>;
    if (part.trim().startsWith('|')) return <div key={index} className="table-scroll" tabIndex={0}><pre>{part}</pre></div>;
    return <p key={index} style={{ whiteSpace: 'pre-wrap' }}>{part}</p>;
  })}</>;
}

export function Markdown({ source, language }: { source: string; language: Language }) {
  const parts = source.split(/```[^\n]*\n([\s\S]*?)```/);
  // split with one capture group alternates prose (even) and code (odd).
  return <div className="report-text">{parts.map((part, index) => index % 2 ? <CopyCode key={index} code={part.replace(/\n$/, '')} language={language} /> : <Block key={index} source={part} language={language} />)}</div>;
}
