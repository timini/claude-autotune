// Autotune capture mod. It observes the main loop, builds one bounded
// envelope per finished response and hands it to `python3 -m autotune
// ingress`. It never changes what the engine returns: every hook passes the
// result of next(e) back untouched, and capture runs inside try/catch.

import type { EngineInterface, Register, ToolCallResult, TurnCompleteInput } from 'claude-code'

type Engine = EngineInterface

const SCHEMA = 'autotune.envelope/1'
const MAX_REQUEST_CHARS = 32000
const MAX_ANSWER_CHARS = 64000
const MAX_EXCERPT_CHARS = 2000
const MAX_TOOL_CALLS = 200
const MAX_SKILLS = 100
const MAX_INSTRUCTIONS = 200
const MAX_CAPTURE_ERRORS = 20
const MAX_REFUSAL_CATEGORY_CHARS = 200
const MAX_REFUSAL_EXPLANATION_CHARS = 8000
// A typed `/skill` expands just before its turn starts. Anything older than
// this when a turn starts came from somewhere else (a background subagent).
const PENDING_SKILL_MS = 2000
// Ingress rejects envelopes over 512 KiB; keep a margin for its own checks.
const MAX_ENVELOPE_BYTES = 500 * 1024
const INGRESS_TIMEOUT_MS = 1000
const COMMAND_TIMEOUT_MS = 20000
const SESSION_END_TIMEOUT_MS = 500
const MAX_WALK_DEPTH = 64
// Envelopes ingress could not take, kept in $.store for a later hook to resend.
const PENDING_KEY = 'autotune.pending'
const MAX_PENDING = 20
const MAX_PENDING_BYTES = 600 * 1024
const MAX_PENDING_ENVELOPE_BYTES = 512 * 1024
const PENDING_PER_HOOK = 2
// Ingress exit code for an envelope that can never be accepted.
const EXIT_INVALID = 2
const RESTART_SOURCES = new Set(['clear', 'resume', 'fork'])
const LIFECYCLE_SOURCES = new Set(['startup', 'resume', 'clear', 'compact', 'fork'])
const END_REASONS = new Set(['answer', 'aborted', 'refusal', 'error'])
const RESERVED_TOOL_KEYS = new Set(['tool', 'tool_use_id', 'agentId', 'requestMeta', 'consent'])
// Ingress answers that say the envelope itself is wrong; sending it again
// cannot help. Anything else (a failed spawn, a lost ack, a locked database,
// a slow git) is retried once with the same identity, which ingress dedupes.
const NO_RETRY_ERRORS = new Set([
  'invalid_json',
  'invalid_encoding',
  'invalid_field',
  'missing_field',
  'envelope_too_large',
  'nesting_too_deep',
  'unsupported_schema',
  'config_invalid',
])

type Instruction = { file_path: string; memory_type: string | null; load_reason: string | null }
type Skill = { skill: string; source: string | null; path: string | null; plugin_name: string | null }
type ToolCallRecord = {
  tool: string
  tool_use_id: string
  input_summary: string
  is_error: boolean | null
  result_excerpt: string | null
  denied: boolean
}

type Lifecycle = {
  lifecycle_id: string
  source: string
  transcript_path: string | null
  cwd: string | null
  instructions: Instruction[]
  instructionsCapped: boolean
  // False while the record exists only because InstructionsLoaded arrived
  // before this session's SessionStart.
  started: boolean
}

type TurnState = {
  turnId: string
  request_text: string
  requestTruncated: boolean
  started_at: string
  tool_calls: ToolCallRecord[]
  dropped_tool_calls: number
  skills: Skill[]
  skillsCapped: boolean
  truncated: Set<string>
  capture_errors: string[]
}

type Ack = {
  ok?: boolean
  skipped?: string
  error?: string
  queue?: { queued?: number; leased?: number; blocked?: number }
}

// Module state. A reload drops it; a turn whose start was lost is then sent
// with coverage.missing_turn_start set.
const lifecycles = new Map<string, Lifecycle>()
const turns = new Map<string, TurnState>()
// Positive answers only. A negative one is checked again each turn, so a
// repository enrolled mid-session (a shell `autotune enable`, a pull that
// brings in `.autotune.json`) starts capturing on its next turn.
const enrollment = new Map<string, true>()
let currentTurnId: string | undefined
let pendingSkills: { skill: Skill; at: number }[] = []
// Main-loop Skill tool calls in flight. A skill.prompt carries no loop id, so
// one seen during a turn counts only while the main loop runs the Skill tool.
let mainSkillCalls = 0

