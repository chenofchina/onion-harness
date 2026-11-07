import { useState } from 'react'
import { Archive, Check, Code2, Play, Power, Trash2, X } from 'lucide-react'
import type { Bootstrap, McpToolRecord, TaskRecord } from '../lib/api'
import { Modal } from './Dialogs'
import { StatusDot } from './Sidebar'

/** Create MCP: request a generated tool, then manage what exists. */
export function McpDialog({
  open,
  tools,
  onClose,
  onPropose,
  onToggle,
  onDelete,
  busy,
}: {
  open: boolean
  tools: McpToolRecord[]
  onClose: () => void
  onPropose: (spec: {
    name: string
    title: string
    description: string
    parameters: string
    code: string
    reason: string
  }) => void
  onToggle: (name: string, enabled: boolean) => void
  onDelete: (name: string) => void
  busy?: boolean
}) {
  const [tab, setTab] = useState<'create' | 'installed'>('create')
  const [name, setName] = useState('')
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [params, setParams] = useState(
    '{\n  "type": "object",\n  "properties": {\n    "text": { "type": "string" }\n  },\n  "required": ["text"]\n}',
  )
  const [code, setCode] = useState(
    'def handle(arguments):\n    text = str(arguments.get("text", ""))\n    return {"length": len(text)}',
  )
  const [reason, setReason] = useState('')
  const [error, setError] = useState<string | null>(null)

  if (!open) return null

  const submit = () => {
    try {
      JSON.parse(params)
    } catch {
      setError('Parameters must be valid JSON')
      return
    }
    setError(null)
    onPropose({ name, title, description, parameters: params, code, reason })
  }

  return (
    <Modal title="Create MCP" onClose={onClose} wide>
      <div className="tabs">
        <button data-active={tab === 'create'} onClick={() => setTab('create')}>
          Create
        </button>
        <button data-active={tab === 'installed'} onClick={() => setTab('installed')}>
          Installed ({tools.length})
        </button>
      </div>

      <div className="modal__body">
        {tab === 'create' && (
          <>
            <div className="card-change__warn" style={{ color: 'var(--md-sys-color-on-surface-variant)', background: 'color-mix(in srgb, var(--md-sys-color-on-surface) 6%, transparent)' }}>
              <Code2 size={15} style={{ flexShrink: 0, marginTop: 1 }} />
              <span>
                A generated tool is a Python script written to ~/.onionharness/mcp/. It may import
                only standard utility modules; network and shell access are refused. Creating one
                still requires your confirmation in the change ledger.
              </span>
            </div>

            <div className="row" style={{ gap: 10 }}>
              <div className="field" style={{ flex: 1 }}>
                <label>Name</label>
                <input
                  type="text"
                  value={name}
                  onChange={(event) => setName(event.target.value.toLowerCase())}
                  placeholder="word_stats"
                  spellCheck={false}
                />
              </div>
              <div className="field" style={{ flex: 1 }}>
                <label>Title</label>
                <input
                  type="text"
                  value={title}
                  onChange={(event) => setTitle(event.target.value)}
                  placeholder="Word Stats"
                />
              </div>
            </div>

            <div className="field">
              <label>Description</label>
              <input
                type="text"
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                placeholder="What the tool does"
              />
            </div>

            <div className="field">
              <label>Parameters (JSON Schema)</label>
              <textarea
                className="code-block mcp-code"
                value={params}
                onChange={(event) => setParams(event.target.value)}
                rows={6}
                spellCheck={false}
              />
            </div>

            <div className="field">
              <label>handle(arguments)</label>
              <textarea
                className="code-block mcp-code"
                value={code}
                onChange={(event) => setCode(event.target.value)}
                rows={7}
                spellCheck={false}
              />
            </div>

            <div className="field">
              <label>Why</label>
              <input
                type="text"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder="Reason recorded in the change ledger"
              />
            </div>

            {error && <div className="card-change__warn">{error}</div>}
          </>
        )}

        {tab === 'installed' && (
          <>
            {tools.length === 0 && (
              <span className="monitor-empty">No generated tools yet.</span>
            )}
            <div className="stack">
              {tools.map((tool) => (
                <div key={tool.name} className="history-row">
                  <span className="history-row__main">
                    <strong>{tool.title || tool.name}</strong>
                    <small>
                      {tool.path || `mcp/${tool.name}/tool.py`} ·{' '}
                      {tool.enabled ? 'enabled' : 'disabled'}
                    </small>
                  </span>
                  <button
                    className="btn"
                    data-variant="outlined"
                    onClick={() => onToggle(tool.name, !tool.enabled)}
                    disabled={busy}
                    title={tool.enabled ? 'Disable' : 'Enable'}
                  >
                    <Power size={13} /> {tool.enabled ? 'Disable' : 'Enable'}
                  </button>
                  <button
                    className="btn"
                    data-variant="outlined"
                    onClick={() => onDelete(tool.name)}
                    disabled={busy}
                    title="Delete"
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              ))}
            </div>
          </>
        )}
      </div>

      {tab === 'create' && (
        <div className="modal__footer">
          <button className="btn" data-variant="text" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn"
            data-variant="filled"
            onClick={submit}
            disabled={busy || !name || !description || !code}
          >
            <Play size={14} /> Propose tool
          </button>
        </div>
      )}
    </Modal>
  )
}

/** Archive area, reachable from Settings. */
export function ArchiveDialog({
  tasks,
  onClose,
  onRestore,
  onSelect,
}: {
  tasks: TaskRecord[]
  onClose: () => void
  onRestore: (id: string) => void
  onSelect: (id: string) => void
}) {
  return (
    <Modal title="Archived tasks" onClose={onClose} wide>
      <div className="modal__body">
        {tasks.length === 0 && <span className="monitor-empty">Nothing archived.</span>}
        <div className="stack">
          {tasks.map((task) => (
            <div key={task.id} className="history-row">
              <StatusDot status={task.status} />
              <span className="history-row__main">
                <strong>{task.title || task.prompt}</strong>
                <small>
                  {new Date(task.created_at * 1000).toLocaleString()} · {task.status}
                </small>
              </span>
              <button className="btn" data-variant="outlined" onClick={() => onSelect(task.id)}>
                Open
              </button>
              <button className="btn" data-variant="tonal" onClick={() => onRestore(task.id)}>
                <Archive size={13} /> Restore
              </button>
            </div>
          ))}
        </div>
      </div>
      <div className="modal__footer">
        <button className="btn" data-variant="filled" onClick={onClose}>
          <Check size={14} /> Done
        </button>
      </div>
    </Modal>
  )
}

export type { Bootstrap }