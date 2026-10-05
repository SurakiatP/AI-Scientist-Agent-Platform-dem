import { Children, isValidElement, useId, useState, type ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import rehypeKatex from 'rehype-katex';
import remarkGfm from 'remark-gfm';
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
  const footnotePrefix = `${useId().replace(/:/g, '')}-`; // several reports can share one page
  const components: Components = {
    // In-page links (GFM footnotes) keep their ids/aria and stay in this tab; everything else opens safely in a new one.
    a: ({ node: _node, href, children, ...rest }) => href ? <a {...rest} href={href} {...(href.startsWith('#') ? {} : { target: '_blank', rel: 'noopener noreferrer' })}>{children}</a> : <>{children}</>,
    img: ({ alt }) => <span>{alt}</span>, // remote images are never fetched
    pre: ({ children }) => <CopyCode code={textOf(children).replace(/\n$/, '')} language={language} />,
    table: ({ node: _node, children }) => <div className="table-scroll" tabIndex={0} role="region" aria-label={text(language, 'Table', 'ตาราง')}><table>{children}</table></div>,
  };
  // No rehype-raw: raw HTML in the source is escaped to text. KaTeX trust=false disables \href/\url/\includegraphics.
  return <div className="report-text"><ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[[rehypeKatex, { trust: false, strict: 'warn', throwOnError: false }]]} remarkRehypeOptions={{ clobberPrefix: footnotePrefix, footnoteLabel: text(language, 'Footnotes', 'เชิงอรรถ'), footnoteLabelProperties: { className: ['visually-hidden'] }, footnoteBackLabel: (i) => text(language, `Back to reference ${i + 1}`, `กลับไปที่อ้างอิง ${i + 1}`) }} urlTransform={urlTransform} components={components}>{source}</ReactMarkdown></div>;
}
