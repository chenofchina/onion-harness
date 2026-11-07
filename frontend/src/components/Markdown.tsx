import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/**
 * Renders assistant output as Markdown.
 *
 * `react-markdown` builds a React tree rather than setting innerHTML, so model
 * output cannot inject scripts. Raw HTML is intentionally not enabled: the
 * model may quote markup and we do not want it to become live DOM.
 */
export function Markdown({ text }: { text: string }) {
  if (!text) return null
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ node, ...props }) => {
            void node
            return <a {...props} target="_blank" rel="noreferrer noopener" />
          },
          code: ({ node, className, children, ...props }) => {
            void node
            const inline = !className && !String(children).includes('\n')
            if (inline) {
              return (
                <code className="md-code-inline" {...props}>
                  {children}
                </code>
              )
            }
            const language = /language-(\w+)/.exec(className || '')?.[1]
            return (
              <code className={className} data-language={language} {...props}>
                {children}
              </code>
            )
          },
          table: ({ node, ...props }) => {
            void node
            return (
              <div className="md-table-wrap">
                <table {...props} />
              </div>
            )
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}