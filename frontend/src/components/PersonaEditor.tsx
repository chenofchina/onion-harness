import { useState } from 'react'
import { Check, Trash2, X } from 'lucide-react'
import { api, type PersonaOption } from '../lib/api'
import { Modal } from './Dialogs'

/**
 * Create or edit a custom persona.
 *
 * The prompt here is injected at the same level as the system prompt - it is not a
 * footnote or a style hint, it sits beside the core instructions and travels through
 * the same system channel. What it cannot do is relax a safety rule: the mode
 * contract is composed after this overlay, so Plan still refuses writes and Standard
 * still asks for approval no matter what is written here. That is stated in the UI
 * because a user writing "ignore all restrictions" should understand what will
 * happen rather than discover it later.
 *
 * The id is the identity of the persona: it decides the file name on disk, and it
 * cannot be changed after creation. Everything else is editable, so an existing
 * persona can be iterated on without creating duplicates.
 */
export function PersonaEditor({
  open,
  initial,
  onClose,
  onSaved,
}: {
  open: boolean
  /** An existing custom persona to edit, or null to create a new one. */
  initial: PersonaOption | null
  onClose: () => void
  onSaved: (personas: PersonaOption[], savedId: string) => void
}) {
  const editing = Boolean(initial)
  const [id, setId] = useState(initial?.id || '')
  const [label, setLabel] = useState(initial?.label || '')
  const [description, setDescription] = useState(initial?.description || '')
  const [overlay, setOverlay] = useState(initial?.overlay || '')
  const [temperature, setTemperature] = useState(initial?.sampling?.temperature ?? 0.7)
  const [topP, setTopP] = useState(initial?.sampling?.top_p ?? 1.0)
  const [activate, setActivate] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)

  if (!open) return null

  const save = async () => {
    setBusy(true)
    setError('')
    try {
      const result = await api.savePersona({
        id: id.trim(),
        label: label.trim(),
        overlay,
        description: description.trim(),
        temperature: Number(temperature),
        top_p: Number(topP),
        activate: editing ? false : activate,
      })
      onSaved(result.personas, result.persona.id)
      onClose()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    setBusy(true)
    setError('')
    try {
      const result = await api.deletePersona(initial!.id)
      onSaved(result.personas, '')
      onClose()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
      setConfirmDelete(false)
    }
  }

  return (
    <Modal title={editing ? `Edit ${initial?.label}` : 'New persona'} onClose={onClose} wide>
      <div className="modal__body">
        <div className="row" style={{ gap: 10, alignItems: 'flex-start' }}>
          <div className="field" style={{ flex: 1 }}>
            <label>Name</label>
            <input
              type="text"
              value={label}
              onChange={(event) => {
                setLabel(event.target.value)
                // Only derive the id while creating: it is a stable identity, so
                // renaming later must not orphan the file on disk.
                if (!editing && (!id || id === slug(label))) setId(slug(event.target.value))
              }}
              placeholder="Terse"
            />
          </div>
          <div className="field" style={{ width: 170 }}>
            <label>Id</label>
            <input
              type="text"
              value={id}
              onChange={(event) => setId(event.target.value)}
              placeholder="terse"
              disabled={editing}
              readOnly={editing}
              title={editing ? 'The id is fixed; it names the file on disk' : undefined}
            />
          </div>
        </div>

        <div className="field">
          <label>Short description</label>
          <input
            type="text"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="Few words, no preamble."
          />
          <span className="field__hint">Shown in the persona picker.</span>
        </div>

        <div className="field">
          <label>Persona prompt</label>
          <textarea
            value={overlay}
            onChange={(event) => setOverlay(event.target.value)}
            rows={7}
            placeholder="Describe how this persona should behave, in plain language."
            style={{ resize: 'vertical', fontFamily: 'inherit' }}
          />
          <span className="field__hint">
            Injected into the system channel at the same level as the system prompt, so
            it survives context compression. It cannot relax Plan or Standard: the mode
            contract is applied after this text, so writes are still refused in Plan and
            still require approval in Standard.
          </span>
        </div>

        <div className="row" style={{ gap: 16 }}>
          <div className="field" style={{ flex: 1 }}>
            <label>Temperature · {Number(temperature).toFixed(2)}</label>
            <input
              type="range"
              min={0}
              max={2}
              step={0.05}
              value={temperature}
              onChange={(event) => setTemperature(Number(event.target.value))}
            />
            <span className="field__hint">Lower is more predictable, higher more varied.</span>
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label>Top P · {Number(topP).toFixed(2)}</label>
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={topP}
              onChange={(event) => setTopP(Number(event.target.value))}
            />
            <span className="field__hint">Nucleus sampling cutoff.</span>
          </div>
        </div>

        {!editing && (
          <label className="row" style={{ gap: 8, alignItems: 'center', fontSize: '0.82rem' }}>
            <input
              type="checkbox"
              checked={activate}
              onChange={(event) => setActivate(event.target.checked)}
            />
            Use this persona as soon as it is saved
          </label>
        )}

        {error && <div className="card-change__warn">{error}</div>}

        {confirmDelete && (
          <div className="card-change__warn">
            <span>Delete “{initial?.label}”? This cannot be undone.</span>
            <span style={{ flex: 1 }} />
            <button className="btn" data-variant="text" onClick={() => setConfirmDelete(false)}>
              Cancel
            </button>
            <button className="btn" data-variant="filled" onClick={remove} disabled={busy}>
              Delete
            </button>
          </div>
        )}
      </div>

      <div className="modal__footer">
        {editing && (
          <button
            className="btn"
            data-variant="text"
            onClick={() => setConfirmDelete(true)}
            disabled={busy}
            style={{ marginRight: 'auto' }}
          >
            <Trash2 size={14} /> Delete
          </button>
        )}
        <button className="btn" data-variant="text" onClick={onClose} disabled={busy}>
          <X size={14} /> Cancel
        </button>
        <button
          className="btn"
          data-variant="filled"
          onClick={save}
          disabled={busy || !label.trim() || !overlay.trim() || !id.trim()}
        >
          <Check size={14} /> Save
        </button>
      </div>
    </Modal>
  )
}

function slug(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40)
}