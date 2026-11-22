import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Safe markdown: raw HTML in model output is never rendered (no rehype-raw), and links are limited to
 * http(s)/mailto and open in a new tab without access to this page.
 */
const SAFE_URL = /^(https?:|mailto:)/i;

export function Markdown({ text }: { text: string }) {
  return (
    <div className="aos-md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        urlTransform={(url) => {
          const safe = defaultUrlTransform(url);
          return SAFE_URL.test(safe) ? safe : "";
        }}
        components={{
          a: ({ href, children }) =>
            href ? (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            ) : (
              <span>{children}</span>
            ),
          img: () => null,
          table: ({ children }) => (
            <div className="aos-table-wrap">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
