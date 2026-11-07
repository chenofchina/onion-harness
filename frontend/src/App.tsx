import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { MonitorPlay, Sparkles } from 'lucide-react'
import {
  api,
  ApiError,
  openEventStream,
  type Bootstrap,
  type ChangeRecord,
  type McpToolRecord,
  type MemoryRecord,
  type OnionEvent,
  type ProviderPreset,
  type RoundtableRosterEntry,
  type Revision,
  type Settings,
  type TaskRecord,
  type UsagePayload,
} from './lib/api'
import { ThemeProvider, useTheme } from './lib/theme'
import { TitleBar } from './components/TitleBar'
import { Composer } from './components/Composer'
import { Sidebar } from './components/Sidebar'
import { TopBar } from './components/TopBar'
import { MonitorPanel } from './components/MonitorPanel'
import { OnionMark } from './components/OnionMark'
import { renderEvent } from './components/EventCards'
import { HistoryDialog, ModelPicker, SettingsDialog, WorkspaceDialog } from './components/Dialogs'
import { AdvancedDrawer } from './components/AdvancedDrawer'
import { SidecarPreview, type PreviewMode } from './components/SidecarPreview'
import { Segmented } from './components/PopoverMenu'
import { TaskCardHeader, TaskEditor } from './components/TaskActions'
import { ArchiveDialog, McpDialog } from './components/McpDialog'
import { MemoryDialog } from './components/MemoryDialog'
import { RoundtableDialog } from './components/RoundtableDialog'
import { RoundtableView } from './components/RoundtableView'
import { MinutesPanel } from './components/MinutesPanel'
import { SeatInspector } from './components/SeatInspector'
import { DebaterList } from './components/DebaterList'
import { RoundtableHall } from './components/RoundtableHall'
import { AddSeatDialog } from './components/AddSeatDialog'
import { EMPTY_ROUNDTABLE, reduceRoundtable } from './lib/roundtable'

type Toast = { id: number; text: string; kind?: 'error' }

const EMPTY_USAGE: UsagePayload = {
  prompt_tokens: 0,
  completion_tokens: 0,
  total_tokens: 0,
  context_window: 128000,
  ratio: 0,
  level: 'green',
}

export default function App() {
  return (
    <ThemeProvider>
      <Shell />
    </ThemeProvider>
  )
}