function nowIso(): string {
  return new Date().toISOString()
}

// Cuts to at most `max` UTF-16 units without splitting a surrogate pair.
function cut(text: string, max: number): { text: string; truncated: boolean } {
  if (text.length <= max) return { text, truncated: false }
  let end = max
  const code = text.charCodeAt(end - 1)
  if (code >= 0xd800 && code <= 0xdbff) end -= 1
  return { text: text.slice(0, end), truncated: true }
}

// Cuts identity-like strings to the lengths ingress accepts; an empty or
// missing value becomes `fallback`.
function cap(value: unknown, max: number, fallback: string): string {
  const text = typeof value === 'string' && value !== '' ? value : fallback
  return cut(text, max).text
}

function capOrNull(value: unknown, max: number): string | null {
  return typeof value === 'string' ? cut(value, max).text : null
}

function errorCode(err: unknown): string {
  if (err instanceof Error) return err.name || 'Error'
  return typeof err
}

function noteError(state: TurnState | undefined, code: string): void {
  if (state === undefined || state.capture_errors.length >= MAX_CAPTURE_ERRORS) return
  state.capture_errors.push(code.slice(0, 200))
}

async function isWorkerOrigin($: Engine): Promise<boolean> {
  const origin = await $.env.get('AUTOTUNE_ORIGIN')
  return origin !== undefined && origin !== '' && origin !== 'foreground'
}

async function pythonPath($: Engine): Promise<string> {
  const value = await $.env.get('AUTOTUNE_PYTHON')
  return value === undefined || value === '' ? 'python3' : value
}

function parentDir(path: string): string | undefined {
  const trimmed = path.length > 1 ? path.replace(/\/+$/, '') : path
  const slash = trimmed.lastIndexOf('/')
  if (slash < 0 || trimmed === '/') return undefined
  return slash === 0 ? '/' : trimmed.slice(0, slash)
}

function joinPath(dir: string, name: string): string {
  return dir.endsWith('/') ? dir + name : `${dir}/${name}`
}

async function exists($: Engine, path: string): Promise<boolean> {
  try {
    await $.fs.stat(path)
    return true
  } catch {
    return false
  }
}

// Optimization only: ingress makes the authoritative check. Walks up from cwd
// to the first directory holding `.git` (a directory, or a file in a linked
// worktree), which is what `git rev-parse --show-toplevel` reports, and looks
// for `.autotune.json` there. `$.session.repo().root` is not used because it
// names the main working tree for a linked worktree.
async function isEnrolled($: Engine, lifecycleId: string, cwd: string): Promise<boolean> {
  const key = `${lifecycleId}\u0000${cwd}`
  if (enrollment.has(key)) return true
  let answer = false
  let dir: string | undefined = cwd
  for (let depth = 0; dir !== undefined && depth < MAX_WALK_DEPTH; depth += 1) {
    if (await exists($, joinPath(dir, '.git'))) {
      answer = await exists($, joinPath(dir, '.autotune.json'))
      break
    }
    dir = parentDir(dir)
  }
  if (answer) enrollment.set(key, true)
  return answer
}

function lifecycleFor(sessionId: string): Lifecycle | undefined {
  return lifecycles.get(sessionId)
}

function newLifecycle(source: string, transcriptPath: string | null, cwd: string | null): Lifecycle {
  return {
    lifecycle_id: crypto.randomUUID(),
    source: LIFECYCLE_SOURCES.has(source) ? source : 'unknown',
    transcript_path: transcriptPath,
    cwd,
    instructions: [],
    instructionsCapped: false,
    started: false,
  }
}

async function sessionCwd($: Engine, fallback: string | null): Promise<string> {
  try {
    return await $.session.cwd()
  } catch {
    return fallback ?? ''
  }
}

function parseAck(stdout: string): Ack | undefined {
  const lines = stdout.split('\n').map(line => line.trim()).filter(line => line !== '')
  const last = lines[lines.length - 1]
  if (last === undefined) return undefined
  try {
    const value: unknown = JSON.parse(last)
    if (value === null || typeof value !== 'object' || Array.isArray(value)) return undefined
    return value as Ack
  } catch {
    return undefined
  }
}

