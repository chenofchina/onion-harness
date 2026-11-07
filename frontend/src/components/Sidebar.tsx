import { ClockArrowUp, History, Plus, Puzzle, Settings2, SquarePen, Users } from 'lucide-react'
import type { ProviderPreset, RoundtableSeatPublic, TaskRecord } from '../lib/api'
import { DebaterList } from './DebaterList'
import { LobeIcon } from './LobeIcon'
import { OnionGlyph } from './OnionMark'

type Props = {
  collapsed: boolean
  tasks: TaskRecord[]
  activeTaskId: string | null
  provider?: ProviderPreset
  model: string
  thinkingLabel: string
  onChangeCount: number
  mcpCount: number
  onNewTask: () => void
  onNewRoundtable: () => void
  onSelectTask: (id: string) => void
  onOpenSettings: () => void
  onOpenHistory: () => void
  onOpenMcp: () => void
  onPickModel: () => void
  roundtableSeats?: RoundtableSeatPublic[]
  roundtableHostId?: string
  roundtableActiveSeat?: string | null
  roundtableCounts?: Record<string, number>
  onSelectSeat?: (seatId: string) => void
  onAddSeat?: () => void
}

export function Sidebar({
  collapsed,
  tasks,
  activeTaskId,
  provider,
  model,
  thinkingLabel,
  onChangeCount,
  mcpCount,
  onNewTask,
  onNewRoundtable,
  onSelectTask,
  onOpenSettings,
  onOpenHistory,
  onOpenMcp,
  onPickModel,
  roundtableSeats = [],
  roundtableHostId = '',
  roundtableActiveSeat = null,
  roundtableCounts = {},
  onSelectSeat,
  onAddSeat,
}: Props) {
  return (
    <aside className="onion-sidebar" data-collapsed={collapsed} aria-label="Task navigation">
      <div className="onion-sidebar__brand">
        <OnionGlyph size={30} />
        {!collapsed && (
          <div className="onion-sidebar__brand-text">
            <strong>Onion Harness</strong>
            <span>Agent runtime</span>
          </div>
        )}
      </div>

      <button className="onion-newtask" onClick={onNewTask} title="New task">
        <SquarePen size={18} />
        {!collapsed && <span>New task</span>}
      </button>

      <button className="onion-newtask onion-newtask--alt" onClick={onNewRoundtable} title="Roundtable">
        <Users size={18} />
        {!collapsed && <span>Roundtable</span>}
      </button>

      <div className="onion-sidebar__section">
        <span>Recent</span>
        {!collapsed && <History size={13} />}
      </div>

      <div className="onion-tasklist">
        {tasks.length === 0 && !collapsed && (
          <div className="monitor-empty" style={{ padding: '8px 12px' }}>
            No tasks yet. Start one above.
          </div>
        )}
        {tasks.map((task) => (
          <button
            key={task.id}
            className="onion-taskitem"
            data-active={task.id === activeTaskId}
            onClick={() => onSelectTask(task.id)}
            title={task.title || task.prompt}
          >
            <StatusDot status={task.status} />
            <span className="onion-taskitem__title">{task.title || task.prompt}</span>
          </button>
        ))}
      </div>

      {roundtableSeats.length > 0 && !collapsed && onSelectSeat && onAddSeat && (
        <DebaterList
          seats={roundtableSeats}
          hostId={roundtableHostId}
          activeSeat={roundtableActiveSeat}
          countBySeat={roundtableCounts}
          onSelectSeat={onSelectSeat}
          onAddSeat={onAddSeat}
          canAdd={roundtableSeats.length < 6}
        />
      )}

      <div className="onion-sidebar__footer">
        <button className="onion-footer" onClick={onOpenMcp} title="Create MCP">
          <Puzzle size={17} />
          {!collapsed && (
            <span className="onion-footer__label">
              Create MCP
              {mcpCount > 0 && <span className="impact-chip" style={{ marginLeft: 6 }}>{mcpCount}</span>}
            </span>
          )}
        </button>
        <button className="onion-model-chip" onClick={onPickModel} title="Change model">
          {provider ? <LobeIcon name={provider.icon} size={20} /> : <Plus size={18} />}
          {!collapsed && (
            <span className="onion-model-chip__meta">
              <strong>{model || 'Select model'}</strong>
              <span>
                {provider?.label || 'No provider'} · {thinkingLabel}
              </span>
            </span>
          )}
        </button>
        <button className="onion-footer" onClick={onOpenHistory} title="Change history">
          <ClockArrowUp size={17} />
          {!collapsed && (
            <span className="onion-footer__label">
              Change history
              {onChangeCount > 0 && <span className="impact-chip" style={{ marginLeft: 6 }}>{onChangeCount}</span>}
            </span>
          )}
        </button>
        <button className="onion-footer" onClick={onOpenSettings} title="Settings">
          <Settings2 size={17} />
          {!collapsed && <span className="onion-footer__label">Settings</span>}
        </button>
      </div>
    </aside>
  )
}

export function StatusDot({ status }: { status: string }) {
  const state =
    status === 'running' || status === 'queued' ? 'running' : status === 'failed' ? 'error' : undefined
  return (
    <span
      className="onion-status-dot"
      data-state={state}
      style={
        !state
          ? { background: status === 'completed' ? 'var(--onion-level-green)' : 'var(--md-sys-color-outline)' }
          : undefined
      }
    />
  )
}