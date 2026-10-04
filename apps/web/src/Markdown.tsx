import { Children, isValidElement, useState, type ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import rehypeKatex from 'rehype-katex';
import remarkMath from 'remark-math';
import 'katex/dist/katex.min.css';

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

// Only http(s), mailto and relative URLs survive; everything else (javascript:, data:, vbscript:, ...) is dropped.
const SAFE_URL = /^(https?:|mailto:|[/#?.]|[^:]*$)/i;
const urlTransform = (url: string) => SAFE_URL.test(url.trim()) && !/^\s*\/\//.test(url) ? url : '';
const textOf = (node: ReactNode): string => Children.toArray(node).map((c) => typeof c === 'string' ? c : isValidElement<{ children?: ReactNode }>(c) ? textOf(c.props.children) : '').join('');

export function Markdown({ source, language }: { source: string; language: Language }) {
  const components: Components = {
    a: ({ node: _node, href, children }) => href ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <>{children}</>,
    img: ({ alt }) => <span>{alt}</span>, // remote images are never fetched
    pre: ({ children }) => <CopyCode code={textOf(children).replace(/\n$/, '')} language={language} />,
    // ponytail: no remark-gfm is pinned, so pipe tables arrive as a paragraph; keep them readable and scrollable. Add remark-gfm to get real <table>.
    p: ({ node, children }) => {
      const start = node?.position?.start.offset, end = node?.position?.end.offset;
      const t = start != null && end != null ? source.slice(start, end) : textOf(children);
      return /^\s*\|/.test(t) && t.includes('\n')
        ? <div className="table-scroll" tabIndex={0} role="region" aria-label={text(language, 'Table (plain text)', 'ตาราง (ข้อความ)')}><pre>{t}</pre></div>
        : <p>{children}</p>;
    },
    table: ({ node: _node, children }) => <div className="table-scroll" tabIndex={0}><table>{children}</table></div>,
  };
  // No rehype-raw: raw HTML in the source is escaped to text. KaTeX trust=false disables \href/\url/\includegraphics.
  return <div className="report-text"><ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[[rehypeKatex, { trust: false, strict: 'warn', throwOnError: false }]]} urlTransform={urlTransform} components={components}>{source}</ReactMarkdown></div>;
}
