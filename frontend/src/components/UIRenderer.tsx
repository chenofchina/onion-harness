import { createElement, useMemo, type ReactNode } from 'react'
import type { UIActionSpec, UINodeSpec } from '../lib/api'

/**
 * Renders a validated `<ui>` block.
 *
 * The Python side already dropped every tag and attribute that is not allowlisted,
 * so what arrives here is a small, known node tree. It is still rendered by
 * building React elements rather than setting `innerHTML`: that way a mistake in
 * the allowlist cannot turn into a script execution, and the data path stays the
 * same as every other event card.
 *
 * A click does not run anything locally. It posts the action id to the server,
 * which feeds it back to the agent as an ordinary user turn - so an approved click
 * still goes through the same review and approval gates as a typed message.
 */

function nodeKey(node: UINodeSpec, index: number) {
  return `${node.tag}-${index}`
}

function renderNodes(
  nodes: UINodeSpec[],
  onAction: (id: string) => void,
  depth = 0,
): ReactNode[] | null {
  if (depth > 12) return null // guard against a pathological tree
  return nodes.map((node, index) => {
    const key = nodeKey(node, index)
    // A bare text run from Python, carrying its position among the elements.
    if (node.tag === '#text') return node.text || ''
    // A node can carry both text and element children:
    // `<p>Press <b>OK</b> now</p>` arrives as text plus a child. Rendering only the
    // children dropped the prose, which is one way a valid block looked half-drawn.
    const childNodes = node.children?.length
      ? renderNodes(node.children, onAction, depth + 1)
      : null
    const children: ReactNode =
      node.text && childNodes ? [node.text, ...childNodes] : childNodes || node.text || null

    if (node.tag === 'button') {
      const actionId = node.attrs?.['data-action']
      return (
        <button
          key={key}
          type="button"
          className={['ui-btn', node.attrs?.class].filter(Boolean).join(' ')}
          // Boolean attributes arrive as "" (present) or are absent entirely, so
          // presence - not truthiness - decides.
          disabled={'disabled' in (node.attrs || {})}
          onClick={() => {
            if (actionId) onAction(actionId)
          }}
        >
          {children ?? 'Button'}
        </button>
      )
    }

    if (node.tag === 'input') {
      // Inputs are rendered read-only on purpose: a value typed here would have no
      // way back to the agent unless it were submitted with an action, and a
      // half-working form is worse than a clearly inert one.
      const isCheckbox = node.attrs?.type === 'checkbox'
      return (
        <input
          key={key}
          type={isCheckbox ? 'checkbox' : 'text'}
          className={['ui-input', node.attrs?.class].filter(Boolean).join(' ')}
          placeholder={node.attrs?.placeholder}
          // `value` on a checkbox is ignored by React, and `checked` on a text
          // input is a warning; each control takes the one that applies.
          defaultChecked={isCheckbox ? 'checked' in (node.attrs || {}) : undefined}
          defaultValue={isCheckbox ? undefined : node.attrs?.value}
          disabled={'disabled' in (node.attrs || {})}
          readOnly={!isCheckbox}
        />
      )
    }

    if (node.tag === 'br') return <br key={key} />
    if (node.tag === 'hr') return <hr key={key} />

    const className = node.attrs?.class
    // Only allowlisted tags arrive, and the list is closed, so this is bounded by
    // the Python allowlist rather than by anything a model can invent.
    // createElement is used because the tag name is data, not a literal.
    return createElement(
      node.tag,
      {
        key,
        className: className ? `ui-${node.tag} ${className}` : `ui-${node.tag}`,
        // These attributes survived the allowlist but were being dropped here, so
        // an already-open <details> closed and a spanning table cell did not span.
        ...(node.tag === 'details' && 'open' in (node.attrs || {}) ? { open: true } : {}),
        ...(node.tag === 'td' || node.tag === 'th'
          ? {
              colSpan: node.attrs?.colspan ? Number(node.attrs.colspan) : undefined,
              rowSpan: node.attrs?.rowspan ? Number(node.attrs.rowspan) : undefined,
            }
          : {}),
      },
      children,
    )
  })
}

export function UIRenderer({
  nodes,
  onAction,
}: {
  nodes: UINodeSpec[]
  onAction: (id: string) => void
}) {
  const rendered = useMemo(() => renderNodes(nodes, onAction), [nodes, onAction])
  return <div className="card ui-block">{rendered}</div>
}

export function UIActionList({
  actions,
  onAction,
}: {
  actions: UIActionSpec[]
  onAction: (id: string) => void
}) {
  if (!actions.length) return null
  return (
    <div className="card ui-actions">
      {actions.map((action) => (
        <button
          key={action.id}
          type="button"
          className="ui-btn"
          onClick={() => onAction(action.id)}
        >
          {action.label || action.id}
        </button>
      ))}
    </div>
  )
}