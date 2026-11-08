/** Typed client for the local Onion API (HTTP + SSE). */

export type TaskRecord = {
  id: string
  title: string
  prompt: string
  workspace: string
  provider: string
  model: string
  mode?: string
  kind?: string
  roundtable?: Record<string, unknown>
  /** The persisted user-input event for this turn, if the server just created one. */
  user_event?: OnionEvent
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | string
  created_at: number
  updated_at: number
  archived?: boolean
  edit_count?: number
  turn_count?: number
  context_window?: number
  keep_recent?: number
  usage?: UsagePayload
}

export type ModeSpec = {
  id: string
  label: string
  icon: string
  description: string
  read_only: boolean
  requires_approval: boolean
  verifies_tool_output: boolean
}

export type ElevationLevel = { id: string; label: string; description: string }

export type ThinkingSlider = {
  min: number
  max: number
  step: number
  stops: string[]
  labels: string[]
}

export type McpToolRecord = {
  name: string
  title: string
  description: string
  parameters: Record<string, any>
  enabled: boolean
  created_at: number
  version?: number | null
  path?: string
  loaded?: boolean
}

/* ------------------------------------------------------------- roundtable */

export type SeatSpec = {
  id: string
  name: string
  role: 'host' | 'member' | string
  provider_id: string
  label: string
  base_url: string
  wire_format: string
  model: string
  thinking: string
  has_api_key?: boolean
  /** GUI-only: which provider this seat was configured from. */
  icon?: string
}

export type RoundtableConfig = {
  topic: string
  seats: SeatSpec[]
  host_id: string
  max_rounds: number
  dissent: string
  thinking: string
}

export type RoundtableSeatPublic = {
  id: string
  name: string
  role: string
  provider_id: string
  label: string
  model: string
  base_url: string
  thinking: string
}

export type ThoughtStep = {
  kind: 'read' | 'human' | 'reason' | 'decide' | 'reply' | string
  label: string
  text: string
}

export type RoundtableSpeech = {
  seat_id: string
  name: string
  round: number
  stance: 'agree' | 'reject' | 'neutral' | string
  content: string
  reasoning: string
  rebuts: string[]
  input_tokens: number
  output_tokens: number
  elapsed_ms: number
  thought?: ThoughtStep[]
  answered?: string
  raw?: string
}

export type RoundtableMessage = {
  text: string
  to: string
  round: number
  at: number
}

export type RoundtableDivergence = {
  round: number
  seat_id: string
  name: string
  stance: string
  point: string
  reasoning: string
  rebuts: string[]
}

export type RoundtableAgreement = {
  round: number
  seat_id: string
  name: string
  point: string
}

export type RoundtableState = {
  seats: RoundtableSeatPublic[]
  hostId: string
  maxRounds: number
  round: number
  speeches: RoundtableSpeech[]
  rebuttals: { from: string; to: string; round: number; reasoning: string }[]
  agreements: RoundtableAgreement[]
  divergences: RoundtableDivergence[]
  activeSeat: string | null
  messages: RoundtableMessage[]
  consensus: string | null
  breaker: { message: string; reason: string } | null
  done: boolean
  totalTokens: number
}

export type RoundtableRosterEntry = ProviderPreset & { has_api_key: boolean; custom?: boolean }

export type UsagePayload = {
  prompt_tokens?: number
  completion_tokens?: number
  total_tokens?: number
  context_window?: number
  ratio?: number
  level?: 'green' | 'yellow' | 'red'
}

export type OnionEvent = {
  id: string
  seq: number
  type: string
  task_id: string
  source: string
  timestamp: number
  payload: Record<string, any>
}

export type ProviderPreset = {
  id: string
  label: string
  base_url: string
  format: 'openai' | 'anthropic'
  icon: string
  models: string[]
  api_key_url: string
  builtin: boolean
  notes: string
}

export type ThinkingLevel = { id: string; label: string; budget_tokens: number }

export type ExecutorDesc = {
  name: string
  label: string
  icon: string
  language: string
  tool_name: string
  description: string
}

export type PanelConfig = {
  id: string
  type: string
  position: string
  visible: boolean
  size: string
}

export type PromptSegment = { id: string; title: string; enabled: boolean; chars: number }

export type ChangeRecord = {
  id: string
  task_id: string
  kind: string
  label: string
  target: string
  reason: string
  params: Record<string, any>
  diff: { path: string; before: any; after: any }[]
  impact: string[]
  reversible: boolean
  requires_ack: boolean
  status: string
  version: number | null
  created_at: number
}

export type Revision = {
  version: number
  change: ChangeRecord
  before: Record<string, any>
  after: Record<string, any>
  applied_at: number
  reverted_at: number | null
  redo_of: number | null
  note: string
}