// `exitCode` is absent when ingress did not run to completion (a failed
// spawn, a timeout) or its answer could not be read.
type IngressOutcome = { ack: Ack } | { error: string; exitCode?: number }

// Sends once and, on a failure that a resend can fix, once more if the hook's
// budget still covers a full attempt.
async function sendEnvelope(
  $: Engine,
  envelope: unknown,
  timeoutMs: number,
  remainingMs: () => number,
): Promise<IngressOutcome> {
  const first = await runIngress($, envelope, timeoutMs)
  if (!('error' in first) || NO_RETRY_ERRORS.has(first.error)) return first
  if (remainingMs() < timeoutMs * 2) return first
  const second = await runIngress($, envelope, timeoutMs)
  return 'error' in second ? { ...second, error: `${second.error} (after retry)` } : second
}

async function runIngress($: Engine, envelope: unknown, timeoutMs: number): Promise<IngressOutcome> {
  const python = await pythonPath($)
  let ran
  try {
    ran = await $.process.run([python, '-m', 'autotune', 'ingress'], {
      cwd: $.plugin.root,
      stdin: JSON.stringify(envelope),
      timeoutMs,
    })
  } catch {
    return { error: 'ingress_failed' }
  }
  const ack = parseAck(ran.stdout)
  if (ack === undefined) return { error: 'invalid_ack' }
  if (ack.ok !== true) {
    return { error: typeof ack.error === 'string' ? ack.error : 'ingress_error', exitCode: ran.exitCode }
  }
  if (ran.exitCode !== 0) return { error: `exit_${ran.exitCode}`, exitCode: ran.exitCode }
  return { ack }
}

// Only an envelope ingress called invalid is given up; a busy database, a
// bad config or a missing python may all be fixed by the next try.
function worthKeeping(outcome: IngressOutcome): boolean {
  return 'error' in outcome && outcome.exitCode !== EXIT_INVALID
}

// Store reads and writes run one at a time, so two hooks never interleave a
// read and a write of the queue.
let pendingLock: Promise<unknown> = Promise.resolve()

function withPending<T>(work: () => Promise<T>): Promise<T> {
  const run = pendingLock.then(work, work)
  pendingLock = run.catch(() => undefined)
  return run
}

async function loadPending($: Engine): Promise<unknown[]> {
  try {
    const value = await $.store.get(PENDING_KEY)
    return Array.isArray(value) ? value.filter(v => v !== null && typeof v === 'object') : []
  } catch {
    return []
  }
}

async function savePending($: Engine, list: unknown[]): Promise<void> {
  if (list.length === 0) await $.store.delete(PENDING_KEY)
  else await $.store.set(PENDING_KEY, list)
}

// Drops the oldest entries until the queue fits both caps.
function trimPending(list: unknown[]): void {
  while (list.length > MAX_PENDING) list.shift()
  while (list.length > 0 && byteLength(list) > MAX_PENDING_BYTES) list.shift()
}

async function keepForLater($: Engine, envelope: unknown): Promise<void> {
  try {
    if (byteLength(envelope) > MAX_PENDING_ENVELOPE_BYTES) return
    await withPending(async () => {
      const list = await loadPending($)
      list.push(envelope)
      trimPending(list)
      await savePending($, list)
    })
  } catch {
    // Losing the copy loses only this envelope.
  }
}

// Resends up to PENDING_PER_HOOK kept envelopes, oldest first, once each. A
// sent or invalid one leaves the queue; the first that fails for another
// reason stays, and so do the rest, since ingress is likely still down.
async function resendPending($: Engine, remainingMs: () => number): Promise<void> {
  try {
    const waiting = (await withPending(() => loadPending($))).slice(0, PENDING_PER_HOOK)
    const done = new Set<string>()
    for (const envelope of waiting) {
      if (remainingMs() < INGRESS_TIMEOUT_MS * 2) break
      const outcome = await runIngress($, envelope, INGRESS_TIMEOUT_MS)
      if (worthKeeping(outcome)) break
      done.add(JSON.stringify(envelope))
    }
    if (done.size === 0) return
    await withPending(async () => {
      const list = await loadPending($)
      await savePending($, list.filter(env => !done.has(JSON.stringify(env))))
    })
  } catch {
    // The queue stays as it was; a later hook tries again.
  }
}

