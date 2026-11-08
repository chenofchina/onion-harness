import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ChevronDown,
  ChevronRight,
  FileCode,
  FileText,
  Folder,
  FolderOpen,
  RefreshCw,
} from 'lucide-react'
import { api, type FileChange, type FileEntry, type FileView } from '../lib/api'

/**
 * Workspace file browser.
 *
 * Loads one directory level at a time rather than walking the whole tree: a
 * repository root can hold thousands of files, and the panel only ever shows a
 * handful of rows. Directories expand on click.
 *
 * The path is always sent relative to the workspace root and re-validated on the
 * server, so this component cannot be used to read outside it even if a crafted
 * entry appeared.
 */

function fileIcon(name: string) {
  const ext = name.split('.').pop()?.toLowerCase() || ''
  if (['py', 'ts', 'tsx', 'js', 'jsx', 'rs', 'go', 'java', 'c', 'cpp', 'cs', 'rb'].includes(ext)) {
    return <FileCode size={13} />
  }
  return <FileText size={13} />
}

type Node = FileEntry

export function FileBrowser({
  onSelect,
  changedPaths,
  selectedPath,
  taskId = '',
}: {
  onSelect: (entry: FileEntry) => void
  changedPaths: Set<string>
  selectedPath: string
  taskId?: string
}) {
  const [root, setRoot] = useState('')
  const [children, setChildren] = useState<Record<string, Node[]>>({})
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const loadDir = useCallback(
    async (path: string) => {
    setLoading(true)
    try {
      const data = await api.workspaceTree(path, 1, taskId)
      setRoot(data.root)
      setChildren((prev) => ({ ...prev, [path]: data.entries }))
      setError(data.error || '')
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setLoading(false)
    }
    },
    [taskId],
  )

  useEffect(() => {
    loadDir('')
    // Only on mount: the tree is refreshed explicitly or by following a selection.
  }, [loadDir])

  // Reveal a file the transcript linked to, even if its folders are closed.
  const lastRevealed = useRef('')
  useEffect(() => {
    if (!selectedPath || selectedPath === lastRevealed.current) return
    lastRevealed.current = selectedPath
    const parts = selectedPath.split('/')
    const dirs = parts.slice(0, -1).reduce<string[]>((acc, _part, index) => {
      acc.push(parts.slice(0, index + 1).join('/'))
      return acc
    }, [])
    Promise.all(dirs.map((dir) => loadDir(dir))).then(() => {
      setExpanded((prev) => {
        const next = { ...prev }
        for (const dir of dirs) next[dir] = true
        return next
      })
    })
  }, [selectedPath, loadDir])

  const toggle = (entry: Node) => {
    if (!entry.is_dir) {
      onSelect(entry)
      return
    }
    const next = !expanded[entry.path]
    setExpanded((prev) => ({ ...prev, [entry.path]: next }))
    if (next && !children[entry.path]) void loadDir(entry.path)
  }

  const renderLevel = (path: string, depth: number): React.ReactNode => {
    const entries = children[path]
    if (!entries) return null
    return entries.map((entry) => {
      const open = Boolean(expanded[entry.path])
      const isChanged = changedPaths.has(entry.path)
      return (
        <div key={entry.path}>
          <button
            type="button"
            className="ws-row"
            data-dir={entry.is_dir}
            data-selected={!entry.is_dir && entry.path === selectedPath}
            data-changed={isChanged}
            style={{ paddingLeft: 8 + depth * 13 }}
            onClick={() => toggle(entry)}
            title={entry.path}
          >
            {entry.is_dir ? (
              open ? (
                <ChevronDown size={12} />
              ) : (
                <ChevronRight size={12} />
              )
            ) : (
              <span className="ws-row__spacer" />
            )}
            {entry.is_dir ? (
              open ? (
                <FolderOpen size={13} />
              ) : (
                <Folder size={13} />
              )
            ) : (
              fileIcon(entry.name)
            )}
            <span className="ws-row__name">{entry.name}</span>
            {isChanged && <span className="ws-row__dot" title="Changed in this task" />}
          </button>
          {entry.is_dir && open && renderLevel(entry.path, depth + 1)}
        </div>
      )
    })
  }

  return (
    <div className="ws-browser">
      <div className="ws-browser__head">
        <span className="ws-browser__title" title={root}>
          {root ? root.split(/[\\/]/).pop() : 'Workspace'}
        </span>
        <button
          type="button"
          className="onion-iconbtn"
          onClick={() => {
            setChildren({})
            setExpanded({})
            void loadDir('')
          }}
          title="Reload"
          aria-label="Reload workspace"
          style={{ width: 24, height: 24 }}
        >
          <RefreshCw size={12} className={loading ? 'spin' : undefined} />
        </button>
      </div>
      <div className="ws-browser__body">
        {error && <div className="ws-browser__error">{error}</div>}
        {!error && !children[''] && <span className="monitor-empty">Loading…</span>}
        {!error && children['']?.length === 0 && (
          <span className="monitor-empty">This workspace is empty.</span>
        )}
        {renderLevel('', 0)}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------- file viewer */

export function FileViewer({
  path,
  text,
  language,
  binary,
  truncated,
  error,
}: {
  path: string
  text: string
  language: string
  binary: boolean
  truncated: boolean
  error: string
}) {
  const lines = useMemo(() => text.split('\n'), [text])
  return (
    <div className="file-view">
      <div className="file-view__head">
        <span className="file-view__path">{path}</span>
        {language && <span className="impact-chip">{language}</span>}
      </div>
      {error ? (
        <div className="sidecar__note">{error}</div>
      ) : binary ? (
        <div className="sidecar__note">Binary file — not shown as text.</div>
      ) : (
        <>
          <pre className="file-view__code">
            {lines.map((line, index) => (
              <div key={index} className="file-view__line">
                <span className="file-view__gutter">{index + 1}</span>
                <span className="file-view__text">{line || ' '}</span>
              </div>
            ))}
          </pre>
          {truncated && (
            <div className="sidecar__note">This file is large; only the beginning is shown.</div>
          )}
        </>
      )}
    </div>
  )
}

/* ---------------------------------------------------------------- diff view */

/** Colour-coded unified diff. Additions and removals are separate rows. */
export function DiffBlocks({ changes }: { changes: FileChange[] }) {
  if (!changes.length) return null
  return (
    <div className="diff-list">
      {changes.map((change) => (
        <DiffBlock key={`${change.kind}-${change.path}`} change={change} />
      ))}
    </div>
  )
}

export function DiffBlock({ change }: { change: FileChange }) {
  const [open, setOpen] = useState(change.kind !== 'deleted')
  const badge =
    change.kind === 'added' ? 'added' : change.kind === 'deleted' ? 'deleted' : 'modified'

  return (
    <div className="diff-block" data-kind={change.kind}>
      <button type="button" className="diff-block__head" onClick={() => setOpen((v) => !v)}>
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <span className="diff-block__path">{change.path}</span>
        <span className="badge" data-status={change.kind === 'deleted' ? 'failed' : 'success'}>
          {badge}
        </span>
      </button>
      {open && (
        <pre className="diff-block__body">
          {(change.diff || '').split('\n').map((line, index) => {
            const kind = line.startsWith('+++') || line.startsWith('---')
              ? 'meta'
              : line.startsWith('+')
                ? 'add'
                : line.startsWith('-')
                  ? 'del'
                  : line.startsWith('@@')
                    ? 'hunk'
                    : 'ctx'
            return (
              <div key={index} className="diff-line" data-kind={kind}>
                {line || ' '}
              </div>
            )
          })}
          {!change.diff && (
            <div className="diff-line" data-kind="ctx">
              (no textual diff available)
            </div>
          )}
        </pre>
      )}
    </div>
  )
}

/**
 * Inline card shown in the transcript when a task touched files.
 *
 * This is the entry point that was missing: a change the agent made used to be
 * invisible unless the user went looking. The card names the files and links to
 * the diff, so the transcript itself is the record.
 */
export function FileChangeCard({
  files,
  onOpen,
}: {
  files: { path: string; kind: string; added?: number; removed?: number }[]
  onOpen: (path?: string) => void
}) {
  if (!files.length) return null
  return (
    <div className="card file-change-card">
      <div className="file-change-card__head">
        <span className="card__header-icon">
          <FileCode size={16} />
        </span>
        <span className="file-change-card__title">
          {files.length} file{files.length === 1 ? '' : 's'} changed
        </span>
        <span style={{ flex: 1 }} />
        <button className="btn" data-variant="text" onClick={() => onOpen()}>
          View diff
        </button>
      </div>
      <div className="file-change-card__list">
        {files.map((file) => (
          <button
            key={file.path}
            type="button"
            className="file-change-card__row"
            onClick={() => onOpen(file.path)}
            title={file.path}
          >
            <span className="badge" data-status={file.kind === 'deleted' ? 'failed' : 'success'}>
              {file.kind}
            </span>
            <span className="file-change-card__path">{file.path}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

/** Loads one file's diff for the preview panel. */
export function useTaskDiff(taskId: string | null, open: boolean) {
  const [changes, setChanges] = useState<FileChange[]>([])
  const [loading, setLoading] = useState(false)
  const [note, setNote] = useState('')

  useEffect(() => {
    if (!taskId || !open) return
    let cancelled = false
    setLoading(true)
    api
      .taskFiles(taskId)
      .then((data) => {
        if (cancelled) return
        setChanges(data.changes || [])
        setNote(data.note || '')
      })
      .catch((exc) => {
        if (!cancelled) setNote(exc instanceof Error ? exc.message : String(exc))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [taskId, open])

  return { changes, loading, note }
}

export type { FileView }