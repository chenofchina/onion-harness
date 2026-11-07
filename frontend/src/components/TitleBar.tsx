import { useEffect, useState } from 'react'
import { Maximize, Minimize, Minus, Square, SquaresSubtract, X } from 'lucide-react'
import { getHost } from '../lib/host'
import { OnionGlyph } from './OnionMark'

/**
 * Android-style custom title bar for the frameless window.
 *
 * Dragging, resizing and double-click-maximize are handled natively by the
 * host's window-procedure subclass (WM_NCHITTEST), so this strip does not move
 * the window via JavaScript: that path had visible lag and could not resize.
 * The strip is tagged with the host's drag-region class as a fallback for
 * platforms without the native subclass, and the controls opt out of both.
 */

export function TitleBar() {
  const [maximized, setMaximized] = useState(false)
  const [fullscreen, setFullscreen] = useState(false)
  const [hasBridge, setHasBridge] = useState(false)

  useEffect(() => {
    const host = getHost()
    setHasBridge(Boolean(host))
    if (!host?.is_maximized) return
    const sync = () => {
      Promise.resolve(host.is_maximized!())
        .then((value) => setMaximized(Boolean(value)))
        .catch(() => undefined)
      if (host.is_fullscreen) {
        Promise.resolve(host.is_fullscreen())
          .then((value) => setFullscreen(Boolean(value)))
          .catch(() => undefined)
      }
    }
    sync()
    // The native window can maximize without going through our button
    // (double-click, Win+Up, Aero snap), so poll lightly to keep the icon right.
    const timer = window.setInterval(sync, 800)
    return () => window.clearInterval(timer)
  }, [])

  const maximize = () => {
    const host = getHost()
    if (!host?.toggle_maximize) return
    try {
      Promise.resolve(host.toggle_maximize())
        .then((value) => setMaximized(Boolean(value)))
        .catch(() => undefined)
    } catch {
      /* unavailable in the browser build */
    }
  }

  // F11 toggles full screen, matching the desktop convention.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'F11') return
      event.preventDefault()
      const host = getHost()
      if (!host?.toggle_fullscreen) return
      Promise.resolve(host.toggle_fullscreen())
        .then((value) => setFullscreen(Boolean(value)))
        .catch(() => undefined)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const toggleFullscreen = () => {
    const host = getHost()
    if (!host?.toggle_fullscreen) return
    try {
      Promise.resolve(host.toggle_fullscreen())
        .then((value) => setFullscreen(Boolean(value)))
        .catch(() => undefined)
    } catch {
      /* unavailable in the browser build */
    }
  }

  const windowAction = (action: 'minimize' | 'close') => {
    const host = getHost()
    const fn = host?.[action]
    if (!fn) return
    try {
      Promise.resolve(fn()).catch(() => undefined)
    } catch {
      /* unavailable in the browser build */
    }
  }

  return (
    <div className="onion-titlebar" data-bridge={hasBridge}>
      <div className="onion-titlebar__brand">
        <OnionGlyph size={18} />
        <span>Onion Harness</span>
      </div>
      <div
        className="onion-titlebar__drag"
        onDoubleClick={maximize}
        role="presentation"
        title="Drag to move · double-click to maximize"
      />
      <div className="onion-titlebar__controls" data-no-drag="true">
        <button
          className="titlebar-btn"
          onClick={() => windowAction('minimize')}
          title="Minimize"
          aria-label="Minimize window"
        >
          <Minus size={14} />
        </button>
        <button
          className="titlebar-btn"
          onClick={maximize}
          title={maximized ? 'Restore' : 'Maximize'}
          aria-label={maximized ? 'Restore window' : 'Maximize window'}
        >
          {maximized ? <SquaresSubtract size={13} /> : <Square size={12} />}
        </button>
        <button
          className="titlebar-btn"
          onClick={toggleFullscreen}
          title={fullscreen ? 'Exit full screen (F11)' : 'Full screen (F11)'}
          aria-label={fullscreen ? 'Exit full screen' : 'Enter full screen'}
        >
          {fullscreen ? <Minimize size={13} /> : <Maximize size={12} />}
        </button>
        <button
          className="titlebar-btn titlebar-btn--close"
          onClick={() => windowAction('close')}
          title="Close"
          aria-label="Close window"
        >
          <X size={15} />
        </button>
      </div>
    </div>
  )
}