export type Settings = {
  theme_mode: 'system' | 'light' | 'dark'
  provider_id: string
  model: string
  thinking: string
  workspace: string
  max_turns: number
  keep_recent_messages: number
  context_window?: number
  mode?: string
  elevation?: string
  configured_providers?: string[]
  /** Per-provider base URL overrides, including custom endpoints. */
  base_urls?: Record<string, string>
  base_url?: string
  active_persona?: string
  temperature?: number | null
  top_p?: number | null
  auto_review?: Partial<AutoReviewConfig>
  web_search?: Partial<WebSearchConfig>
}

export type UINodeSpec = {
  tag: string
  attrs?: Record<string, string>
  text?: string
  children?: UINodeSpec[]
}

export type UIActionSpec = { id: string; label: string }

export type PersonaOption = {
  id: string
  label: string
  description: string
  overlay: string
  sampling: { temperature: number; top_p: number }
  builtin: boolean
}

export type WebSearchConfig = {
  enabled: boolean
  base_url: string
  timeout: number
  max_results: number
}

export type AutoReviewConfig = {
  enabled: boolean
  provider_id: string
  model: string
  timeout_seconds: number
  fail_closed: boolean
}

export type MemoryRecord = {
  id: string
  text: string
  kind: string
  weight: number
  source_task: string
  created_at: number
  last_used_at: number
  use_count: number
}

export type MemoryStats = {
  enabled: boolean
  total: number
  by_kind: Record<string, number>
}

export type Bootstrap = {
  settings: Settings
  providers: ProviderPreset[]
  thinking_levels: ThinkingLevel[]
  thinking_slider: ThinkingSlider
  executors: ExecutorDesc[]
  panels: PanelConfig[]
  prompt_segments: PromptSegment[]
  skills: { name: string; description: string; enabled: boolean }[]
  modes: ModeSpec[]
  elevation_levels: ElevationLevel[]
  mcp_tools: McpToolRecord[]
  memory?: MemoryStats
  personas?: PersonaOption[]
  persona_errors?: string[]
  active_persona?: PersonaOption
  auto_review?: AutoReviewConfig
  web_search?: WebSearchConfig
  tasks: TaskRecord[]
  archived: TaskRecord[]
  history: Revision[]
  proposals: ChangeRecord[]
  version: number
  data_dir: string
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  const text = await response.text()
  const data = text ? JSON.parse(text) : {}
  if (!response.ok) {
    const message = (data && (data.error || data.message)) || `Request failed (${response.status})`
    throw new ApiError(message, response.status, data?.code)
  }
  return data as T
}

