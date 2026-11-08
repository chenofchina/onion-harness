import { useEffect, useMemo, useState } from 'react'
import {
  Archive,
  Check,
  Cloud,
  Globe,
  KeyRound,
  Puzzle,
  RotateCcw,
  ShieldCheck,
  Trash2,
  X,
} from 'lucide-react'
import {
  api,
  type AutoReviewConfig,
  type ProviderPreset,
  type Revision,
  type Settings,
  type ThinkingLevel,
  type WebSearchConfig,
} from '../lib/api'
import { LobeIcon } from './LobeIcon'
import { useTheme, type ThemeMode } from '../lib/theme'

/* ---------------------------------------------------------------- modal */

export function Modal({
  title,
  onClose,
  children,
  footer,
  wide,
}: {
  title: string
  onClose: () => void
  children: React.ReactNode
  footer?: React.ReactNode
  wide?: boolean
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${wide ? 'modal--wide' : ''}`} role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal__header">
          <h2>{title}</h2>
          <button className="onion-iconbtn" onClick={onClose} aria-label="Close" title="Close" style={{ width: 32, height: 32 }}>
            <X size={17} />
          </button>
        </div>
        {children}
        {footer && <div className="modal__footer">{footer}</div>}
      </div>
    </div>
  )
}

/* ----------------------------------------------------------- model picker */

/**
 * Discrete context-window steps.
 *
 * A continuous 1..1,000,000 slider would be unusable: almost every pixel would
 * land on a value nobody wants. These are the real limits providers ship.
 */
const CONTEXT_STEPS = [
  { tokens: 8_000, label: '8k' },
  { tokens: 16_000, label: '16k' },
  { tokens: 32_000, label: '32k' },
  { tokens: 64_000, label: '64k' },
  { tokens: 128_000, label: '128k' },
  { tokens: 200_000, label: '200k' },
  { tokens: 256_000, label: '256k' },
  { tokens: 512_000, label: '512k' },
  { tokens: 1_000_000, label: '1M' },
]

/** Nearest step index for an arbitrary token value. */
function contextStep(tokens: number): number {
  let best = 0
  let bestGap = Number.POSITIVE_INFINITY
  CONTEXT_STEPS.forEach((step, index) => {
    const gap = Math.abs(step.tokens - tokens)
    if (gap < bestGap) {
      bestGap = gap
      best = index
    }
  })
  return best
}

function formatTokens(tokens: number): string {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`
  if (tokens >= 1_000) return `${Math.round(tokens / 1_000)}k`
  return String(tokens)
}



export function ModelPicker({
  providers,
  thinkingLevels,
  settings,
  onClose,
  onSaved,
}: {
  providers: ProviderPreset[]
  thinkingLevels: ThinkingLevel[]
  settings: Settings
  onClose: () => void
  onSaved: (settings: Settings) => void
}) {
  const [providerId, setProviderId] = useState(settings.provider_id || providers[0]?.id || 'deepseek')
  const [model, setModel] = useState(settings.model)
  const [thinking, setThinking] = useState(settings.thinking || 'off')
  const [baseUrl, setBaseUrl] = useState(
    settings.base_urls?.[settings.provider_id] ?? settings.base_url ?? '',
  )
  const [contextWindow, setContextWindow] = useState(settings.context_window ?? 128000)
  const [keepRecent, setKeepRecent] = useState(settings.keep_recent_messages ?? 10)
  const [apiKey, setApiKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const provider = useMemo(
    () => providers.find((p) => p.id === providerId) || providers[0],
    [providers, providerId],
  )
  const configured = settings.configured_providers?.includes(provider?.id || '') ?? false

  useEffect(() => {
    if (!provider) return
    if (!provider.models.includes(model)) {
      setModel(provider.models[0] || '')
    }
  }, [provider]) // eslint-disable-line react-hooks/exhaustive-deps

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      if (apiKey.trim()) {
        await api.saveApiKey(provider.id, apiKey.trim())
      }
      const typedUrl = baseUrl.trim()
      const next = await api.updateSettings({
        provider_id: provider.id,
        model,
        thinking,
        context_window: contextWindow,
        keep_recent_messages: keepRecent,
        // Blank means "use the preset default"; anything else is the override.
        base_url: typedUrl && typedUrl !== provider.base_url ? typedUrl : '',
      })
      onSaved(next)
      onClose()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  const forget = async () => {
    setBusy(true)
    try {
      const next = await api.clearApiKey(provider.id)
      onSaved(next)
    } finally {
      setBusy(false)
    }
  }

  if (!provider) return null

  return (
    <Modal
      title="Model"
      onClose={onClose}
      footer={
        <>
          <button className="btn" data-variant="text" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button className="btn" data-variant="filled" onClick={save} disabled={busy || !model}>
            <Check size={14} /> Save
          </button>
        </>
      }
    >
      <div className="modal__body">
        <div className="field">
          <label>Provider</label>
          <div className="provider-grid">
            {providers.map((preset) => (
              <button
                key={preset.id}
                className="provider-card"
                data-selected={preset.id === provider.id}
                onClick={() => {
                  setProviderId(preset.id)
                  setBaseUrl(settings.base_urls?.[preset.id] ?? '')
                }}
                type="button"
              >
                <LobeIcon name={preset.icon} size={18} />
                <span className="provider-card__meta">
                  <span>{preset.label}</span>
                  <small>
                    {settings.configured_providers?.includes(preset.id)
                      ? 'key saved'
                      : preset.format === 'anthropic'
                        ? 'anthropic'
                        : 'openai-compatible'}
                  </small>
                </span>
              </button>
            ))}
          </div>
        </div>

        <div className="field">
          <label>Base URL</label>
          <input
            type="text"
            value={baseUrl || provider.base_url}
            onChange={(event) => setBaseUrl(event.target.value)}
            placeholder={provider.base_url || 'https://api.example.com/v1'}
            spellCheck={false}
          />
          <span className="field__hint">
            {provider.format === 'anthropic'
              ? 'Anthropic Messages API (translated to the OpenAI-compatible surface).'
              : 'OpenAI-compatible chat completions endpoint.'}
            {provider.notes ? ` ${provider.notes}` : ''}
            {' '}Edit this to use a proxy or a self-hosted gateway.
          </span>
        </div>

        <div className="field">
          <label>Model</label>
          <input
            type="text"
            value={model}
            onChange={(event) => setModel(event.target.value)}
            placeholder="model id"
            list="onion-model-options"
          />
          <datalist id="onion-model-options">
            {provider.models.map((item) => (
              <option key={item} value={item} />
            ))}
          </datalist>
        </div>

        <div className="field">
          <label>API key</label>
          <div className="row">
            <input
              type="password"
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
              placeholder={configured ? '•••••••• saved — enter to replace' : 'sk-…'}
              style={{ flex: 1 }}
            />
            {configured && (
              <button className="btn" data-variant="outlined" onClick={forget} disabled={busy} title="Forget key">
                <Trash2 size={14} />
              </button>
            )}
          </div>
          <span className="field__hint">
            <KeyRound size={11} style={{ verticalAlign: -1, marginRight: 4 }} />
            Stored locally in your user data directory. Never sent anywhere except the provider you chose.
            {provider.api_key_url ? (
              <>
                {' '}
                Get a key at {provider.api_key_url}
              </>
            ) : null}
          </span>
        </div>

        <div className="field">
          <label>Thinking depth</label>
          <div className="segmented">
            {thinkingLevels.map((level) => (
              <button
                key={level.id}
                data-active={thinking === level.id}
                onClick={() => setThinking(level.id)}
                type="button"
              >
                {level.label}
              </button>
            ))}
          </div>
          <span className="field__hint">
            Extended thinking is sent to Anthropic as a budget, and to OpenAI reasoning
            models as an effort hint. Other providers ignore it.
          </span>
        </div>

        <div className="field">
          <label className="rt-label-row">
            <span>Context window</span>
            <span className="rt-slider__value">{formatTokens(contextWindow)}</span>
          </label>
          <div className="rt-slider">
            <input
              type="range"
              min={0}
              max={CONTEXT_STEPS.length - 1}
              step={1}
              value={contextStep(contextWindow)}
              onChange={(event) =>
                setContextWindow(CONTEXT_STEPS[Number(event.target.value)].tokens)
              }
              aria-label="Context window"
              style={{
                ['--rt-progress' as string]: `${
                  (contextStep(contextWindow) / (CONTEXT_STEPS.length - 1)) * 100
                }%`,
              }}
            />
            <div className="rt-slider__marks" aria-hidden>
              <span>8k</span>
              <span>128k</span>
              <span>512k</span>
              <span>1M</span>
            </div>
          </div>
          <span className="field__hint">
            How much conversation the model is assumed to hold. Older turns beyond this
            are summarised. Use the provider&apos;s real limit; the meter in the top bar
            tracks against it.
          </span>
        </div>

        <div className="field">
          <label>Messages kept verbatim</label>
          <div className="rt-slider rt-slider--inline">
            <input
              type="range"
              min={2}
              max={40}
              step={1}
              value={keepRecent}
              onChange={(event) => setKeepRecent(Number(event.target.value))}
              aria-label="Messages kept verbatim"
              style={{ ['--rt-progress' as string]: `${((keepRecent - 2) / 38) * 100}%` }}
            />
            <span className="rt-slider__value">{keepRecent}</span>
          </div>
          <span className="field__hint">
            The most recent messages sent unmodified; anything older is compressed into a
            summary in the system channel.
          </span>
        </div>

        {error && <div className="card-change__warn">{error}</div>}
      </div>
    </Modal>
  )
}

/* -------------------------------------------------------------- settings */

export function SettingsDialog({
  settings,
  onClose,
  onSaved,
  dataDir,
  archivedCount = 0,
  mcpCount = 0,
  onOpenArchive,
  onOpenMcp,
  autoReview,
  onSaveAutoReview,
  webSearch,
  onSaveWebSearch,
}: {
  settings: Settings
  onClose: () => void
  onSaved: (settings: Settings) => void
  dataDir: string
  archivedCount?: number
  mcpCount?: number
  onOpenArchive?: () => void
  onOpenMcp?: () => void
  autoReview?: AutoReviewConfig
  onSaveAutoReview?: (patch: Partial<AutoReviewConfig>) => Promise<void>
  webSearch?: WebSearchConfig
  onSaveWebSearch?: (patch: Partial<WebSearchConfig>) => Promise<void>
}) {
  const { mode, setMode } = useTheme()
  const [workspace, setWorkspace] = useState(settings.workspace)
  const [maxTurns, setMaxTurns] = useState(settings.max_turns)
  const [keepRecent, setKeepRecent] = useState(settings.keep_recent_messages)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [review, setReview] = useState<AutoReviewConfig | undefined>(autoReview)
  const [search, setSearch] = useState<WebSearchConfig | undefined>(webSearch)

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      const next = await api.updateSettings({
        workspace,
        max_turns: Number(maxTurns),
        keep_recent_messages: Number(keepRecent),
        theme_mode: mode,
      })
      if (review && onSaveAutoReview) await onSaveAutoReview(review)
      if (search && onSaveWebSearch) await onSaveWebSearch(search)
      onSaved(next)
      onClose()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  const themes: { id: ThemeMode; label: string }[] = [
    { id: 'system', label: 'System' },
    { id: 'light', label: 'Light' },
    { id: 'dark', label: 'Dark' },
  ]

  return (
    <Modal
      title="Settings"
      onClose={onClose}
      footer={
        <>
          <button className="btn" data-variant="text" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button className="btn" data-variant="filled" onClick={save} disabled={busy}>
            <Check size={14} /> Save
          </button>
        </>
      }
    >
      <div className="modal__body">
        <div className="field">
          <label>Appearance</label>
          <div className="segmented">
            {themes.map((theme) => (
              <button key={theme.id} data-active={mode === theme.id} onClick={() => setMode(theme.id)} type="button">
                {theme.label}
              </button>
            ))}
          </div>
          <span className="field__hint">Follows the Material 3 dynamic colour scheme.</span>
        </div>

        <div className="field">
          <label>Default working directory</label>
          <input
            type="text"
            value={workspace}
            onChange={(event) => setWorkspace(event.target.value)}
            placeholder="C:\\path\\to\\workspace"
          />
          <span className="field__hint">
            Commands run with this as their working directory. Leave empty to use a temp directory.
          </span>
        </div>

        <div className="field">
          <label>Max agent turns</label>
          <input
            type="number"
            min={1}
            max={200}
            value={maxTurns}
            onChange={(event) => setMaxTurns(Number(event.target.value))}
          />
          <span className="field__hint">Hard stop for a single task.</span>
        </div>

        <div className="field">
          <label>Context: keep recent messages</label>
          <input
            type="number"
            min={2}
            max={50}
            value={keepRecent}
            onChange={(event) => setKeepRecent(Number(event.target.value))}
          />
          <span className="field__hint">
            Older turns are summarised through the system channel so instructions survive compression.
          </span>
        </div>

        <div className="field">
          <label>Data directory</label>
          <input type="text" value={dataDir} readOnly />
          <span className="field__hint">Task history, settings and the change ledger live here.</span>
        </div>

        {review && (
          <div className="field">
            <label>Safety review</label>
            <div className="row" style={{ gap: 8, alignItems: 'center' }}>
              <button
                className="btn"
                data-variant={review.enabled ? 'filled' : 'outlined'}
                onClick={() => setReview({ ...review, enabled: !review.enabled })}
                type="button"
              >
                <ShieldCheck size={14} /> {review.enabled ? 'On' : 'Off'}
              </button>
              <span className="text-muted" style={{ fontSize: '0.78rem' }}>
                {review.model} · timeout {review.timeout_seconds}s
              </span>
            </div>
            <span className="field__hint">
              Before a destructive command runs, a cheap model reviews it and answers
              PASS or REJECT. Only destructive snippets are checked, so reads cost
              nothing extra. If the reviewer cannot be reached the call is refused
              rather than run unchecked.
            </span>
          </div>
        )}

        {search && (
          <div className="field">
            <label>Web search</label>
            <div className="row" style={{ gap: 8, alignItems: 'center' }}>
              <button
                className="btn"
                data-variant={search.enabled ? 'filled' : 'outlined'}
                onClick={() => setSearch({ ...search, enabled: !search.enabled })}
                type="button"
              >
                <Globe size={14} /> {search.enabled ? 'On' : 'Off'}
              </button>
              <input
                type="text"
                value={search.base_url}
                onChange={(event) => setSearch({ ...search, base_url: event.target.value })}
                placeholder="http://localhost:8080"
                style={{ flex: 1 }}
              />
              <input
                type="number"
                min={1}
                max={30}
                value={search.timeout}
                onChange={(event) =>
                  setSearch({ ...search, timeout: Number(event.target.value) || 5 })
                }
                style={{ width: 72 }}
                title="Timeout in seconds"
              />
            </div>
            <span className="field__hint">
              The agent searches the live web through a local search-neo service that
              aggregates Baidu and Bing. No cloud key is involved. If the service is
              unreachable the agent is told the search failed rather than allowed to
              answer from memory.
            </span>
          </div>
        )}

        {(onOpenArchive || onOpenMcp) && (
          <div className="field">
            <label>Library</label>
            <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
              {onOpenArchive && (
                <button className="btn" data-variant="outlined" onClick={onOpenArchive}>
                  <Archive size={14} /> Archived tasks ({archivedCount})
                </button>
              )}
              {onOpenMcp && (
                <button className="btn" data-variant="outlined" onClick={onOpenMcp}>
                  <Puzzle size={14} /> MCP tools ({mcpCount})
                </button>
              )}
            </div>
          </div>
        )}

        {error && <div className="card-change__warn">{error}</div>}
      </div>
    </Modal>
  )
}

/* --------------------------------------------------------- change history */

export function HistoryDialog({
  revisions,
  onClose,
  onRevert,
  onRedo,
  busy,
}: {
  revisions: Revision[]
  onClose: () => void
  onRevert: (version: number) => void
  onRedo: () => void
  busy: boolean
}) {
  const reverted = revisions.filter((revision) => revision.reverted_at !== null)

  return (
    <Modal
      title="Change history"
      onClose={onClose}
      wide
      footer={
        <>
          <button className="btn" data-variant="outlined" onClick={onRedo} disabled={busy || reverted.length === 0}>
            <RotateCcw size={14} /> Redo ({reverted.length})
          </button>
          <button className="btn" data-variant="filled" onClick={onClose}>
            Close
          </button>
        </>
      }
    >
      <div className="modal__body">
        {revisions.length === 0 && <span className="monitor-empty">No changes have been applied yet.</span>}
        <div className="stack">
          {[...revisions].reverse().map((revision) => {
            const isReverted = revision.reverted_at !== null
            return (
              <div key={revision.version} className="history-row" data-reverted={isReverted}>
                <span className="version-tag">v{revision.version}</span>
                <span className="history-row__main">
                  <strong>{revision.change.label}</strong>
                  <small>
                    {revision.change.target} · {new Date(revision.applied_at * 1000).toLocaleString()}
                    {isReverted ? ' · reverted' : ''}
                  </small>
                </span>
                {!isReverted && (
                  <button
                    className="btn"
                    data-variant="outlined"
                    onClick={() => onRevert(revision.version)}
                    disabled={busy}
                  >
                    <RotateCcw size={13} /> Roll back
                  </button>
                )}
              </div>
            )
          })}
        </div>
        <span className="field__hint">
          Rolling back to a version also rolls back every change applied after it. History entries are
          kept, so you can redo them afterwards.
        </span>
      </div>
    </Modal>
  )
}

/* ------------------------------------------------------- workspace picker */

export function WorkspaceDialog({
  value,
  onClose,
  onSave,
}: {
  value: string
  onClose: () => void
  onSave: (path: string) => void
}) {
  const [path, setPath] = useState(value)
  return (
    <Modal
      title="Working directory"
      onClose={onClose}
      footer={
        <>
          <button className="btn" data-variant="text" onClick={onClose}>
            Cancel
          </button>
          <button className="btn" data-variant="filled" onClick={() => onSave(path)}>
            <Check size={14} /> Use this directory
          </button>
        </>
      }
    >
      <div className="modal__body">
        <div className="field">
          <label>Path</label>
          <input
            type="text"
            value={path}
            onChange={(event) => setPath(event.target.value)}
            placeholder="C:\\path\\to\\workspace"
            autoFocus
          />
          <span className="field__hint">
            Commands are executed with this directory as their cwd. It must already exist. Leave it
            empty to run in a temporary directory.
          </span>
        </div>
        <div className="card-change__warn" style={{ color: 'var(--md-sys-color-on-surface-variant)', background: 'color-mix(in srgb, var(--md-sys-color-on-surface) 6%, transparent)' }}>
          <Cloud size={15} style={{ flexShrink: 0, marginTop: 1 }} />
          <span>The sandbox confines commands to this directory but does not virtualise the filesystem yet.</span>
        </div>
      </div>
    </Modal>
  )
}