function statusFor(outcome: IngressOutcome): string | undefined {
  if ('error' in outcome) return `autotune: capture error (${outcome.error.slice(0, 60)})`
  const queue = outcome.ack.queue
  const pending = (Number(queue?.queued) || 0) + (Number(queue?.leased) || 0)
  return pending > 0 ? `autotune: ${pending} queued` : undefined
}

function summarizeInput(e: Record<string, unknown>): string {
  const args: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(e)) {
    if (!RESERVED_TOOL_KEYS.has(key)) args[key] = value
  }
  let text: string
  try {
    text = JSON.stringify(args) ?? ''
  } catch {
    text = '[unserializable input]'
  }
  return text
}

function recordToolCall(state: TurnState, e: Record<string, unknown>, result: ToolCallResult): void {
  const input = cut(summarizeInput(e), MAX_EXCERPT_CHARS)
  if (input.truncated) state.truncated.add('tool_calls.input_summary')
  const denied = result.deny !== undefined
  let isError: boolean | null = null
  if (result.isError === true) isError = true
  else if (!denied && (result.text !== undefined || result.ref !== undefined)) isError = false
  let excerpt: string | null = null
  if (typeof result.text === 'string') {
    const cutText = cut(result.text, MAX_EXCERPT_CHARS)
    if (cutText.truncated) state.truncated.add('tool_calls.result_excerpt')
    excerpt = cutText.text
  }
  state.tool_calls.push({
    tool: cap(e.tool, 200, 'unknown'),
    tool_use_id: cap(e.tool_use_id, 200, 'unknown'),
    input_summary: input.text,
    is_error: isError,
    result_excerpt: excerpt,
    denied,
  })
  if (state.tool_calls.length > MAX_TOOL_CALLS) {
    state.tool_calls.shift()
    state.dropped_tool_calls += 1
  }
}

function addSkill(list: Skill[], skill: Skill): boolean {
  if (list.some(s => s.skill === skill.skill && s.path === skill.path)) return true
  if (list.length >= MAX_SKILLS) return false
  list.push(skill)
  return true
}

function byteLength(value: unknown): number {
  return new TextEncoder().encode(JSON.stringify(value)).length
}

type CompleteEnvelope = {
  tool_calls: ToolCallRecord[]
  answer_text: string
  request_text: string | null
  coverage: { truncated_fields: string[]; dropped_tool_calls: number; missing_turn_start: boolean; capture_errors: string[] }
  [key: string]: unknown
}

// Keeps the envelope under the ingress limit: first tool result excerpts,
// then the oldest tool calls, then the answer and request text.
function fitEnvelope(env: CompleteEnvelope): void {
  const truncated = new Set(env.coverage.truncated_fields)
  if (byteLength(env) <= MAX_ENVELOPE_BYTES) return
  for (const call of env.tool_calls) {
    if (call.result_excerpt !== null && call.result_excerpt.length > 200) {
      call.result_excerpt = cut(call.result_excerpt, 200).text
      truncated.add('tool_calls.result_excerpt')
    }
  }
  while (byteLength(env) > MAX_ENVELOPE_BYTES && env.tool_calls.length > 0) {
    const drop = Math.max(1, Math.ceil(env.tool_calls.length / 4))
    env.tool_calls.splice(0, drop)
    env.coverage.dropped_tool_calls += drop
  }
  let limit = env.answer_text.length
  while (byteLength(env) > MAX_ENVELOPE_BYTES && limit > 1000) {
    limit = Math.floor(limit / 2)
    env.answer_text = cut(env.answer_text, limit).text
    truncated.add('answer_text')
  }
  limit = env.request_text?.length ?? 0
  while (byteLength(env) > MAX_ENVELOPE_BYTES && env.request_text !== null && limit > 1000) {
    limit = Math.floor(limit / 2)
    env.request_text = cut(env.request_text, limit).text
    truncated.add('request_text')
  }
  env.coverage.truncated_fields = [...truncated]
}

