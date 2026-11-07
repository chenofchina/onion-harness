import { useMemo, useState } from 'react'
import { Check, Cloud, KeyRound, Trash2, X } from 'lucide-react'
import type { ProviderPreset, Settings } from '../lib/api'
import { LobeIcon } from './LobeIcon'

/**
 * Advanced settings as a side drawer.
 *
 * The spec asks that complex configuration (API key, base URL) leave the
 * composer and slide in from the side, so the conversation is never covered by
 * a modal.
 */
export function AdvancedDrawer({
  open,
  providers,
  settings,
  onClose,
  onSaveKey,
  onClearKey,
  onSaveBaseUrl,
  onPickWorkspace,
}: {
  open: boolean
  providers: ProviderPreset[]
  settings: Settings
  onClose: () => void
  onSaveKey: (providerId: string, key: string) => Promise<Settings>
  onClearKey: (providerId: string) => Promise<Settings>
  onSaveBaseUrl: (providerId: string, baseUrl: string) => Promise<Settings>
  onPickWorkspace: () => void
}) {
  const [providerId, setProviderId] = useState(settings.provider_id)
  const [apiKey, setApiKey] = useState('')
  const [baseUrl, setBaseUrl] = useState(
    settings.base_urls?.[settings.provider_id] ?? settings.base_url ?? '',
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  const provider = useMemo(
    () => providers.find((item) => item.id === providerId) ?? providers[0],
    [providers, providerId],
  )
  const configured = settings.configured_providers?.includes(provider?.id ?? '') ?? false

  if (!open) return null

  const save = async () => {
    if (!provider) return
    setBusy(true)
    setError(null)
    try {
      if (apiKey.trim()) await onSaveKey(provider.id, apiKey.trim())
      // Persist the URL the user typed. Previously only the key was written, so
      // a custom or alternative endpoint was silently discarded.
      const typed = baseUrl.trim()
      if (typed && typed !== provider.base_url) {
        await onSaveBaseUrl(provider.id, typed)
      } else if (!typed && settings.base_urls?.[provider.id]) {
        await onSaveBaseUrl(provider.id, '')
      }
      setApiKey('')
      setSaved(true)
      window.setTimeout(() => setSaved(false), 1600)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  const forget = async () => {
    if (!provider) return
    setBusy(true)
    try {
      await onClearKey(provider.id)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="drawer-scrim" onMouseDown={onClose} />
      <aside className="drawer" role="dialog" aria-label="Advanced settings">
        <div className="drawer__header">
          <h2>Advanced settings</h2>
          <button
            className="onion-iconbtn"
            onClick={onClose}
            aria-label="Close advanced settings"
            title="Close"
            style={{ width: 32, height: 32 }}
          >
            <X size={17} />
          </button>
        </div>

        <div className="drawer__body">
          {provider && (
            <>
              <div className="field">
                <label>Provider</label>
                <div className="drawer__providers">
                  {providers.map((preset) => (
                    <button
                      key={preset.id}
                      className="provider-card"
                      data-selected={preset.id === provider.id}
                      onClick={() => {
                        setProviderId(preset.id)
                        setBaseUrl(
                          settings.base_urls?.[preset.id] ?? preset.base_url,
                        )
                      }}
                      type="button"
                    >
                      <LobeIcon name={preset.icon} size={16} />
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
                  placeholder={provider.base_url}
                />
                <span className="field__hint">
                  {provider.format === 'anthropic'
                    ? 'Anthropic Messages API, translated to the OpenAI-compatible surface.'
                    : 'OpenAI-compatible chat completions endpoint.'}
                  {provider.notes ? ` ${provider.notes}` : ''}
                </span>
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
                    <button
                      className="btn"
                      data-variant="outlined"
                      onClick={forget}
                      disabled={busy}
                      title="Forget key"
                    >
                      <Trash2 size={14} />
                    </button>
                  )}
                </div>
                <span className="field__hint">
                  <KeyRound size={11} style={{ verticalAlign: -1, marginRight: 4 }} />
                  Stored in the Windows credential manager, never in a settings file.
                  Only the provider id is written to disk.
                  {provider.api_key_url ? ` Get a key at ${provider.api_key_url}` : ''}
                </span>
              </div>

              <div className="field">
                <label>Workspace</label>
                <button className="btn" data-variant="outlined" onClick={onPickWorkspace}>
                  {settings.workspace || 'Choose a working directory'}
                </button>
              </div>
            </>
          )}

          {error && <div className="card-change__warn">{error}</div>}
          {saved && (
            <div className="drawer__saved">
              <Check size={14} /> Saved
            </div>
          )}

          <div className="card-change__warn" style={{ color: 'var(--md-sys-color-on-surface-variant)', background: 'color-mix(in srgb, var(--md-sys-color-on-surface) 6%, transparent)' }}>
            <Cloud size={15} style={{ flexShrink: 0, marginTop: 1 }} />
            <span>
              Settings are written atomically to ~/.onionharness/settings.json. API keys
              are stored separately in the Windows credential manager, encrypted per user
              account, and are never written to that file.
            </span>
          </div>
        </div>

        <div className="drawer__footer">
          <button className="btn" data-variant="text" onClick={onClose}>
            Close
          </button>
          <button className="btn" data-variant="filled" onClick={save} disabled={busy}>
            <Check size={14} /> Save
          </button>
        </div>
      </aside>
    </>
  )
}