import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ArrowUp,
  ChevronDown,
  FolderOpen,
  Maximize2,
  Minimize2,
  Paperclip,
  Plus,
  Settings2,
  ShieldCheck,
  Square,
  X,
} from 'lucide-react'
import type {
  ElevationLevel,
  ProviderPreset,
  ThinkingSlider as ThinkingSliderSpec,
} from '../lib/api'
import { pickNativeDirectory, pickNativeFiles } from '../lib/host'
import { LobeIcon } from './LobeIcon'
import { ComposerPopover, MenuItem, ThinkingSlider } from './PopoverMenu'

type Props = {
  value: string
  onChange: (value: string) => void
  onSend: () => void
  onStop: () => void
  running: boolean
  workspace: string
  onWorkspaceChange: (path: string) => void
  provider?: ProviderPreset
  model: string
  onPickModel: () => void
  thinking: string
  thinkingSlider: ThinkingSliderSpec
  onThinkingChange: (id: string) => void
  elevation: string
  elevationLevels: ElevationLevel[]
  onElevationChange: (id: string) => void
  mode: string
  modes: { id: string; label: string; description: string }[]
  attachments: string[]
  onAttachmentsChange: (paths: string[]) => void
  onOpenAdvanced: () => void
  disabledReason?: string
}

const MIN_HEIGHT = 96
const DEFAULT_HEIGHT = 150