// Older builds report an interrupt only through `isAborted`.
function endReason(e: TurnCompleteInput): string {
  const reason: unknown = (e as { reason?: unknown }).reason
  if (typeof reason === 'string' && END_REASONS.has(reason)) return reason
  if (e.isAborted === true) return 'aborted'
  return 'unknown'
}

function buildTurnComplete(
  e: TurnCompleteInput,
  sessionId: string,
  cwd: string,
  lifecycle: Lifecycle | undefined,
  state: TurnState | undefined,
): CompleteEnvelope {
  const truncated = new Set(state?.truncated ?? [])
  if (state?.requestTruncated) truncated.add('request_text')
  if (state?.skillsCapped) truncated.add('skills')
  if (lifecycle?.instructionsCapped) truncated.add('instructions')
  const answer = cut(typeof e.answer === 'string' ? e.answer : '', MAX_ANSWER_CHARS)
  if (answer.truncated) truncated.add('answer_text')
  let refusal: { category: string | null; explanation: string | null } | null = null
  if (e.reason === 'refusal') {
    const explanation = typeof e.refusal?.explanation === 'string'
      ? cut(e.refusal.explanation, MAX_REFUSAL_EXPLANATION_CHARS)
      : undefined
    if (explanation?.truncated) truncated.add('refusal.explanation')
    refusal = {
      category: capOrNull(e.refusal?.category, MAX_REFUSAL_CATEGORY_CHARS),
      explanation: explanation?.text ?? null,
    }
  }
  const usage = e.usage ?? null
  const env: CompleteEnvelope = {
    schema: SCHEMA,
    kind: 'turn_complete',
    origin: 'foreground',
    session_id: sessionId,
    cwd,
    sent_at: nowIso(),
    turn_id: e.turnId,
    lifecycle_id: lifecycle?.lifecycle_id ?? 'unknown',
    lifecycle_source: lifecycle?.source ?? 'unknown',
    transcript_path: lifecycle?.transcript_path ?? null,
    started_at: state?.started_at ?? null,
    completed_at: nowIso(),
    end_reason: endReason(e),
    refusal,
    duration_ms: Number.isFinite(e.durationMs) ? Math.max(0, Math.round(e.durationMs)) : null,
    usage,
    model: capOrNull(usage?.model, 200),
    request_text: state ? state.request_text : null,
    answer_text: answer.text,
    tool_calls: state ? state.tool_calls.map(call => ({ ...call })) : [],
    skills: state ? [...state.skills] : [],
    instructions: lifecycle ? [...lifecycle.instructions] : [],
    coverage: {
      truncated_fields: [...truncated],
      dropped_tool_calls: state?.dropped_tool_calls ?? 0,
      missing_turn_start: state === undefined,
      capture_errors: state ? [...state.capture_errors] : [],
    },
  }
  fitEnvelope(env)
  return env
}