export class ApiError extends Error {
  status: number
  code?: string
  constructor(message: string, status: number, code?: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

export const api = {
  bootstrap: () => request<Bootstrap>('/bootstrap'),
  settings: () => request<Settings>('/settings'),
  updateSettings: (patch: Record<string, unknown>) =>
    request<Settings>('/settings', { method: 'POST', body: JSON.stringify(patch) }),
  saveApiKey: (providerId: string, apiKey: string) =>
    request<Settings>('/settings/api-key', {
      method: 'POST',
      body: JSON.stringify({ provider_id: providerId, api_key: apiKey }),
    }),
  saveBaseUrl: (providerId: string, baseUrl: string) =>
    request<Settings>('/settings', {
      method: 'POST',
      body: JSON.stringify({ provider_id: providerId, base_url: baseUrl }),
    }),
  clearApiKey: (providerId: string) =>
    request<Settings>(`/settings/api-key?provider_id=${encodeURIComponent(providerId)}`, {
      method: 'DELETE',
    }),
  tasks: () => request<{ tasks: TaskRecord[] }>('/tasks'),
  createTask: (payload: Record<string, unknown>) =>
    request<TaskRecord>('/tasks', { method: 'POST', body: JSON.stringify(payload) }),
  cancelTask: (taskId: string) =>
    request<{ task_id: string }>(`/tasks/${taskId}/cancel`, { method: 'POST' }),
  deleteTask: (taskId: string) =>
    request<{ task_id: string }>(`/tasks/${taskId}`, { method: 'DELETE' }),
  taskEvents: (taskId: string) => request<{ events: OnionEvent[] }>(`/tasks/${taskId}/events`),
  history: () => request<{ history: Revision[]; proposals: ChangeRecord[] }>('/history'),
  proposeChange: (payload: Record<string, unknown>) =>
    request<{ change_id: string }>('/changes', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  applyChange: (changeId: string, payload: Record<string, unknown> = {}) =>
    request<Revision>(`/changes/${changeId}/apply`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  rejectChange: (changeId: string, reason = '') =>
    request<ChangeRecord>(`/changes/${changeId}/reject`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    }),
  revertVersion: (version: number) =>
    request<{ reverted: Revision[] }>(`/history/${version}/revert`, { method: 'POST' }),
  redo: () => request<{ redone: Revision[] }>('/history/redo', { method: 'POST' }),
  editTask: (taskId: string, prompt: string, model?: Record<string, unknown>) =>
    request<TaskRecord>(`/tasks/${taskId}/edit`, {
      method: 'POST',
      body: JSON.stringify({ prompt, model }),
    }),
  archiveTask: (taskId: string, archived = true) =>
    request<{ task_id: string; archived: boolean }>(`/tasks/${taskId}/archive`, {
      method: 'POST',
      body: JSON.stringify({ archived }),
    }),
  resolveApproval: (taskId: string, approvalId: string, approved: boolean, note = '') =>
    request<{ approved: boolean }>(`/tasks/${taskId}/approvals/${approvalId}`, {
      method: 'POST',
      body: JSON.stringify({ approved, note }),
    }),
  setMcpEnabled: (name: string, enabled: boolean) =>
    request<{ name: string; enabled: boolean }>(`/mcp/${name}/enabled`, {
      method: 'POST',
      body: JSON.stringify({ enabled }),
    }),
  deleteMcp: (name: string) =>
    request<{ name: string; deleted: boolean }>(`/mcp/${name}`, { method: 'DELETE' }),
  mcpTools: () => request<{ tools: McpToolRecord[] }>('/mcp'),
  memory: () => request<{ stats: MemoryStats; memories: MemoryRecord[] }>('/memory'),
  addMemory: (text: string, kind: string) =>
    request<MemoryRecord>('/memory', { method: 'POST', body: JSON.stringify({ text, kind }) }),
  updateMemory: (id: string, patch: { text: string; kind: string }) =>
    request<MemoryRecord>(`/memory/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),
  forgetMemory: (id: string) =>
    request<{ forgotten: string }>(`/memory/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  purgeMemory: () => request<{ purged: number }>('/memory', { method: 'DELETE' }),
  archivedTasks: () => request<{ tasks: TaskRecord[] }>('/tasks/archived'),
  roundtableRoster: () =>
    request<{ providers: RoundtableRosterEntry[] }>('/roundtable/roster'),
  createRoundtable: (payload: Record<string, unknown>) =>
    request<TaskRecord>('/roundtable', { method: 'POST', body: JSON.stringify(payload) }),
  roundtableMessage: (taskId: string, text: string, to = 'all') =>
    request<{ message: RoundtableMessage }>(`/roundtable/${taskId}/messages`, {
      method: 'POST',
      body: JSON.stringify({ text, to }),
    }),
  roundtableAddSeat: (taskId: string, seat: Record<string, unknown>) =>
    request<{ seat: Record<string, unknown> }>(`/roundtable/${taskId}/seats`, {
      method: 'POST',
      body: JSON.stringify(seat),
    }),
  uiAction: (taskId: string, actionId: string) =>
    request<TaskRecord>(`/tasks/${taskId}/ui-action`, {
      method: 'POST',
      body: JSON.stringify({ action_id: actionId }),
    }),
  continueTask: (taskId: string, payload: Record<string, unknown>) =>
    request<TaskRecord>(`/tasks/${taskId}/continue`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  updateTaskContext: (taskId: string, patch: Record<string, unknown>) =>
    request<TaskRecord>(`/tasks/${taskId}/context`, {
      method: 'POST',
      body: JSON.stringify(patch),
    }),
  taskHistory: (taskId: string) =>
    request<{ history: { role: string; text: string }[] }>(`/tasks/${taskId}/history`),
  roundtableThoughts: (taskId: string) =>
    request<{ seats: { seat: any; thoughts: RoundtableSpeech[]; messages: RoundtableMessage[] }[] }>(
      `/roundtable/${taskId}/thoughts`,
    ),
}

/** Subscribe to the global SSE stream. Returns an unsubscribe function. */
export function openEventStream(
  onEvent: (event: OnionEvent) => void,
  onStatus?: (state: 'open' | 'closed' | 'error') => void,
): () => void {
  let source: EventSource | null = null
  let closed = false
  let retry = 0

  const connect = () => {
    if (closed) return
    source = new EventSource('/api/events')
    source.onopen = () => {
      retry = 0
      onStatus?.('open')
    }
    source.onmessage = (message) => {
      try {
        onEvent(JSON.parse(message.data) as OnionEvent)
      } catch {
        /* ignore malformed frames */
      }
    }
    source.onerror = () => {
      onStatus?.('error')
      source?.close()
      if (closed) return
      retry += 1
      setTimeout(connect, Math.min(5000, 400 * retry))
    }
  }

  connect()
  return () => {
    closed = true
    source?.close()
    onStatus?.('closed')
  }
}