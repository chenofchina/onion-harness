import { useEffect, useState } from 'react'
import { loadLobeIcon } from '../lib/lobe'

type Props = {
  name: string
  size?: number
  className?: string
  title?: string
}

/**
 * Renders a mono Lobe icon inline. The SVG is sanitised to path/title only so
 * it inherits `currentColor` and adapts to the active Material 3 theme.
 */
export function LobeIcon({ name, size = 20, className, title }: Props) {
  const [markup, setMarkup] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let alive = true
    setFailed(false)
    loadLobeIcon(name).then((svg) => {
      if (!alive) return
      if (svg) setMarkup(svg)
      else setFailed(true)
    })
    return () => {
      alive = false
    }
  }, [name])

  if (failed || (!markup && !name)) {
    return (
      <span className={`lobe-fallback ${className ?? ''}`} style={{ width: size, height: size }}>
        {name.slice(0, 1).toUpperCase()}
      </span>
    )
  }

  if (!markup) {
    return <span className={`lobe-skeleton ${className ?? ''}`} style={{ width: size, height: size }} />
  }

  const safe = sanitizeSvg(markup)
  return (
    <span
      className={`lobe-icon ${className ?? ''}`}
      style={{ width: size, height: size }}
      title={title ?? name}
      aria-hidden={title ? undefined : true}
      role={title ? 'img' : 'presentation'}
      dangerouslySetInnerHTML={{ __html: safe }}
    />
  )
}

/**
 * Allowlist sanitizer for CDN SVG.
 *
 * The icons come from a pinned npm package, but `dangerouslySetInnerHTML` still
 * means anything that reaches it becomes live DOM. Instead of stripping known
 * bad patterns (which new payloads slip past), only recognised shape elements
 * with recognised attributes are kept; everything else is dropped. Event
 * handlers, <script>, <foreignObject> and href-based vectors cannot survive
 * regardless of how they are spelled.
 */
const ALLOWED_TAGS = new Set([
  'svg', 'g', 'path', 'circle', 'rect', 'ellipse', 'line', 'polyline', 'polygon', 'title',
])
const ALLOWED_ATTRS = new Set([
  'viewBox', 'd', 'cx', 'cy', 'r', 'rx', 'ry', 'x', 'y', 'x1', 'y1', 'x2', 'y2',
  'width', 'height', 'points', 'fill', 'fill-rule', 'fill-opacity', 'clip-rule',
  'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin', 'stroke-opacity',
  'stroke-dasharray', 'stroke-dashoffset', 'opacity', 'transform', 'xmlns',
])

function sanitizeSvg(raw: string): string {
  const parser = new DOMParser()
  const doc = parser.parseFromString(raw, 'image/svg+xml')
  const root = doc.documentElement
  if (!root || root.nodeName.toLowerCase() !== 'svg' || doc.querySelector('parsererror')) {
    return ''
  }

  const clean = (node: Element): void => {
    for (const attr of [...node.attributes]) {
      if (!ALLOWED_ATTRS.has(attr.name)) node.removeAttribute(attr.name)
    }
    // Icons are monochrome: sizing and colour come from CSS.
    node.removeAttribute('width')
    node.removeAttribute('height')
    if (node.hasAttribute('fill') && !['currentColor', 'none'].includes(node.getAttribute('fill')!)) {
      node.setAttribute('fill', 'currentColor')
    }
    for (const child of [...node.children]) {
      if (!ALLOWED_TAGS.has(child.nodeName.toLowerCase())) {
        child.remove()
      } else {
        clean(child)
      }
    }
  }

  clean(root)
  if (!root.hasAttribute('viewBox')) root.setAttribute('viewBox', '0 0 24 24')
  return new XMLSerializer().serializeToString(root)
}