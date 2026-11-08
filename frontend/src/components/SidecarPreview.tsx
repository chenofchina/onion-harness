import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Columns2,
  Crosshair,
  ExternalLink,
  Globe,
  RefreshCw,
  Terminal,
  X,
} from 'lucide-react'
import { api, type ChangeRecord, type FileView, type OnionEvent } from '../lib/api'
import {
  DiffBlock,
  DiffBlocks,
  FileBrowser,
  FileViewer,
  useTaskDiff,
} from './WorkspaceBrowser'

/**
 * Sidecar preview panel.
 *
 * Three multimodal modes share one resizable split:
 *   web      - iframe preview of a detected local server, with an element picker
 *              that writes a CSS path back into the composer
 *   terminal - live stdout/stderr mirror for the running executor
 *   diff     - side-by-side before/after for a change proposal
 */

export type PreviewMode = 'web' | 'terminal' | 'diff'

type Props = {
  open: boolean
  mode: PreviewMode
  onModeChange: (mode: PreviewMode) => void
  onClose: () => void
  url: string
  onUrlChange: (url: string) => void
  events: OnionEvent[]
  proposals: ChangeRecord[]
  onPickedElement: (snippet: string) => void
  taskId?: string | null
  focusPath?: string
}

export function SidecarPreview({
  open,
  mode,
  onModeChange,
  onClose,
  url,
  onUrlChange,
  events,
  proposals,
  onPickedElement,
  taskId = null,
  focusPath = '',
}: Props) {
  const [width, setWidth] = useState(440)
  const dragging = useRef(false)

  // Hooks must run unconditionally: these sit above the early return so
  // toggling the panel never changes the hook order (React error #310).
  const detectedUrl = useMemo(() => findDetectedServer(events), [events])

  useEffect(() => {
    const onMove = (event: MouseEvent) => {
      if (!dragging.current) return
      const next = window.innerWidth - event.clientX
      // Keep the transcript at least ~460px wide so the split stays usable.
      setWidth(Math.max(340, Math.min(next, window.innerWidth - 460)))
    }
    const onUp = () => {
      dragging.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
  }, [])

  if (!open) return null

  const effectiveUrl = url || detectedUrl || ''

  return (
    <aside className="sidecar" style={{ width }} aria-label="Preview panel">
      <div
        className="sidecar__grip"
        onMouseDown={() => {
          dragging.current = true
          document.body.style.cursor = 'col-resize'
          document.body.style.userSelect = 'none'
        }}
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize preview panel"
      />

      <div className="sidecar__header">
        <div className="sidecar__tabs" role="tablist" aria-label="Preview mode">
          <button
            role="tab"
            aria-selected={mode === 'web'}
            data-active={mode === 'web'}
            onClick={() => onModeChange('web')}
            title="Web preview"
          >
            <Globe size={13} />
            <span>Web</span>
          </button>
          <button
            role="tab"
            aria-selected={mode === 'terminal'}
            data-active={mode === 'terminal'}
            onClick={() => onModeChange('terminal')}
            title="Terminal mirror"
          >
            <Terminal size={13} />
            <span>Terminal</span>
          </button>
          <button
            role="tab"
            aria-selected={mode === 'diff'}
            data-active={mode === 'diff'}
            onClick={() => onModeChange('diff')}
            title="Diff view"
          >
            <Columns2 size={13} />
            <span>Diff</span>
          </button>
        </div>
        <button
          className="onion-iconbtn"
          onClick={onClose}
          title="Close preview"
          aria-label="Close preview"
          style={{ width: 30, height: 30 }}
        >
          <X size={15} />
        </button>
      </div>

      {mode === 'web' && (
        <WebPreview
          url={effectiveUrl}
          detectedUrl={detectedUrl}
          onUrlChange={onUrlChange}
          onPickedElement={onPickedElement}
        />
      )}
      {mode === 'terminal' && <TerminalMirror events={events} />}
      {mode === 'diff' && (
        <WorkspaceDiff
          taskId={taskId}
          proposals={proposals}
          events={events}
          open={open}
          focusPath={focusPath}
        />
      )}
    </aside>
  )
}

/* ------------------------------------------------------------------- web */

function WebPreview({
  url,
  detectedUrl,
  onUrlChange,
  onPickedElement,
}: {
  url: string
  detectedUrl: string | null
  onUrlChange: (url: string) => void
  onPickedElement: (snippet: string) => void
}) {
  const frameRef = useRef<HTMLIFrameElement>(null)
  const [picking, setPicking] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Inside the iframe we cannot read the DOM cross-origin, so the picker script
  // is injected by the served page itself when same-origin; otherwise we fall
  // back to a coordinate hint that tells the user to copy the selector manually.
  const startPick = () => {
    setPicking(true)
    setError(null)
    try {
      const doc = frameRef.current?.contentDocument
      if (!doc) {
        setError('Cross-origin preview: open it in a tab to inspect elements.')
        return
      }
      const handler = (event: MouseEvent) => {
        const target = event.target as HTMLElement | null
        if (!target) return
        event.preventDefault()
        event.stopPropagation()
        onPickedElement(`Inspect ${pathOf(target)} on ${url} — `)
        doc.removeEventListener('click', handler, true)
        setPicking(false)
      }
      doc.addEventListener('click', handler, true)
    } catch {
      setError('Cross-origin preview: open it in a tab to inspect elements.')
    }
  }

  return (
    <div className="sidecar__body sidecar__body--web">
      <div className="sidecar__urlbar">
        <Globe size={13} />
        <input
          value={url}
          onChange={(event) => onUrlChange(event.target.value)}
          placeholder="http://localhost:5173"
          spellCheck={false}
        />
        <button
          className="onion-iconbtn"
          title="Reload preview"
          aria-label="Reload preview"
          style={{ width: 28, height: 28 }}
          onClick={() => {
            const frame = frameRef.current
            if (frame) frame.src = frame.src
          }}
        >
          <RefreshCw size={13} />
        </button>
        <button
          className="onion-iconbtn"
          data-active={picking}
          title="Pick an element"
          aria-label="Pick an element"
          style={{ width: 28, height: 28 }}
          onClick={startPick}
        >
          <Crosshair size={13} />
        </button>
        <button
          className="onion-iconbtn"
          title="Open in browser"
          aria-label="Open in browser"
          style={{ width: 28, height: 28 }}
          onClick={() => url && window.open(url, '_blank')}
        >
          <ExternalLink size={13} />
        </button>
      </div>

      {detectedUrl && url !== detectedUrl && (
        <button className="sidecar__detect" onClick={() => onUrlChange(detectedUrl)}>
          <Globe size={12} /> Dev server detected: {detectedUrl} — use it
        </button>
      )}

      {url ? (
        <iframe
          ref={frameRef}
          className="sidecar__frame"
          src={url}
          title="Web preview"
          sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
        />
      ) : (
        <div className="sidecar__empty">
          <Globe size={28} />
          <p>No preview yet.</p>
          <span>Start a local dev server and its URL will appear here automatically.</span>
        </div>
      )}

      {error && <div className="sidecar__note">{error}</div>}
    </div>
  )
}

function pathOf(element: HTMLElement): string {
  const parts: string[] = []
  let node: HTMLElement | null = element
  while (node && node.nodeType === 1 && parts.length < 4) {
    let part = node.tagName.toLowerCase()
    if (node.id) {
      part += `#${node.id}`
      parts.unshift(part)
      break
    }
    if (node.className && typeof node.className === 'string') {
      const cls = node.className.trim().split(/\s+/).slice(0, 2).join('.')
      if (cls) part += `.${cls}`
    }
    parts.unshift(part)
    node = node.parentElement
  }
  return parts.join(' > ')
}

/* -------------------------------------------------------------- terminal */

function TerminalMirror({ events }: { events: OnionEvent[] }) {
  const [buffer, setBuffer] = useState<string[]>([])
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    setBuffer(
      events
        .filter((event) => event.type === 'tool-output')
        .map((event) => event.payload.text as string)
        .flatMap((text) => String(text ?? '').split(/\r?\n/))
        .filter((line) => line.length > 0),
    )
  }, [events])

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' })
  }, [buffer.length])

  return (
    <div className="sidecar__body sidecar__body--terminal">
      <div className="terminal">
        <div className="terminal__head">
          <span className="terminal__dot" data-state="live" />
          <span>stdout / stderr</span>
          <span className="spacer" style={{ flex: 1 }} />
          <span className="terminal__count">{buffer.length} lines</span>
        </div>
        <div className="terminal__body">
          {buffer.length === 0 && (
            <span className="terminal__idle">Waiting for executor output…</span>
          )}
          {buffer.map((line, index) => (
            <div key={index} className="terminal__line">
              {line}
            </div>
          ))}
          <div ref={endRef} />
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ diff */

/**
 * The workspace panel: a file browser plus what this task changed.
 *
 * The previous version only listed ChangeLedger proposals, so files the agent
 * edited during normal work were invisible. This shows the workspace itself and
 * the diff of the current task, with two ways in: pick a changed file from the
 * task list, or browse any file in the tree.
 */
function WorkspaceDiff({
  taskId,
  proposals,
  events,
  open,
  focusPath,
}: {
  taskId: string | null
  proposals: ChangeRecord[]
  events: OnionEvent[]
  open: boolean
  focusPath: string
}) {
  const { changes: taskChanges, loading, note } = useTaskDiff(taskId, open)
  const [tab, setTab] = useState<'changes' | 'browse'>('changes')
  const [selectedFile, setSelectedFile] = useState<string>('')
  const [view, setView] = useState<FileView | null>(null)
  const [viewError, setViewError] = useState('')

  // Files flagged by the task event, so the tree can mark them even when the
  // diff request has not landed yet.
  const changedPaths = useMemo(() => {
    const set = new Set<string>()
    for (const change of taskChanges) set.add(change.path)
    for (const event of events) {
      if (event.type !== 'file-changed') continue
      for (const file of event.payload.files || []) {
        if (file?.path) set.add(String(file.path))
      }
    }
    return set
  }, [taskChanges, events])

  const ledger = useMemo(() => {
    const fromEvents = events
      .filter((event) => event.type === 'change-proposal' && event.payload.change)
      .map((event) => event.payload.change as ChangeRecord)
    const seen = new Set<string>()
    return [...proposals, ...fromEvents].filter((change) => {
      if (seen.has(change.id)) return false
      seen.add(change.id)
      return true
    })
  }, [proposals, events])

  const openFile = useCallback((path: string) => {
    setSelectedFile(path)
    setViewError('')
    api
      .workspaceFile(path)
      .then((data) => setView(data))
      .catch((exc) => {
        setView(null)
        setViewError(exc instanceof Error ? exc.message : String(exc))
      })
  }, [])

  // A change card opens the panel already focused on its file.
  useEffect(() => {
    if (open && focusPath) setSelectedFile(focusPath)
  }, [open, focusPath])

  useEffect(() => {
    if (open && selectedFile) openFile(selectedFile)
  }, [open, selectedFile, openFile])

  const selectedChange = taskChanges.find((change) => change.path === selectedFile)

  return (
    <div className="sidecar__body sidecar__body--diff">
      <div className="ws-tabs">
        <button
          type="button"
          data-active={tab === 'changes'}
          onClick={() => setTab('changes')}
        >
          This task {taskChanges.length > 0 && `(${taskChanges.length})`}
        </button>
        <button type="button" data-active={tab === 'browse'} onClick={() => setTab('browse')}>
          Files
        </button>
      </div>

      {tab === 'browse' ? (
        <div className="ws-split">
          <FileBrowser
            onSelect={(entry) => openFile(entry.path)}
            changedPaths={changedPaths}
            selectedPath={selectedFile}
            taskId={taskId || ''}
          />
          <div className="ws-viewer">
            {!selectedFile && (
              <div className="sidecar__empty">
                <Columns2 size={24} />
                <p>Pick a file to view it.</p>
                <span>Files changed by this task are marked with a dot.</span>
              </div>
            )}
            {selectedFile && viewError && <div className="sidecar__note">{viewError}</div>}
            {selectedFile && view && (
              <FileViewer
                path={view.path}
                text={view.text}
                language={view.language}
                binary={view.binary}
                truncated={view.truncated}
                error={view.error}
              />
            )}
          </div>
        </div>
      ) : (
        <div className="ws-changes">
          {loading && <span className="monitor-empty">Comparing the workspace…</span>}
          {!loading && note && <div className="sidecar__note">{note}</div>}

          {selectedChange && (
            <div className="ws-changes__detail">
              <DiffBlock change={selectedChange} />
            </div>
          )}

          {!selectedChange && taskChanges.length > 0 && (
            <DiffBlocks changes={taskChanges} />
          )}

          {!loading && taskChanges.length === 0 && !note && (
            <div className="sidecar__empty">
              <Columns2 size={26} />
              <p>This task did not change any files.</p>
              <span>Edits the agent makes in the workspace appear here with a diff.</span>
            </div>
          )}

          {ledger.length > 0 && (
            <div className="ws-ledger">
              <span className="popover-section__label">Onion changes</span>
              {ledger.map((item) => (
                <div key={item.id} className="ws-ledger__row">
                  <span className="badge" data-status="proposed">{item.kind}</span>
                  <span>{item.label}</span>
                  <small>{item.target}</small>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function stringify(value: any): string {
  if (value === null || value === undefined) return '(empty)'
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

/* --------------------------------------------------------------- helpers */

function findDetectedServer(events: OnionEvent[]): string | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]
    const text = JSON.stringify(event.payload || {})
    const match = text.match(/https?:\/\/(?:localhost|127\.0\.0\.1)(?::\d+)?[\w\-./?=#&%]*/i)
    if (match) return match[0].replace(/["\\]+$/, '')
  }
  return null
}