function Shell() {
  const { mode, setMode } = useTheme()
  const [boot, setBoot] = useState<Bootstrap | null>(null)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [tasks, setTasks] = useState<TaskRecord[]>([])
  const [archived, setArchived] = useState<TaskRecord[]>([])
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null)
  const [events, setEvents] = useState<OnionEvent[]>([])
  const [usage, setUsage] = useState<UsagePayload>(EMPTY_USAGE)
  const [prompt, setPrompt] = useState('')
  const [workspace, setWorkspace] = useState('')
  const [attachments, setAttachments] = useState<string[]>([])
  const [contextWindow, setContextWindow] = useState(128000)
  const [keepRecent, setKeepRecent] = useState(10)
  const [roundtableRoster, setRoundtableRoster] = useState<RoundtableRosterEntry[]>([])

  // Provider defaults the user saved in the Model dialog. Every fallback reads
  // these instead of a hardcoded 128k, so a 1M setting actually sticks.
  const savedContextWindow = Number(settings?.context_window || 128000)
  const savedKeepRecent = Number(settings?.keep_recent_messages || 10)
  const [roundtableError, setRoundtableError] = useState<string | null>(null)
  const [selectedSeat, setSelectedSeat] = useState<string | null>(null)
  const [collapsed, setCollapsed] = useState(false)
  const [monitorOpen, setMonitorOpen] = useState(false)
  const [connection, setConnection] = useState<'open' | 'closed' | 'error'>('closed')
  const [runningTaskId, setRunningTaskId] = useState<string | null>(null)
  const [proposals, setProposals] = useState<ChangeRecord[]>([])
  const [revisions, setRevisions] = useState<Revision[]>([])
  const [busyChangeId, setBusyChangeId] = useState<string | null>(null)
  const [toasts, setToasts] = useState<Toast[]>([])
  const [dialog, setDialog] = useState<
    'model' | 'settings' | 'history' | 'workspace' | 'archive' | 'mcp' | 'memory' | 'roundtable' | null
  >(null)
  const [memories, setMemories] = useState<MemoryRecord[]>([])
  const [memoryBusy, setMemoryBusy] = useState(false)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [fatal, setFatal] = useState<string | null>(null)
  const [highlightedEventId, setHighlightedEventId] = useState<string | null>(null)
  const [editingTaskId, setEditingTaskId] = useState<string | null>(null)
  const [editBusy, setEditBusy] = useState(false)

  // Sidecar preview
  const [previewOpen, setPreviewOpen] = useState(false)
  const [previewMode, setPreviewMode] = useState<PreviewMode>('web')
  const [previewUrl, setPreviewUrl] = useState('')

  const streamRef = useRef<(() => void) | null>(null)
  const activeRef = useRef<string | null>(null)
  const eventKeys = useRef<Set<string>>(new Set())
  const endRef = useRef<HTMLDivElement>(null)

  const toast = useCallback((text: string, kind?: 'error') => {
    const id = Date.now() + Math.random()
    setToasts((prev) => [...prev, { id, text, kind }])
    setTimeout(() => setToasts((prev) => prev.filter((item) => item.id !== id)), 4200)
  }, [])

  useEffect(() => {
    activeRef.current = activeTaskId
  }, [activeTaskId])

  /* ------------------------------------------------------------- load */
  useEffect(() => {
    let cancelled = false
    api
      .bootstrap()
      .then((data) => {
        if (cancelled) return
        setBoot(data)
        setSettings(data.settings)
        setTasks(data.tasks)
        setArchived(data.archived || [])
        setProposals(data.proposals)
        setRevisions(data.history)
        setWorkspace(data.settings.workspace || '')
        if (data.settings.context_window) {
          setContextWindow(data.settings.context_window)
          // Before any usage event arrives the ring shows 0 / window, so the
          // window must already be the configured one rather than 128k.
          setUsage((prev) => ({
            ...prev,
            context_window: data.settings.context_window,
          }))
        }
        if (data.settings.keep_recent_messages) {
          setKeepRecent(data.settings.keep_recent_messages)
        }
        if (data.settings.theme_mode && data.settings.theme_mode !== mode) {
          setMode(data.settings.theme_mode)
        }
      })
      .catch((error) => {
        if (!cancelled) setFatal(error instanceof Error ? error.message : String(error))
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  /* -------------------------------------------------------------- SSE */
  useEffect(() => {
    streamRef.current?.()
    streamRef.current = openEventStream(
      (incoming) => {
        if (eventKeys.current.has(incoming.id)) return
        eventKeys.current.add(incoming.id)

        // Raw terminal output is voluminous; it feeds the sidecar only and is
        // never appended to the transcript.
        if (incoming.type === 'tool-output') {
          if (incoming.task_id === activeRef.current) {
            setEvents((prev) =>
              prev.length > 4000 ? [...prev.slice(-3000), incoming] : [...prev, incoming],
            )
          }
          return
        }

        const relevantTask =
          incoming.task_id === activeRef.current || incoming.task_id === 'system'
        if (incoming.task_id === 'system') {
          handleSystemChange(incoming)
        }
        if (!relevantTask) return

        if (incoming.task_id === 'system') {
          if (incoming.payload.change?.task_id && incoming.payload.change.task_id === activeRef.current) {
            setEvents((prev) => [...prev, incoming])
          }
          return
        }

        setEvents((prev) => [...prev, incoming])
        applyUsage(incoming, setUsage)

        if (incoming.type === 'change-proposal' && incoming.payload.change) {
          setProposals((prev) => upsertProposal(prev, incoming.payload.change))
          setMonitorOpen(true)
        }
        if (incoming.type === 'change-applied' && incoming.payload.revision) {
          setProposals((prev) => prev.filter((p) => p.id !== incoming.payload.revision.change.id))
          setRevisions((prev) => upsertRevision(prev, incoming.payload.revision))
        }
        if (incoming.type === 'change-rejected') {
          setProposals((prev) => prev.filter((p) => p.id !== (incoming.payload.change?.id || '')))
        }
        if (incoming.type === 'change-reverted') {
          setRevisions((prev) =>
            prev.map((revision) =>
              (incoming.payload.versions || []).includes(revision.version)
                ? { ...revision, reverted_at: Date.now() / 1000 }
                : revision,
            ),
          )
        }
        if (incoming.type === 'tool-call' || incoming.type === 'tool-approval') {
          setMonitorOpen(true)
        }
        if (incoming.type === 'memory-learned') {
          // Pick the new notes up without a full bootstrap round trip.
          api
            .memory()
            .then((data) => setBoot((prev) => (prev ? { ...prev, memory: data.stats } : prev)))
            .catch(() => undefined)
        }
        if (incoming.type === 'task-created' || incoming.type === 'task-status') {
          if (incoming.payload.status !== 'completed' && incoming.payload.status !== 'failed') {
            setRunningTaskId(incoming.task_id)
          }
        }
        if (incoming.type === 'task-done') {
          setRunningTaskId((current) => (current === incoming.task_id ? null : current))
          setTasks((prev) =>
            prev.map((task) =>
              task.id === incoming.task_id
                ? { ...task, status: incoming.payload.status, updated_at: Date.now() / 1000 }
                : task,
            ),
          )
          if (incoming.payload.usage) {
            setUsage((prev) => ({ ...prev, ...incoming.payload.usage }))
          }
        }
      },
      (state) => setConnection(state),
    )
    return () => {
      streamRef.current?.()
      streamRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleSystemChange = useCallback((incoming: OnionEvent) => {
    if (incoming.type === 'change-proposal' && incoming.payload.change) {
      setProposals((prev) => upsertProposal(prev, incoming.payload.change))
    }
    if (incoming.type === 'change-applied' && incoming.payload.revision) {
      setProposals((prev) => prev.filter((p) => p.id !== incoming.payload.revision.change.id))
      setRevisions((prev) => upsertRevision(prev, incoming.payload.revision))
      refreshBoot()
    }
    if (incoming.type === 'change-rejected') {
      setProposals((prev) => prev.filter((p) => p.id !== (incoming.payload.change?.id || '')))
    }
    if (incoming.type === 'change-redone' && incoming.payload.revision) {
      setRevisions((prev) => upsertRevision(prev, incoming.payload.revision))
    }
    if (incoming.type === 'change-reverted') {
      setRevisions((prev) =>
        prev.map((revision) =>
          (incoming.payload.versions || []).includes(revision.version)
            ? { ...revision, reverted_at: Date.now() / 1000 }
            : revision,
        ),
      )
    }
  }, [])

  const memoryStats = boot?.memory

  const loadMemories = useCallback(() => {
    api
      .memory()
      .then((data) => {
        setMemories(data.memories)
        setBoot((prev) => (prev ? { ...prev, memory: data.stats } : prev))
      })
      .catch(() => undefined)
  }, [])

  const forgetMemory = useCallback(
    (id: string) => {
      setMemoryBusy(true)
      api
        .forgetMemory(id)
        .then(() => {
          setMemories((prev) => prev.filter((memory) => memory.id !== id))
          loadMemories()
        })
        .catch((exc) => toast(exc instanceof Error ? exc.message : String(exc), 'error'))
        .finally(() => setMemoryBusy(false))
    },
    [loadMemories, toast],
  )

  const purgeMemory = useCallback(() => {
    setMemoryBusy(true)
    api
      .purgeMemory()
      .then((result) => {
        setMemories([])
        loadMemories()
        toast(`Forgot ${result.purged} stored note${result.purged === 1 ? '' : 's'}`)
      })
      .catch((exc) => toast(exc instanceof Error ? exc.message : String(exc), 'error'))
      .finally(() => setMemoryBusy(false))
  }, [loadMemories, toast])

  const refreshBoot = useCallback(() => {
    api
      .bootstrap()
      .then((data) => {
        setBoot(data)
        setSettings(data.settings)
        setArchived(data.archived || [])
      })
      .catch(() => undefined)
  }, [])

  /* --------------------------------------------------------- scrolling */
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [events.length, runningTaskId])

  /* ---------------------------------------------------------- handlers */
  const selectTask = useCallback(
    async (taskId: string) => {
      setActiveTaskId(taskId)
      setDialog(null)
      setEditingTaskId(null)
      eventKeys.current = new Set()
      setUsage(EMPTY_USAGE)
      // Events that arrive while history is loading must survive the merge,
      // otherwise a live task silently drops the first few steps.
      const stamp = Date.now()
      try {
        const { events: loaded } = await api.taskEvents(taskId)
        loaded.forEach((event) => eventKeys.current.add(event.id))
        const task = [...tasks, ...archived].find((item) => item.id === taskId)
        // Tasks created before the server persisted user turns have no such
        // event on disk. Only then synthesise one, otherwise the prompt would
        // appear twice.
        const alreadyHasUserTurn = loaded.some((event) => event.type === 'user-input')
        const synthetic: OnionEvent[] =
          task && !alreadyHasUserTurn
            ? [
                {
                  id: `user-${task.id}`,
                  seq: 0,
                  type: 'user-input',
                  task_id: task.id,
                  source: 'gui',
                  timestamp: task.created_at,
                  payload: { text: task.prompt },
                },
              ]
            : []
        setEvents((prev) => {
          // Keep anything the stream delivered after this load started, but
          // drop events that belong to the task we just left.
          const live = prev.filter(
            (event) =>
              event.task_id === taskId &&
              event.timestamp * 1000 >= stamp &&
              !loaded.some((item) => item.id === event.id),
          )
          const merged = [...synthetic, ...loaded, ...live]
          merged.sort((a, b) => a.timestamp - b.timestamp || a.seq - b.seq)
          return merged
        })
        const lastUsage = [...loaded].reverse().find((event) => event.type === 'token-usage')
        if (lastUsage) setUsage((prev) => ({ ...prev, ...lastUsage.payload }))
        if (task) {
          // Tasks created before the context controls existed carry no value;
          // they inherit the user's saved default rather than 128k.
          const window = Number(task.context_window || savedContextWindow)
          setContextWindow(window)
          setKeepRecent(Number(task.keep_recent || savedKeepRecent))
          setUsage((prev) => ({ ...prev, context_window: window }))
        } else {
          setContextWindow(savedContextWindow)
          setKeepRecent(savedKeepRecent)
        }
        if (task && (task.status === 'running' || task.status === 'queued')) {
          setRunningTaskId(taskId)
        } else {
          setRunningTaskId(null)
        }
        if (loaded.some((event) => event.type === 'change-proposal')) setMonitorOpen(true)
        // A roundtable always opens its minutes board: the topology alone does
        // not tell you what was agreed or disputed.
        if (task && (task.kind === 'roundtable' || task.mode === 'roundtable')) {
          setMonitorOpen(true)
          // Load the roster so the debater list can add seats later.
          api
            .roundtableRoster()
            .then((data) => setRoundtableRoster(data.providers))
            .catch(() => undefined)
        }
      } catch (error) {
        toast(error instanceof Error ? error.message : String(error), 'error')
      }
    },
    [tasks, archived, toast],
  )

  const newTask = useCallback(() => {
    setActiveTaskId(null)
    setEvents([])
    setUsage(EMPTY_USAGE)
    setPrompt('')
    setRunningTaskId(null)
    setEditingTaskId(null)
    eventKeys.current = new Set()
    // A brand-new conversation inherits the provider defaults the user saved;
    // hardcoding 128k here silently ignored a 1M setting.
    setContextWindow(savedContextWindow)
    setKeepRecent(savedKeepRecent)
    setDialog(null)
  }, [savedContextWindow, savedKeepRecent])

  const activeTask = tasks.find((task) => task.id === activeTaskId)
  const currentMode = settings?.mode ?? boot?.settings.mode ?? 'standard'

  // Declared before send(): the send path decides whether to continue or start
  // a conversation, and must not reference it after first use.
  const activeTaskKind = tasks.find((task) => task.id === activeTaskId)?.kind
  const activeTaskMode = tasks.find((task) => task.id === activeTaskId)?.mode
  const activeIsRoundtable = activeTaskKind === 'roundtable' || activeTaskMode === 'roundtable'

  const send = useCallback(async () => {
    const text = prompt.trim()
    if (!text || !settings) return
    // Attached files travel as an explicit context block so the model knows
    // exactly which paths the user pointed at.
    const contextBlock = attachments.length
      ? `\n\nAttached files:\n${attachments.map((path) => `- ${path}`).join('\n')}`
      : ''
    setPrompt('')
    setAttachments([])
    try {
      const modelPayload = {
        provider_id: settings.provider_id,
        model: settings.model,
        thinking: settings.thinking,
        mode: settings.mode,
        elevation: settings.elevation,
        base_url: currentProvider(boot, settings)?.base_url,
        wire_format: currentProvider(boot, settings)?.format,
      }
      // With a conversation open, a new message continues that thread rather
      // than silently starting a fresh one. The backend replays prior turns.
      const continuing = Boolean(activeTaskId) && !activeIsRoundtable
      // Only send context values that differ from what the conversation (or the
      // saved default) already uses. Passing them unconditionally would
      // overwrite a per-task adjustment with whatever the composer happened to
      // hold.
      const reference = activeTask ?? null
      const refWindow = Number(reference?.context_window || savedContextWindow)
      const refKeep = Number(reference?.keep_recent || savedKeepRecent)
      const contextOverride: Record<string, number> = {}
      if (contextWindow !== refWindow) contextOverride.context_window = contextWindow
      if (keepRecent !== refKeep) contextOverride.keep_recent = keepRecent

      const record = continuing
        ? await api.continueTask(activeTaskId as string, {
            prompt: text + contextBlock,
            model: modelPayload,
            context: Object.keys(contextOverride).length ? contextOverride : undefined,
          })
        : await api.createTask({
            prompt: text + contextBlock,
            workspace: workspace || undefined,
            model: {
              ...modelPayload,
              ...(Object.keys(contextOverride).length
                ? contextOverride
                : // New conversations inherit the saved defaults server-side.
                  {}),
            },
          })

      setTasks((prev) => [record, ...prev.filter((task) => task.id !== record.id)])
      setActiveTaskId(record.id)
      setRunningTaskId(record.id)
      setUsage((prev) => ({
        ...prev,
        context_window: Number(record.context_window || contextWindow),
      }))
      // The server persists the user bubble and hands back the event. The SSE
      // stream may have delivered it *before* this response arrived, so only
      // append when it is genuinely absent — otherwise the same message
      // renders twice.
      const userEvent = record.user_event
      if (userEvent) eventKeys.current.add(userEvent.id)
      setEvents((prev) => {
        if (!userEvent) return continuing ? prev : []
        if (prev.some((event) => event.id === userEvent.id)) {
          return continuing ? prev : [userEvent]
        }
        return continuing ? [...prev, userEvent] : [userEvent]
      })
      if (!continuing) {
        eventKeys.current = new Set(userEvent ? [userEvent.id] : [])
      }
      setMonitorOpen(true)
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      toast(message, 'error')
      if (error instanceof ApiError && error.status === 401) setAdvancedOpen(true)
      setPrompt(text)
      setRunningTaskId(null)
    }
  }, [prompt, settings, workspace, boot, toast, attachments, activeTaskId, activeIsRoundtable, contextWindow, keepRecent, activeTask, savedContextWindow, savedKeepRecent])

  const stop = useCallback(async () => {
    if (!runningTaskId) return
    try {
      await api.cancelTask(runningTaskId)
      toast('Cancelling task…')
    } catch (error) {
      toast(error instanceof Error ? error.message : String(error), 'error')
    }
  }, [runningTaskId, toast])

  const applyChange = useCallback(
    async (changeId: string, opts: { acceptIrreversible: boolean; params: Record<string, any> }) => {
      setBusyChangeId(changeId)
      try {
        const revision = await api.applyChange(changeId, {
          accept_irreversible: opts.acceptIrreversible,
          params: opts.params,
        })
        setProposals((prev) => prev.filter((proposal) => proposal.id !== changeId))
        setRevisions((prev) => upsertRevision(prev, revision))
        toast(`Applied ${revision.change.label} as v${revision.version}`)
        refreshBoot()
      } catch (error) {
        toast(error instanceof Error ? error.message : String(error), 'error')
      } finally {
        setBusyChangeId(null)
      }
    },
    [toast, refreshBoot],
  )

  const rejectChange = useCallback(
    async (changeId: string) => {
      setBusyChangeId(changeId)
      try {
        await api.rejectChange(changeId)
        setProposals((prev) => prev.filter((proposal) => proposal.id !== changeId))
      } catch (error) {
        toast(error instanceof Error ? error.message : String(error), 'error')
      } finally {
        setBusyChangeId(null)
      }
    },
    [toast],
  )

  const revertVersion = useCallback(
    async (version: number) => {
      setBusyChangeId(`v${version}`)
      try {
        await api.revertVersion(version)
        const { history } = await api.history()
        setRevisions(history)
        toast(`Rolled back from v${version}`)
        refreshBoot()
      } catch (error) {
        toast(error instanceof Error ? error.message : String(error), 'error')
      } finally {
        setBusyChangeId(null)
      }
    },
    [toast, refreshBoot],
  )

  const redo = useCallback(async () => {
    setBusyChangeId('redo')
    try {
      const result = await api.redo()
      const { history } = await api.history()
      setRevisions(history)
      toast(result.redone.length ? `Redid ${result.redone.length} change(s)` : 'Nothing to redo')
      refreshBoot()
    } catch (error) {
      toast(error instanceof Error ? error.message : String(error), 'error')
    } finally {
      setBusyChangeId(null)
    }
  }, [toast, refreshBoot])

  const saveSettings = useCallback(
    (next: Settings) => {
      setSettings(next)
      if (next.theme_mode) setMode(next.theme_mode)
      if (next.workspace !== undefined) setWorkspace(next.workspace)
      refreshBoot()
    },
    [setMode, refreshBoot],
  )

  const updateSetting = useCallback(
    (patch: Partial<Settings>) => {
      setSettings((current) => (current ? { ...current, ...patch } : current))
      api
        .updateSettings(patch as Record<string, unknown>)
        .then((next) => setSettings(next))
        .catch((error) => toast(error instanceof Error ? error.message : String(error), 'error'))
    },
    [toast],
  )

  const resolveApproval = useCallback(
    async (approvalId: string, approved: boolean) => {
      if (!activeTaskId) return
      try {
        await api.resolveApproval(activeTaskId, approvalId, approved)
      } catch (error) {
        toast(error instanceof Error ? error.message : String(error), 'error')
      }
    },
    [activeTaskId, toast],
  )

  const editTask = useCallback(
    async (taskId: string, text: string) => {
      if (!settings) return
      setEditBusy(true)
      try {
        const result = await api.editTask(taskId, text, {
          provider_id: settings.provider_id,
          model: settings.model,
          thinking: settings.thinking,
          mode: settings.mode,
          elevation: settings.elevation,
        })
        setEditingTaskId(null)
        setRunningTaskId(taskId)
        setTaskPrompt(taskId, text)
        // The server returns the persisted edit event; render that exact one.
        const editEvent = (result as TaskRecord & { user_event?: OnionEvent }).user_event
        if (editEvent) {
          eventKeys.current.add(editEvent.id)
          setEvents((prev) => [...prev, editEvent])
        } else {
          setEvents((prev) => [
            ...prev,
            {
              id: `user-edit-${Date.now()}`,
              seq: 0,
              type: 'user-input',
              task_id: taskId,
              source: 'gui',
              timestamp: Date.now() / 1000,
              payload: { text, edited: true },
            },
          ])
        }
        setMonitorOpen(true)
      } catch (error) {
        toast(error instanceof Error ? error.message : String(error), 'error')
      } finally {
        setEditBusy(false)
      }
    },
    [settings, toast],
  )

  const setTaskPrompt = (taskId: string, text: string) => {
    setTasks((prev) =>
      prev.map((task) =>
        task.id === taskId
          ? { ...task, prompt: text, title: text.slice(0, 48), edit_count: (task.edit_count || 0) + 1 }
          : task,
      ),
    )
  }

  const archiveTask = useCallback(
    async (taskId: string, restore = false) => {
      try {
        await api.archiveTask(taskId, !restore)
        if (!restore) {
          setTasks((prev) => prev.filter((task) => task.id !== taskId))
          setArchived((prev) => {
            const found = tasks.find((task) => task.id === taskId)
            return found ? [{ ...found, archived: true }, ...prev] : prev
          })
          if (activeTaskId === taskId) newTask()
          toast('Task archived')
        } else {
          setArchived((prev) => prev.filter((task) => task.id !== taskId))
          const restored = archived.find((task) => task.id === taskId)
          if (restored) setTasks((prev) => [{ ...restored, archived: false }, ...prev])
          toast('Task restored')
        }
      } catch (error) {
        toast(error instanceof Error ? error.message : String(error), 'error')
      }
    },
    [activeTaskId, tasks, archived, newTask, toast],
  )

  const proposeMcp = useCallback(
    async (spec: {
      name: string
      title: string
      description: string
      parameters: string
      code: string
      reason: string
    }) => {
      try {
        await api.proposeChange({
          kind: 'register_mcp_tool',
          target: spec.name,
          reason: spec.reason || 'Created from the Create MCP panel',
          params: {
            name: spec.name,
            title: spec.title || spec.name,
            description: spec.description,
            parameters: JSON.parse(spec.parameters),
            code: spec.code,
          },
        })
        setDialog(null)
        setMonitorOpen(true)
        toast('MCP tool proposed — confirm it in the change card')
      } catch (error) {
        toast(error instanceof Error ? error.message : String(error), 'error')
      }
    },
    [toast],
  )

  const toggleMcp = useCallback(
    async (name: string, enabled: boolean) => {
      try {
        await api.setMcpEnabled(name, enabled)
        refreshBoot()
      } catch (error) {
        toast(error instanceof Error ? error.message : String(error), 'error')
      }
    },
    [refreshBoot, toast],
  )

  const deleteMcp = useCallback(
    async (name: string) => {
      try {
        await api.deleteMcp(name)
        refreshBoot()
        toast(`Removed MCP tool ${name}`)
      } catch (error) {
        toast(error instanceof Error ? error.message : String(error), 'error')
      }
    },
    [refreshBoot, toast],
  )

  /* --------------------------------------------------------- roundtable */
  const roundtableState = useMemo(
    () => (activeIsRoundtable ? reduceRoundtable(events) : EMPTY_ROUNDTABLE),
    [activeIsRoundtable, events],
  )

  const openRoundtable = useCallback(() => {
    setRoundtableError(null)
    api
      .roundtableRoster()
      .then((data) => setRoundtableRoster(data.providers))
      .catch(() => setRoundtableRoster(boot?.providers?.map((p) => ({ ...p, has_api_key: false })) || []))
    setDialog('roundtable')
  }, [boot])

  const startRoundtable = useCallback(
    async (payload: Parameters<typeof api.createRoundtable>[0]) => {
      setRoundtableError(null)
      try {
        const record = await api.createRoundtable(payload as Record<string, unknown>)
        setTasks((prev) => [record, ...prev.filter((task) => task.id !== record.id)])
        setActiveTaskId(record.id)
        setRunningTaskId(record.id)
        setEvents([])
        eventKeys.current = new Set()
        setDialog(null)
        setMonitorOpen(true)
        setSelectedSeat(null)
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)
        setRoundtableError(message)
        if (error instanceof ApiError && error.status === 401) setAdvancedOpen(true)
      }
    },
    [],
  )

  const sendRoundtableMessage = useCallback(
    async (text: string, toSeat: string) => {
      if (!activeTaskId) return
      try {
        await api.roundtableMessage(activeTaskId, text, toSeat)
      } catch (error) {
        toast(error instanceof Error ? error.message : String(error), 'error')
      }
    },
    [activeTaskId, toast],
  )

  const [addSeatOpen, setAddSeatOpen] = useState(false)
  const addRoundtableSeat = useCallback(
    async (seat: Record<string, unknown>) => {
      if (!activeTaskId) return
      try {
        await api.roundtableAddSeat(activeTaskId, seat)
        setAddSeatOpen(false)
        refreshBoot()
      } catch (error) {
        toast(error instanceof Error ? error.message : String(error), 'error')
      }
    },
    [activeTaskId, refreshBoot, toast],
  )

  /* ----------------------------------------------------------- derived */
  const provider = useMemo(() => currentProvider(boot, settings), [boot, settings])
  const thinkingLevels = boot?.thinking_levels || []
  const thinkingSlider = boot?.thinking_slider ?? {
    min: 0,
    max: 4,
    step: 1,
    stops: ['off', 'low', 'medium', 'high', 'max'],
    labels: ['Off', 'Low', 'Medium', 'High', 'Max'],
  }
  const thinkingLabel =
    thinkingSlider.labels[Math.max(0, thinkingSlider.stops.indexOf(settings?.thinking ?? 'off'))] ||
    'Off'
  const running = Boolean(runningTaskId)

  const resultsByCall = useMemo(() => {
    const map = new Map<string, OnionEvent>()
    for (const event of events) {
      if (event.type === 'tool-result') map.set(event.payload.call_id, event)
    }
    return map
  }, [events])

  const callIds = useMemo(() => {
    const set = new Set<string>()
    for (const event of events) {
      if (event.type === 'tool-call') set.add(event.payload.call_id)
    }
    return set
  }, [events])

  /** Scroll the timeline to an event and flash it (Star Chain Graph linkage). */
  const focusEvent = useCallback((eventId: string) => {
    if (!eventId) return
    const element = document.querySelector<HTMLElement>(`[data-event-id="${CSS.escape(eventId)}"]`)
    if (!element) return

    // Drive the container directly: `scrollIntoView` on a child competes with
    // the container's own `scroll-behavior: smooth`, and two competing
    // animations can cancel out, leaving the card unmoved.
    const container = element.closest<HTMLElement>('.onion-stream')
    if (container) {
      const offset =
        element.getBoundingClientRect().top -
        container.getBoundingClientRect().top +
        container.scrollTop -
        (container.clientHeight - element.clientHeight) / 2
      const max = container.scrollHeight - container.clientHeight
      container.scrollTo({ top: Math.max(0, Math.min(offset, max)), behavior: 'smooth' })
    } else {
      element.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }

    setHighlightedEventId(eventId)
    window.setTimeout(
      () => setHighlightedEventId((current) => (current === eventId ? null : current)),
      1900,
    )
  }, [])

  const eventActions = useMemo(
    () => ({
      onApply: applyChange,
      onReject: rejectChange,
      onRevert: revertVersion,
      onResolveApproval: resolveApproval,
      busyChangeId,
    }),
    [applyChange, rejectChange, revertVersion, resolveApproval, busyChangeId],
  )

  if (fatal) {
    return (
      <div style={{ padding: 48, fontFamily: 'system-ui' }}>
        <h1 style={{ fontSize: '1.2rem' }}>Onion Harness could not start</h1>
        <p className="text-muted">{fatal}</p>
      </div>
    )
  }

  const showEmpty = events.length === 0
  const mcpTools: McpToolRecord[] = boot?.mcp_tools || []
  const modes = boot?.modes || []
  const elevationLevels = boot?.elevation_levels || []

  return (
    <div className="onion-app onion-app--frameless">
      <TitleBar />
      <div className="onion-app__body">
        <Sidebar
          collapsed={collapsed}
          tasks={tasks}
          activeTaskId={activeTaskId}
          provider={provider}
          model={settings?.model || ''}
          thinkingLabel={thinkingLabel}
          onChangeCount={proposals.length}
          mcpCount={mcpTools.length}
          memoryCount={memoryStats?.total ?? 0}
          onNewTask={newTask}
          onNewRoundtable={openRoundtable}
          onSelectTask={selectTask}
          onOpenSettings={() => setDialog('settings')}
          onOpenHistory={() => setDialog('history')}
          onOpenMcp={() => setDialog('mcp')}
          onOpenMemory={() => {
            setDialog('memory')
            loadMemories()
          }}
          onPickModel={() => setDialog('model')}
          roundtableSeats={activeIsRoundtable ? roundtableState.seats : []}
          roundtableHostId={roundtableState.hostId}
          roundtableActiveSeat={roundtableState.activeSeat}
          roundtableCounts={activeIsRoundtable ? countSpeechesBySeat(roundtableState.speeches) : {}}
          onSelectSeat={(seatId) =>
            setSelectedSeat((current) => (current === seatId ? null : seatId))
          }
          onAddSeat={() => setAddSeatOpen(true)}
        />

        <div className="onion-main">
          <TopBar
            title={activeTask?.title || (settings?.model ? settings.model : 'Onion Harness')}
            collapsed={collapsed}
            onToggleSidebar={() => setCollapsed((value) => !value)}
            usage={usage}
            monitorOpen={monitorOpen}
            onToggleMonitor={() => setMonitorOpen((value) => !value)}
            connection={connection}
            running={running}
            previewOpen={previewOpen}
            onTogglePreview={() => setPreviewOpen((value) => !value)}
          />

          <div className="onion-stage">
            <div className="onion-stage__center">
              {/* The mode switch is its own capsule above the conversation, not
                  part of the input bar, so it reads as a workspace setting. */}
              {activeIsRoundtable ? (
                <div className="rt-split">
                  <RoundtableView
                    state={roundtableState}
                    topic={activeTask?.prompt || ''}
                    selectedSeat={selectedSeat}
                    onSelectSeat={(seatId) =>
                      setSelectedSeat((current) => (current === seatId ? null : seatId))
                    }
                  />
                  <RoundtableHall
                    events={events}
                    seats={roundtableState.seats}
                    hostId={roundtableState.hostId}
                    onSelectSeat={(seatId) =>
                      setSelectedSeat((current) => (current === seatId ? null : seatId))
                    }
                    onSend={sendRoundtableMessage}
                  />
                </div>
              ) : (
                <ModeBar modes={modes} value={currentMode} onChange={(id) => updateSetting({ mode: id })} />
              )}

              {activeIsRoundtable ? null : showEmpty ? (
                <EmptyState
                  onHint={(text) => setPrompt(text)}
                  providerLabel={provider?.label}
                  hasKey={Boolean(provider && settings?.configured_providers?.includes(provider.id))}
                  onOpenAdvanced={() => setAdvancedOpen(true)}
                />
              ) : (
                <div className="onion-stream">
                  <div className="onion-stream__inner">
                    {events.map((event) => {
                      const isUserAnchor = event.type === 'user-input'
                      const editingThis = isUserAnchor && editingTaskId === activeTask?.id
                      const rendered = editingThis
                        ? null
                        : renderEvent(event, resultsByCall, callIds, eventActions)

                      // Folded events (a tool-result merged into its tool-call)
                      // render nothing. Emitting an empty wrapper still consumed
                      // a flex gap, which doubled the space between consecutive
                      // tool cards. Skip the wrapper entirely.
                      if (!rendered && !editingThis) return null

                      // Tool traffic is denser than prose: mark it so the stream
                      // can tighten the gap between consecutive tool cards.
                      const isToolRow =
                        event.type === 'tool-call' || event.type === 'change-proposal'

                      return (
                        <div
                          key={event.id}
                          className="card"
                          data-event-id={event.id}
                          data-kind={isToolRow ? 'tool' : undefined}
                          data-highlighted={highlightedEventId === event.id ? 'true' : undefined}
                        >
                          {isUserAnchor && activeTask && !event.payload.edited && (
                            <TaskCardHeader
                              task={activeTask}
                              archived={Boolean(activeTask.archived)}
                              onEdit={() => setEditingTaskId(activeTask.id)}
                              onArchive={() => archiveTask(activeTask.id)}
                            />
                          )}
                          {editingThis ? (
                            <TaskEditor
                              initial={activeTask.prompt}
                              busy={editBusy}
                              onCancel={() => setEditingTaskId(null)}
                              onSubmit={(text) => editTask(activeTask.id, text)}
                            />
                          ) : (
                            rendered
                          )}
                        </div>
                      )
                    })}
                    {running && (
                      <div
                        className="card bubble-user"
                        style={{
                          alignSelf: 'flex-start',
                          background: 'transparent',
                          color: 'var(--md-sys-color-on-surface-variant)',
                          paddingLeft: 4,
                        }}
                      >
                        <span className="onion-thinking" aria-label="working">
                          <i />
                          <i />
                          <i />
                        </span>
                      </div>
                    )}
                    <div ref={endRef} />
                  </div>
                </div>
              )}

              {activeIsRoundtable ? null : (
              <Composer
                value={prompt}
                onChange={setPrompt}
                onSend={send}
                onStop={stop}
                running={running}
                workspace={workspace}
                onWorkspaceChange={(path) => {
                  setWorkspace(path)
                  updateSetting({ workspace: path })
                }}
                provider={provider}
                model={settings?.model || ''}
                onPickModel={() => setDialog('model')}
                thinking={settings?.thinking || 'off'}
                thinkingSlider={thinkingSlider}
                onThinkingChange={(id) => updateSetting({ thinking: id })}
                elevation={settings?.elevation || 'standard'}
                elevationLevels={elevationLevels}
                onElevationChange={(id) => updateSetting({ elevation: id })}
                mode={currentMode}
                modes={modes}
                attachments={attachments}
                onAttachmentsChange={setAttachments}
                onOpenAdvanced={() => setAdvancedOpen(true)}
                disabledReason={
                  provider && !settings?.configured_providers?.includes(provider.id)
                    ? 'Add an API key to send'
                    : undefined
                }
              />
              )}
            </div>

            {activeIsRoundtable ? (
              <>
                <MinutesPanel
                  open={monitorOpen && !selectedSeat}
                  onClose={() => setMonitorOpen(false)}
                  state={roundtableState}
                  onSelectSeat={(seatId) =>
                    setSelectedSeat((current) => (current === seatId ? null : seatId))
                  }
                />
                {selectedSeat ? (
                  <SeatInspector
                    seat={roundtableState.seats.find((seat) => seat.id === selectedSeat) || null}
                    speeches={roundtableState.speeches}
                    messages={roundtableState.messages}
                    seatNames={Object.fromEntries(
                      roundtableState.seats.map((seat) => [seat.id, seat.name]),
                    )}
                    onClose={() => setSelectedSeat(null)}
                    onSend={sendRoundtableMessage}
                  />
                ) : null}
              </>
            ) : (
            <MonitorPanel
              open={monitorOpen}
              onClose={() => setMonitorOpen(false)}
              usage={usage}
              events={events}
              proposals={proposals}
              onFocusProposal={() => undefined}
              taskPrompt={events.find((event) => event.type === 'user-input')?.payload.text || ''}
              onFocusEvent={focusEvent}
              onOpenPreview={(mode) => {
                setPreviewMode(mode)
                setPreviewOpen(true)
              }}
            />
            )}

            <SidecarPreview
              open={previewOpen}
              mode={previewMode}
              onModeChange={setPreviewMode}
              onClose={() => setPreviewOpen(false)}
              url={previewUrl}
              onUrlChange={setPreviewUrl}
              events={events}
              proposals={proposals}
              onPickedElement={(snippet) => setPrompt((current) => `${snippet}${current}`)}
            />
          </div>
        </div>
      </div>

      {dialog === 'model' && boot && settings && (
        <ModelPicker
          providers={boot.providers}
          thinkingLevels={thinkingLevels}
          settings={settings}
          onClose={() => setDialog(null)}
          onSaved={saveSettings}
        />
      )}
      {dialog === 'settings' && settings && boot && (
        <SettingsDialog
          settings={settings}
          dataDir={boot.data_dir}
          archivedCount={archived.length}
          mcpCount={mcpTools.length}
          onOpenArchive={() => setDialog('archive')}
          onOpenMcp={() => setDialog('mcp')}
          onClose={() => setDialog(null)}
          onSaved={saveSettings}
        />
      )}
      {dialog === 'history' && (
        <HistoryDialog
          revisions={revisions}
          onClose={() => setDialog(null)}
          onRevert={revertVersion}
          onRedo={redo}
          busy={Boolean(busyChangeId)}
        />
      )}
      {dialog === 'workspace' && (
        <WorkspaceDialog
          value={workspace}
          onClose={() => setDialog(null)}
          onSave={(path) => {
            setWorkspace(path)
            setDialog(null)
            api.updateSettings({ workspace: path }).then(saveSettings).catch(() => undefined)
          }}
        />
      )}
      {dialog === 'archive' && (
        <ArchiveDialog
          tasks={archived}
          onClose={() => setDialog(null)}
          onRestore={(id) => archiveTask(id, true)}
          onSelect={(id) => {
            setDialog(null)
            selectTask(id)
          }}
        />
      )}
      <AddSeatDialog
        open={addSeatOpen}
        roster={roundtableRoster.length ? roundtableRoster : (boot?.providers || []).map((p) => ({ ...p, has_api_key: false }))}
        existingNames={roundtableState.seats.map((seat) => seat.name)}
        onClose={() => setAddSeatOpen(false)}
        onAdd={addRoundtableSeat}
      />
      {dialog === 'roundtable' && boot && settings && (
        <RoundtableDialog
          open
          roster={roundtableRoster.length ? roundtableRoster : boot.providers.map((p) => ({ ...p, has_api_key: false }))}
          settings={settings}
          thinkingLevels={thinkingSlider.stops}
          onClose={() => setDialog(null)}
          onStart={startRoundtable}
          error={roundtableError}
        />
      )}
      {dialog === 'memory' && (
        <MemoryDialog
          open
          stats={memoryStats ?? null}
          memories={memories}
          onClose={() => setDialog(null)}
          onForget={forgetMemory}
          onPurge={purgeMemory}
          busy={memoryBusy}
        />
      )}
      {dialog === 'mcp' && (
        <McpDialog
          open
          tools={mcpTools}
          onClose={() => setDialog(null)}
          onPropose={proposeMcp}
          onToggle={toggleMcp}
          onDelete={deleteMcp}
          busy={Boolean(busyChangeId)}
        />
      )}

      <AdvancedDrawer
        open={advancedOpen}
        providers={boot?.providers || []}
        settings={settings || ({} as Settings)}
        onClose={() => setAdvancedOpen(false)}
        onSaveKey={(providerId, key) => api.saveApiKey(providerId, key).then((next) => {
          saveSettings(next)
          return next
        })}
        onClearKey={(providerId) => api.clearApiKey(providerId).then((next) => {
          saveSettings(next)
          return next
        })}
        onSaveBaseUrl={(providerId, baseUrl) => api.saveBaseUrl(providerId, baseUrl).then((next) => {
          saveSettings(next)
          return next
        })}
        onPickWorkspace={() => setDialog('workspace')}
      />

      <div className="onion-toasts">
        {toasts.map((item) => (
          <div key={item.id} className="onion-toast" data-kind={item.kind}>
            {item.text}
          </div>
        ))}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------- helpers */

function EmptyState({
  onHint,
  providerLabel,
  hasKey,
  onOpenAdvanced,
}: {
  onHint: (text: string) => void
  providerLabel?: string
  hasKey: boolean
  onOpenAdvanced: () => void
}) {
  const hints = [
    'List the files in the working directory and summarise the project',
    'Run a Python script that checks today’s date and prints it',
    'Inspect the largest 5 files here and report sizes',
    'Propose a change to Onion’s system prompt',
  ]
  return (
    <div className="onion-empty">
      <div className="onion-empty__mark">
        <OnionMark size={120} animated />
      </div>
      <h2 className="onion-empty__title">What should Onion do?</h2>
      <p className="onion-empty__subtitle">
        A layered agent runtime. Your prompt reaches the harness, which plans, calls local
        executors in an isolated workspace, and streams every step back here.
      </p>
      <div className="onion-hints">
        {hints.map((hint) => (
          <button key={hint} className="onion-hint" onClick={() => onHint(hint)}>
            {hint}
          </button>
        ))}
      </div>
      {!hasKey && (
        <button className="btn" data-variant="outlined" onClick={onOpenAdvanced}>
          <Sparkles size={14} />
          Connect a provider {providerLabel ? `(${providerLabel})` : ''}
        </button>
      )}
      <span className="text-muted" style={{ fontSize: '0.74rem' }}>
        <MonitorPlay size={12} style={{ verticalAlign: -2, marginRight: 4 }} />
        Switch modes above the input: Plan reasons read-only, Standard asks before changes,
        Strict Coding enforces verification.
      </span>
    </div>
  )
}

function ModeBar({
  modes,
  value,
  onChange,
}: {
  modes: { id: string; label: string; description: string }[]
  value: string
  onChange: (id: string) => void
}) {
  if (modes.length === 0) return null
  const active = modes.find((item) => item.id === value) ?? modes[1]
  return (
    <div className="mode-bar">
      <span className="mode-bar__label">Mode</span>
      <Segmented
        options={modes.map((item) => ({
          id: item.id,
          label: item.label,
          hint: item.description,
        }))}
        value={value}
        onChange={onChange}
        size="small"
        ariaLabel="Execution mode"
      />
      <span className="mode-bar__hint">{active?.description}</span>
    </div>
  )
}

function currentProvider(boot: Bootstrap | null, settings: Settings | null): ProviderPreset | undefined {
  if (!boot || !settings) return undefined
  return boot.providers.find((provider) => provider.id === settings.provider_id)
}

function countSpeechesBySeat(speeches: { seat_id: string }[]): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const speech of speeches) counts[speech.seat_id] = (counts[speech.seat_id] || 0) + 1
  return counts
}

function upsertProposal(list: ChangeRecord[], change: ChangeRecord): ChangeRecord[] {
  const next = list.filter((item) => item.id !== change.id)
  next.push(change)
  return next
}

function upsertRevision(list: Revision[], revision: Revision): Revision[] {
  const next = list.filter((item) => item.version !== revision.version)
  next.push(revision)
  return next.sort((a, b) => a.version - b.version)
}

function applyUsage(event: OnionEvent, setUsage: React.Dispatch<React.SetStateAction<UsagePayload>>) {
  if (event.type === 'token-usage') {
    setUsage((prev) => ({ ...prev, ...event.payload }))
  } else if (event.type === 'context-usage') {
    setUsage((prev) => ({
      ...prev,
      ratio: event.payload.ratio,
      level: event.payload.level,
      context_window: event.payload.context_window ?? prev.context_window,
      total_tokens: event.payload.prompt_tokens ?? prev.total_tokens,
    }))
  } else if (event.type === 'task-done' && event.payload.usage) {
    setUsage((prev) => ({ ...prev, ...event.payload.usage }))
  }
}