export function Composer({
  value,
  onChange,
  onSend,
  onStop,
  running,
  workspace,
  onWorkspaceChange,
  provider,
  model,
  onPickModel,
  thinking,
  thinkingSlider,
  onThinkingChange,
  elevation,
  elevationLevels,
  onElevationChange,
  attachments,
  onAttachmentsChange,
  onOpenAdvanced,
  disabledReason,
}: Props) {
  const ref = useRef<HTMLTextAreaElement>(null)
  const shellRef = useRef<HTMLDivElement>(null)
  const modelBtn = useRef<HTMLButtonElement>(null)
  const thinkingBtn = useRef<HTMLButtonElement>(null)
  const elevationBtn = useRef<HTMLButtonElement>(null)

  const [menu, setMenu] = useState<'model' | 'thinking' | 'elevation' | null>(null)
  const [height, setHeight] = useState(DEFAULT_HEIGHT)
  const [expanded, setExpanded] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const dragging = useRef(false)

  const canSend = value.trim().length > 0 && !running

  /* ---------------------------------------------------------- resizing */
  const clampHeight = useCallback((next: number) => {
    // Measure the stage, not the window: the mode bar and whatever sits above
    // the composer already occupy part of it. The stage height is stable while
    // dragging, so this cannot feed back into itself.
    const stage = shellRef.current?.closest('.onion-stage')
    const stageHeight = stage?.getBoundingClientRect().height ?? window.innerHeight
    const reserved = 96 // mode bar + wrap padding + a sliver of transcript
    const max = Math.max(MIN_HEIGHT + 40, stageHeight - reserved)
    return Math.round(Math.max(MIN_HEIGHT, Math.min(next, max)))
  }, [])

  useEffect(() => {
    const onMove = (event: MouseEvent) => {
      if (!dragging.current) return
      const bottom = shellRef.current?.getBoundingClientRect().bottom ?? window.innerHeight
      setHeight(clampHeight(bottom - event.clientY))
    }
    const onUp = () => {
      if (!dragging.current) return
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
  }, [clampHeight])

  const toggleExpand = () => {
    setExpanded((current) => {
      const next = !current
      setHeight(next ? clampHeight(window.innerHeight * 0.5) : DEFAULT_HEIGHT)
      return next
    })
  }

  /* ------------------------------------------------- native file pickers */
  const addFiles = async () => {
    const files = await pickNativeFiles()
    if (!files) {
      setNotice('Native file picker is only available in the desktop app.')
      window.setTimeout(() => setNotice(null), 3200)
      return
    }
    const merged = [...attachments]
    for (const path of files) if (!merged.includes(path)) merged.push(path)
    onAttachmentsChange(merged)
    setNotice(null)
  }

  const chooseWorkspace = async () => {
    const picked = await pickNativeDirectory()
    if (!picked) {
      setNotice('Native folder picker is only available in the desktop app.')
      window.setTimeout(() => setNotice(null), 3200)
      return
    }
    onWorkspaceChange(picked)
    setNotice(null)
  }

  const removeAttachment = (path: string) => {
    onAttachmentsChange(attachments.filter((item) => item !== path))
  }

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      if (canSend) onSend()
    }
  }

  const thinkingIndex = Math.max(0, thinkingSlider.stops.indexOf(thinking))
  const thinkingLabel = thinkingSlider.labels[thinkingIndex] ?? 'Off'
  const elevationLabel =
    elevationLevels.find((level) => level.id === elevation)?.label ?? 'Standard'

  return (
    <div className="onion-composer-wrap">
      <div className="onion-composer-shell" ref={shellRef} style={{ height }}>
        <div
          className="onion-composer__grip"
          onMouseDown={() => {
            dragging.current = true
            document.body.style.cursor = 'row-resize'
            document.body.style.userSelect = 'none'
          }}
          role="separator"
          aria-label="Resize input area"
          title="Drag to resize"
        >
          <span className="onion-composer__grip-bar" />
        </div>

        <div className="onion-composer onion-composer--tall">
          {attachments.length > 0 && (
            <div className="composer-attachments">
              {attachments.map((path) => (
                <span key={path} className="composer-attachment" title={path}>
                  <Paperclip size={11} />
                  <span>{baseName(path)}</span>
                  <button onClick={() => removeAttachment(path)} title="Remove" aria-label="Remove attachment">
                    <X size={11} />
                  </button>
                </span>
              ))}
            </div>
          )}

          <div className="onion-composer__row">
            <button
              className="onion-iconbtn"
              title="Add files as context"
              aria-label="Add files as context"
              onClick={addFiles}
            >
              <Plus size={19} />
            </button>
            <textarea
              ref={ref}
              value={value}
              placeholder="Describe a task for Onion…  (Enter to send, Shift+Enter for a new line)"
              rows={1}
              onChange={(e) => onChange(e.target.value)}
              onKeyDown={handleKeyDown}
              spellCheck={false}
            />
            <div className="onion-composer__send">
              <button
                className="onion-iconbtn"
                onClick={toggleExpand}
                title={expanded ? 'Collapse input' : 'Expand input'}
                aria-label={expanded ? 'Collapse input' : 'Expand input'}
              >
                {expanded ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
              </button>
              {running ? (
                <button
                  className="send-btn"
                  data-stop="true"
                  onClick={onStop}
                  title="Stop task"
                  aria-label="Stop task"
                >
                  <Square size={15} fill="currentColor" />
                </button>
              ) : (
                <button
                  className="send-btn"
                  onClick={onSend}
                  disabled={!canSend}
                  title="Send"
                  aria-label="Send task"
                >
                  <ArrowUp size={19} />
                </button>
              )}
            </div>
          </div>

          <div className="onion-composer__tools">
            <button
              className="pill-btn"
              data-active={Boolean(workspace)}
              onClick={chooseWorkspace}
              title={workspace || 'Choose a working directory (native dialog)'}
            >
              <FolderOpen size={13} />
              <span>{workspace ? shortPath(workspace) : 'Working directory'}</span>
            </button>

            <button
              ref={modelBtn}
              className="pill-btn"
              data-active={menu === 'model'}
              onClick={() => setMenu((current) => (current === 'model' ? null : 'model'))}
              title="Change model"
            >
              {provider ? <LobeIcon name={provider.icon} size={14} /> : null}
              <span>{model || 'Select model'}</span>
              <ChevronDown size={12} />
            </button>

            <button
              ref={thinkingBtn}
              className="pill-btn"
              data-active={menu === 'thinking'}
              onClick={() => setMenu((current) => (current === 'thinking' ? null : 'thinking'))}
              title="Thinking depth"
            >
              <span>Thinking: {thinkingLabel}</span>
              <ChevronDown size={12} />
            </button>

            <button
              ref={elevationBtn}
              className="pill-btn"
              data-active={menu === 'elevation' || elevation !== 'standard'}
              onClick={() => setMenu((current) => (current === 'elevation' ? null : 'elevation'))}
              title="Elevation policy"
            >
              <ShieldCheck size={13} />
              <span>{elevationLabel}</span>
              <ChevronDown size={12} />
            </button>

            <span className="spacer" style={{ flex: 1 }} />
            <button
              className="pill-btn"
              onClick={onOpenAdvanced}
              title="Advanced settings (API key, base URL)"
            >
              <Settings2 size={13} />
              <span>Advanced</span>
            </button>
          </div>

          <div className="onion-composer__hint-row">
            {notice ? (
              <span className="onion-composer__hint">{notice}</span>
            ) : disabledReason ? (
              <span className="onion-composer__hint">{disabledReason}</span>
            ) : (
              <span className="onion-composer__hint">
                Drag the top edge to resize · Enter to send · Shift+Enter for a new line
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Compact menus open above the composer; they never cover the timeline. */}
      <ComposerPopover
        open={menu === 'model'}
        anchorRef={modelBtn}
        onClose={() => setMenu(null)}
        minWidth={260}
      >
        <div className="popover-section">
          <span className="popover-section__label">Model</span>
          <div className="popover-model-row">
            {provider && (
              <span className="popover-model-row__current">
                <LobeIcon name={provider.icon} size={16} />
                <span>
                  <strong>{model || 'No model'}</strong>
                  <small>{provider.label}</small>
                </span>
              </span>
            )}
            <button
              className="btn"
              data-variant="tonal"
              onClick={() => {
                setMenu(null)
                onPickModel()
              }}
            >
              Change
            </button>
          </div>
        </div>
      </ComposerPopover>

      <ComposerPopover
        open={menu === 'thinking'}
        anchorRef={thinkingBtn}
        onClose={() => setMenu(null)}
        minWidth={280}
      >
        <div className="popover-section">
          <ThinkingSlider
            stops={thinkingSlider.stops}
            labels={thinkingSlider.labels}
            value={thinking}
            onChange={onThinkingChange}
          />
        </div>
      </ComposerPopover>

      <ComposerPopover
        open={menu === 'elevation'}
        anchorRef={elevationBtn}
        onClose={() => setMenu(null)}
        minWidth={280}
      >
        <div className="popover-section">
          <span className="popover-section__label">Elevation</span>
          {elevationLevels.map((level) => (
            <MenuItem
              key={level.id}
              label={level.label}
              hint={level.description}
              selected={level.id === elevation}
              onClick={() => {
                onElevationChange(level.id)
                setMenu(null)
              }}
            />
          ))}
        </div>
      </ComposerPopover>
    </div>
  )
}

function shortPath(path: string) {
  if (path.length <= 32) return path
  const parts = path.replace(/\\/g, '/').split('/')
  return `…/${parts.slice(-2).join('/')}`
}

function baseName(path: string) {
  const parts = path.replace(/\\/g, '/').split('/')
  return parts[parts.length - 1] || path
}