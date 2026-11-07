import { Activity, MonitorPlay, PanelLeft, PanelRight, PanelRightOpen } from 'lucide-react'
import type { UsagePayload } from '../lib/api'

type Props = {
  title: string
  collapsed: boolean
  onToggleSidebar: () => void
  usage: UsagePayload
  monitorOpen: boolean
  onToggleMonitor: () => void
  connection: 'open' | 'closed' | 'error'
  running: boolean
  previewOpen?: boolean
  onTogglePreview?: () => void
}

export function TopBar({
  title,
  collapsed,
  onToggleSidebar,
  usage,
  monitorOpen,
  onToggleMonitor,
  connection,
  running,
  previewOpen,
  onTogglePreview,
}: Props) {
  const ratio = Math.max(0, Math.min(1, usage.ratio ?? 0))
  const level = usage.level ?? (ratio >= 0.85 ? 'red' : ratio >= 0.6 ? 'yellow' : 'green')
  const used = usage.total_tokens ?? 0
  const window = usage.context_window ?? 128000

  return (
    <header className="onion-topbar">
      <button
        className="onion-iconbtn"
        onClick={onToggleSidebar}
        title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
      >
        <PanelLeft size={19} />
      </button>

      <div className="onion-topbar__title">
        <span className="onion-status-dot" data-state={running ? 'running' : connection} />
        <h1>{title || 'Onion Harness'}</h1>
      </div>

      <div className="onion-context" title={`${used.toLocaleString()} of ~${window.toLocaleString()} tokens`}>
        <span className="onion-context__label">Context</span>
        <span className="onion-context__track">
          <span
            className="onion-context__fill"
            data-level={level}
            style={{ width: `${Math.max(2, ratio * 100)}%` }}
          />
        </span>
        <span className="onion-context__value">{Math.round(ratio * 100)}%</span>
      </div>

      <button
        className="onion-iconbtn"
        data-active={previewOpen}
        onClick={onTogglePreview}
        title={previewOpen ? 'Hide preview' : 'Show preview'}
        aria-label={previewOpen ? 'Hide preview' : 'Show preview'}
      >
        <MonitorPlay size={19} />
      </button>

      <button
        className="onion-iconbtn"
        onClick={onToggleMonitor}
        title={monitorOpen ? 'Hide monitor' : 'Show monitor'}
        aria-label={monitorOpen ? 'Hide monitor' : 'Show monitor'}
      >
        {monitorOpen ? <PanelRightOpen size={19} /> : <PanelRight size={19} />}
      </button>
      {running && (
        <span className="badge" data-status="running" style={{ marginRight: 4 }}>
          <Activity size={10} />
          live
        </span>
      )}
    </header>
  )
}