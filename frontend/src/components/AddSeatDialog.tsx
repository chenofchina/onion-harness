import { useState } from 'react'
import { Check, UserPlus } from 'lucide-react'
import type { RoundtableRosterEntry } from '../lib/api'
import { LobeIcon } from './LobeIcon'
import { Modal } from './Dialogs'

/** Add a debater to a meeting that is already running. */
export function AddSeatDialog({
  open,
  roster,
  existingNames,
  onClose,
  onAdd,
}: {
  open: boolean
  roster: RoundtableRosterEntry[]
  existingNames: string[]
  onClose: () => void
  onAdd: (seat: Record<string, unknown>) => void
}) {
  const known = roster.filter((entry) => !entry.custom)
  const usable = known.filter((entry) => entry.has_api_key)
  const first = usable[0] || known[0]
  const [name, setName] = useState('')
  const [providerId, setProviderId] = useState(first?.id || '')
  const [model, setModel] = useState(first?.models[0] || '')
  const [custom, setCustom] = useState(false)
  const [baseUrl, setBaseUrl] = useState('https://')
  const [format, setFormat] = useState('openai')
  const [apiKey, setApiKey] = useState('')

  if (!open) return null

  const preset = roster.find((entry) => entry.id === providerId)
  const ready = custom ? Boolean(baseUrl.startsWith('http') && model.trim()) : Boolean(model)
  const suggestion = `Seat ${existingNames.length + 1}`
  const duplicate = existingNames.some(
    (existing) => existing.trim().toLowerCase() === (name || suggestion).trim().toLowerCase(),
  )

  return (
    <Modal
      title="Add a debater"
      onClose={onClose}
      footer={
        <>
          <button className="btn" data-variant="text" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn"
            data-variant="filled"
            disabled={duplicate || !ready}
            onClick={() =>
              onAdd({
                name: (name || suggestion).trim(),
                provider_id: custom ? 'custom' : providerId,
                model,
                thinking: 'medium',
                wire_format: custom ? format : preset?.format || 'openai',
                base_url: custom ? baseUrl : preset?.base_url || '',
                label: custom ? (name || suggestion).trim() : preset?.label || providerId,
                custom,
                api_key: custom ? apiKey : '',
              })
            }
          >
            <Check size={14} /> Add to roundtable
          </button>
        </>
      }
    >
      <div className="modal__body">
        <div className="field">
          <label>Name</label>
          <input
            type="text"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder={suggestion}
            maxLength={32}
            autoFocus
          />
          {duplicate && <span className="field__hint">That name is already at the table.</span>}
        </div>

        <div className="field">
          <label>Provider</label>
          <select
            value={custom ? 'custom' : providerId}
            onChange={(event) => {
              if (event.target.value === 'custom') {
                setCustom(true)
                return
              }
              setCustom(false)
              setProviderId(event.target.value)
              const next = roster.find((entry) => entry.id === event.target.value)
              setModel(next?.models[0] || '')
            }}
          >
            {known.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.label}
                {entry.has_api_key ? '' : ' (no key)'}
              </option>
            ))}
            <option value="custom">Others…</option>
          </select>
        </div>

        <div className="field">
          <label>Model</label>
          {custom ? (
            <input
              type="text"
              value={model}
              onChange={(event) => setModel(event.target.value)}
              placeholder="model name"
              spellCheck={false}
            />
          ) : (
            <select value={model} onChange={(event) => setModel(event.target.value)}>
              {(preset?.models || []).map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          )}
        </div>

        {custom && (
          <>
            <div className="field">
              <label>Base URL</label>
              <input
                type="text"
                value={baseUrl}
                onChange={(event) => setBaseUrl(event.target.value)}
                placeholder="https://api.example.com/v1"
                spellCheck={false}
              />
            </div>
            <div className="field">
              <label>Wire format</label>
              <select value={format} onChange={(event) => setFormat(event.target.value)}>
                <option value="openai">OpenAI-compatible</option>
                <option value="anthropic">Anthropic Messages</option>
              </select>
            </div>
            <div className="field">
              <label>API key</label>
              <input
                type="password"
                value={apiKey}
                onChange={(event) => setApiKey(event.target.value)}
                placeholder="optional for local endpoints"
              />
            </div>
          </>
        )}

        <div className="row" style={{ gap: 8 }}>
          {!custom && preset && <LobeIcon name={preset.icon} size={16} />}
          <span className="field__hint">
            {custom
              ? 'Any OpenAI-compatible or Anthropic endpoint joins the next round.'
              : preset?.has_api_key
                ? 'This seat joins the next round and can be addressed immediately.'
                : 'This provider has no saved API key; the seat will fail when it speaks.'}
          </span>
        </div>
      </div>
    </Modal>
  )
}