export const register: Register = on => {
  on('classic.SessionStart', async ($, e, next) => {
    try {
      if (!(await isWorkerOrigin($))) {
        const transcript = e.transcript_path === '' ? null : e.transcript_path
        const existing = lifecycles.get(e.session_id)
        let record: Lifecycle
        if (existing !== undefined && !existing.started) {
          // Instruction loads seen before this start belong to it.
          record = existing
          record.source = LIFECYCLE_SOURCES.has(e.source) ? e.source : 'unknown'
        } else if (existing === undefined || RESTART_SOURCES.has(e.source)) {
          record = newLifecycle(e.source, transcript, e.cwd)
        } else {
          record = existing
        }
        record.transcript_path = transcript
        record.cwd = e.cwd
        record.started = true
        lifecycles.set(e.session_id, record)
      }
    } catch {
      // Capture never changes the session.
    }
    return next(e)
  })

  on('classic.InstructionsLoaded', async ($, e, next) => {
    // A subagent's loads are not the main loop's context.
    if (e.agent_id !== undefined) return next(e)
    try {
      if (!(await isWorkerOrigin($))) {
        let record = lifecycles.get(e.session_id)
        if (record === undefined) {
          record = newLifecycle('unknown', e.transcript_path === '' ? null : e.transcript_path, e.cwd)
          lifecycles.set(e.session_id, record)
        }
        if (!record.instructions.some(i => i.file_path === e.file_path)) {
          if (record.instructions.length < MAX_INSTRUCTIONS) {
            record.instructions.push({
              file_path: cap(e.file_path, 4096, 'unknown'),
              memory_type: capOrNull(e.memory_type, 50),
              load_reason: capOrNull(e.load_reason, 50),
            })
          } else {
            record.instructionsCapped = true
          }
        }
      }
    } catch {
      // Ignore.
    }
    return next(e)
  })

  on('skill.prompt', async ($, e, next) => {
    const result = await next(e)
    try {
      const skill: Skill = {
        skill: cap(e.skill, 200, 'unknown'),
        source: capOrNull(e.source, 200),
        path: capOrNull(e.path, 4096),
        plugin_name: capOrNull(e.pluginName, 200),
      }
      const state = currentTurnId === undefined ? undefined : turns.get(currentTurnId)
      if (state !== undefined) {
        // During a turn, only the main loop's own Skill tool calls count; a
        // subagent's skill or preload is not the main loop's.
        if (mainSkillCalls > 0 && !addSkill(state.skills, skill)) state.skillsCapped = true
      } else if (currentTurnId === undefined && pendingSkills.length < MAX_SKILLS) {
        // A typed `/skill` expands before its turn starts.
        pendingSkills.push({ skill, at: Date.now() })
      }
    } catch {
      // Ignore.
    }
    return result
  })

  on('turn.start', async ($, e, next) => {
    if (e.agentId !== undefined) return next(e)
    let state: TurnState | undefined
    let sessionId = ''
    let cwd = ''
    let lifecycle: Lifecycle | undefined
    try {
      if (!(await isWorkerOrigin($))) {
        sessionId = await $.session.id()
        lifecycle = lifecycleFor(sessionId)
        cwd = await sessionCwd($, lifecycle?.cwd ?? null)
        if (await isEnrolled($, lifecycle?.lifecycle_id ?? 'unknown', cwd)) {
          const request = cut(typeof e.text === 'string' ? e.text : '', MAX_REQUEST_CHARS)
          state = {
            turnId: e.turnId,
            request_text: request.text,
            requestTruncated: request.truncated,
            // Also the turn_start envelope's sent_at: ingress orders activity
            // by it, so it is taken before next(e), not after.
            started_at: nowIso(),
            tool_calls: [],
            dropped_tool_calls: 0,
            skills: [],
            skillsCapped: false,
            truncated: new Set(),
            capture_errors: [],
          }
          const now = Date.now()
          for (const pending of pendingSkills) {
            if (now - pending.at <= PENDING_SKILL_MS) addSkill(state.skills, pending.skill)
          }
          turns.set(e.turnId, state)
          currentTurnId = e.turnId
        }
        pendingSkills = []
      }
    } catch (err) {
      noteError(state, `turn_start:${errorCode(err)}`)
    }
    const result = await next(e)
    if (sessionId !== '') await resendPending($, () => next.budget.remainingMs)
    if (state !== undefined) {
      try {
        const envelope = {
          schema: SCHEMA,
          kind: 'turn_start',
          origin: 'foreground',
          session_id: sessionId,
          cwd,
          sent_at: state.started_at,
          turn_id: e.turnId,
          request_text: state.request_text,
          lifecycle_id: lifecycle?.lifecycle_id ?? 'unknown',
          lifecycle_source: lifecycle?.source ?? 'unknown',
        }
        const outcome = await sendEnvelope($, envelope, INGRESS_TIMEOUT_MS, () => next.budget.remainingMs)
        if ('error' in outcome) noteError(state, `turn_start_ingress:${outcome.error}`)
        if (worthKeeping(outcome)) await keepForLater($, envelope)
      } catch (err) {
        noteError(state, `turn_start_ingress:${errorCode(err)}`)
      }
    }
    return result
  })

  on('tool.call', async ($, e, next) => {
    // Only the model's own calls are evidence: another plugin's $.tool.call
    // has an origin other than the engine.
    const mainLoop = e.agentId === undefined && next.origin.plugin === 'engine'
    const isSkill = mainLoop && e.tool === 'Skill'
    if (isSkill) mainSkillCalls += 1
    let result
    try {
      result = await next(e)
    } finally {
      if (isSkill) mainSkillCalls -= 1
    }
    try {
      if (mainLoop && currentTurnId !== undefined) {
        const state = turns.get(currentTurnId)
        if (state !== undefined) {
          try {
            recordToolCall(state, e as unknown as Record<string, unknown>, result)
          } catch (err) {
            noteError(state, `tool_call:${errorCode(err)}`)
          }
        }
      }
    } catch {
      // Ignore.
    }
    return result
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId !== undefined) return next(e)
    const result = await next(e)
    try {
      if (await isWorkerOrigin($)) return result
      const state = turns.get(e.turnId)
      turns.delete(e.turnId)
      if (currentTurnId === e.turnId) currentTurnId = undefined
      const sessionId = await $.session.id()
      const lifecycle = lifecycleFor(sessionId)
      const cwd = await sessionCwd($, lifecycle?.cwd ?? null)
      if (state === undefined && !(await isEnrolled($, lifecycle?.lifecycle_id ?? 'unknown', cwd))) {
        return result
      }
      const envelope = buildTurnComplete(e, sessionId, cwd, lifecycle, state)
      const outcome = await sendEnvelope($, envelope, INGRESS_TIMEOUT_MS, () => next.budget.remainingMs)
      $.ui.status(statusFor(outcome))
      if (worthKeeping(outcome)) await keepForLater($, envelope)
    } catch (err) {
      try {
        $.ui.status(`autotune: capture error (${errorCode(err)})`)
      } catch {
        // Ignore.
      }
    }
    return result
  })

  on('session.end', async ($, e, next) => {
    try {
      if (!(await isWorkerOrigin($))) {
        const lifecycle = lifecycleFor(e.sessionId)
        const cwd = await sessionCwd($, lifecycle?.cwd ?? null)
        const remaining = next.budget.remainingMs
        const timeoutMs = Math.floor(Math.min(SESSION_END_TIMEOUT_MS, remaining / 2))
        if (timeoutMs >= 100 && (await isEnrolled($, lifecycle?.lifecycle_id ?? 'unknown', cwd))) {
          const envelope = {
            schema: SCHEMA,
            kind: 'session_end',
            origin: 'foreground',
            session_id: e.sessionId,
            cwd,
            sent_at: nowIso(),
            reason: cap(e.reason, 200, 'other'),
          }
          // Ingress orders activity by sent_at, so a late session_end is safe.
          if (worthKeeping(await runIngress($, envelope, timeoutMs))) await keepForLater($, envelope)
        }
        lifecycles.delete(e.sessionId)
        // Nothing from this session may be credited to the next one.
        pendingSkills = []
        currentTurnId = undefined
        mainSkillCalls = 0
      }
    } catch {
      // Best effort.
    }
    return next(e)
  })

  on('session.start', async ($, e, next) => {
    try {
      if (!(await isWorkerOrigin($))) {
        try {
          await $.command.register({
            name: 'autotune',
            description: 'Autotune status, reviews, candidates and controls',
            argumentHint: '[status|review [id]|candidate [id]|apply <id>|revert <id>|consolidate|pause|resume|retry|cancel <job>|purge|enable|doctor]',
          })
        } catch {
          // The command is a convenience; the session goes on without it.
        }
        await resendPending($, () => next.budget.remainingMs)
      }
    } catch {
      // Best effort.
    }
    return next(e)
  })

  // Registered only outside worker-origin sessions, so no origin check here.
  on('command.run', { command: 'autotune' }, async ($, e) => {
    const args = e.args.trim().split(/\s+/).filter(part => part !== '')
    if (args.length === 0) args.push('status')
    try {
      const cwd = await $.session.cwd()
      const python = await pythonPath($)
      const ran = await $.process.run([python, '-m', 'autotune', ...args, '--project-dir', cwd], {
        cwd: $.plugin.root,
        timeoutMs: COMMAND_TIMEOUT_MS,
      })
      // `enable` and friends can change enrollment.
      enrollment.clear()
      const out = ran.stdout.trim()
      if (out !== '') return { text: out }
      const err = ran.stderr.trim().split('\n').slice(-5).join('\n')
      return { text: err !== '' ? `autotune exited ${ran.exitCode}: ${err}` : `autotune exited ${ran.exitCode}` }
    } catch (err) {
      return { text: `autotune: could not run the command (${errorCode(err)})` }
    }
  })
}
