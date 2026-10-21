import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

const REPO = '/work/repo'
const OK_ACK = '{"ok": true, "kind": "turn_complete", "episode_id": "ep_1", "job_id": 1, "duplicate": false, "queue": {"queued": 2, "leased": 1, "blocked": 0}, "worker": "started"}'

type Envelope = Record<string, any>

type World = {
  envelopes: Envelope[]
  argvs: (readonly string[])[]
  statuses: (string | undefined)[]
  store: Map<string, unknown>
  commands: { argumentHint?: string; description?: string }[]
}

// One process.run answer: ack text with exit 0, ack text with an exit code,
// or 'reject' to make the call fail.
type Answer = string | 'reject' | { stdout: string; exitCode: number }

type WorldOptions = {
  sessionId: string
  env?: Record<string, string>
  enrolled?: boolean | (() => boolean)
  // What process.run answers. A list gives one answer per call, the last one
  // repeating.
  ingress?: Answer | Answer[]
  // What the plugin's store holds at the start.
  store?: Record<string, unknown>
}

// Stands in for the engine and the host beneath the plugin.
function world(on: On, options: WorldOptions): World {
  const seen: World = {
    envelopes: [],
    argvs: [],
    statuses: [],
    store: new Map(Object.entries(options.store ?? {})),
    commands: [],
  }
  const enrolledNow = () => {
    const e = options.enrolled ?? true
    return typeof e === 'function' ? e() : e
  }
  let runs = 0
  mock.env(on, options.env ?? {})
  on('session.id', () => ({ value: options.sessionId }))
  on('session.cwd', () => ({ value: `${REPO}/src` }))
  on('fs.stat', (_, e) => {
    const present = e.path === `${REPO}/.git` || (enrolledNow() && e.path === `${REPO}/.autotune.json`)
    return present
      ? { value: { kind: e.path.endsWith('.git') ? 'dir' : 'file', size: 0, mtimeMs: 0, isLink: false } }
      : { deny: 'ENOENT' }
  })
  on('process.run', (_, e) => {
    seen.argvs.push(e.argv)
    if (e.init?.stdin !== undefined) seen.envelopes.push(JSON.parse(e.init.stdin))
    const answers = Array.isArray(options.ingress) ? options.ingress : [options.ingress ?? OK_ACK]
    const answer = answers[Math.min(runs, answers.length - 1)]!
    runs += 1
    if (answer === 'reject') return { deny: 'spawn failed' }
    const reply = typeof answer === 'string' ? { stdout: answer, exitCode: 0 } : answer
    return {
      value: {
        exitCode: reply.exitCode,
        stdout: `${reply.stdout}\n`,
        stderr: '',
        isStdoutTruncated: false,
        isStderrTruncated: false,
      },
    }
  })
  on('ui.status', (_, e) => {
    seen.statuses.push(e.text)
    return { value: undefined }
  })
  on('command.register', (_, e) => {
    seen.commands.push(e as any)
    return { value: { command: 'autotune' } }
  })
  on('store.get', (_, e) => ({ value: structuredClone(seen.store.get(e.key)) }))
  on('store.set', (_, e) => {
    seen.store.set(e.key, JSON.parse(JSON.stringify(e.value)))
    return { value: undefined }
  })
  on('store.delete', (_, e) => {
    seen.store.delete(e.key)
    return { value: undefined }
  })
  on('store.keys', () => ({ value: [...seen.store.keys()] }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  on('classic.SessionStart', () => ({}))
  on('classic.InstructionsLoaded', () => ({}))
  on('session.end', (_, e) => ({ sessionId: e.sessionId }))
  return seen
}

const completes = (w: World) => w.envelopes.filter(env => env.kind === 'turn_complete')
const pending = (w: World) => (w.store.get('autotune.pending') ?? []) as Envelope[]
const INVALID = { stdout: '{"ok": false, "error": "invalid_field", "detail": "x"}', exitCode: 2 }
const BUSY = { stdout: '{"ok": false, "error": "persistence_failed", "detail": "locked"}', exitCode: 3 }

// A kept envelope as an earlier hook would have stored it.
function keptComplete(turnId: string, answer = 'earlier') {
  return {
    schema: 'autotune.envelope/1',
    kind: 'turn_complete',
    origin: 'foreground',
    session_id: 'sess-old',
    cwd: REPO,
    sent_at: '2026-10-20T10:00:00.000Z',
    turn_id: turnId,
    answer_text: answer,
  }
}

function finish(turnId: string, answer = 'All done.') {
  return { answer, durationMs: 1234, isAborted: false, turnId, reason: 'answer' as const }
}

test('a main-loop turn sends one turn_complete envelope and keeps the answer', async ($, on) => {
  const w = world(on, { sessionId: 'sess-main' })
  await $.classic.SessionStart({ source: 'startup', session_id: 'sess-main', cwd: REPO })
  await $.turn.start({ text: 'fix the bug', turnId: 'turn-1' })
  const result = await $.turn.complete(finish('turn-1', 'Fixed it.'))

  expect(result.text).toBe('Fixed it.')
  const sent = completes(w)
  expect(sent).toHaveLength(1)
  const env = sent[0]!
  expect(env.schema).toBe('autotune.envelope/1')
  expect(env.origin).toBe('foreground')
  expect(env.session_id).toBe('sess-main')
  expect(env.turn_id).toBe('turn-1')
  expect(env.end_reason).toBe('answer')
  expect(env.answer_text).toBe('Fixed it.')
  expect(env.request_text).toBe('fix the bug')
  expect(env.duration_ms).toBe(1234)
  expect(env.coverage.missing_turn_start).toBe(false)
  expect(env.lifecycle_source).toBe('startup')

  const argv = w.argvs[w.argvs.length - 1]!
  expect(argv.slice(1)).toEqual(['-m', 'autotune', 'ingress'])
  expect(argv[0]).toBe('python3')
  // Status reflects the ack's queued plus leased count, and stays one line.
  expect(w.statuses[w.statuses.length - 1]).toBe('autotune: 3 queued')
})

test('a subagent turn sends nothing', async ($, on) => {
  const w = world(on, { sessionId: 'sess-agent' })
  await $.turn.start({ text: 'look around', turnId: 'turn-a', agentId: 'agent-1' })
  const result = await $.turn.complete({ ...finish('turn-a', 'report'), agentId: 'agent-1' })

  expect(result.text).toBe('report')
  expect(w.envelopes).toHaveLength(0)
  expect(w.argvs).toHaveLength(0)
})

test('a worker-origin session captures and spawns nothing', async ($, on) => {
  const w = world(on, { sessionId: 'sess-worker', env: { AUTOTUNE_ORIGIN: 'worker' } })
  on('tool.call', () => ({ result: { stdout: 'ok' }, text: 'ok', ref: 1 }))
  await $.classic.SessionStart({ source: 'startup', session_id: 'sess-worker', cwd: REPO })
  await $.turn.start({ text: 'review this', turnId: 'turn-w' })
  await $.tool.call({ tool: 'Bash', command: 'ls' })
  const result = await $.turn.complete(finish('turn-w', 'reviewed'))
  await $.session.end({ reason: 'other', sessionId: 'sess-worker', resume: { id: 'sess-worker' } } as any)

  expect(result.text).toBe('reviewed')
  expect(w.argvs).toHaveLength(0)
  expect(w.statuses).toHaveLength(0)
})

test('an unenrolled repository captures nothing', async ($, on) => {
  const w = world(on, { sessionId: 'sess-unenrolled', enrolled: false })
  await $.turn.start({ text: 'hello', turnId: 'turn-u' })
  const result = await $.turn.complete(finish('turn-u', 'hi'))

  expect(result.text).toBe('hi')
  expect(w.argvs).toHaveLength(0)
})

test('a failing ingress still returns the original answer', async ($, on) => {
  const w = world(on, { sessionId: 'sess-reject', ingress: 'reject' })
  await $.turn.start({ text: 'one', turnId: 'turn-r' })
  const result = await $.turn.complete(finish('turn-r', 'answer one'))

  expect(result.text).toBe('answer one')
  // One send and one resend of the same envelope, then the error is shown.
  const sent = completes(w)
  expect(sent).toHaveLength(2)
  expect(sent[1]!.turn_id).toBe(sent[0]!.turn_id)
  expect(w.statuses[w.statuses.length - 1]).toMatch(/^autotune: capture error \(ingress_failed \(after retry\)\)/)
  // Both envelopes are kept for a later hook, the completion as it was sent.
  const kept = pending(w)
  expect(kept.map(env => env.kind)).toEqual(['turn_start', 'turn_complete'])
  expect(kept[1]).toEqual(sent[0])
})

test('a garbage acknowledgement still returns the original answer', async ($, on) => {
  const w = world(on, { sessionId: 'sess-garbage', ingress: 'Traceback (most recent call last):\nnot json' })
  await $.turn.start({ text: 'two', turnId: 'turn-g' })
  const result = await $.turn.complete(finish('turn-g', 'answer two'))

  expect(result.text).toBe('answer two')
  expect(w.statuses[w.statuses.length - 1]).toBe('autotune: capture error (invalid_ack (after retry))')
})

test('tool.call runs the tool once, returns its result unchanged, and is captured', async ($, on) => {
  let runs = 0
  const sentinel = { result: { stdout: 'file-a\nfile-b' }, text: 'file-a\nfile-b', ref: 7 }
  on('tool.call', () => {
    runs += 1
    return sentinel
  })
  const w = world(on, { sessionId: 'sess-tool' })
  await $.turn.start({ text: 'list files', turnId: 'turn-t' })
  const result = await $.tool.call({ tool: 'Bash', command: 'ls -1' })
  await $.turn.complete(finish('turn-t'))

  expect(runs).toBe(1)
  expect(result).toEqual(sentinel)
  const env = completes(w)[0]!
  expect(env.tool_calls).toHaveLength(1)
  const call = env.tool_calls[0]
  expect(call.tool).toBe('Bash')
  expect(call.input_summary).toContain('ls -1')
  expect(call.input_summary).not.toContain('tool_use_id')
  expect(call.result_excerpt).toBe('file-a\nfile-b')
  expect(call.is_error).toBe(false)
  expect(call.denied).toBe(false)
})

test('a completion with no observed start is marked missing_turn_start', async ($, on) => {
  const w = world(on, { sessionId: 'sess-missing' })
  const result = await $.turn.complete(finish('turn-orphan', 'late'))

  expect(result.text).toBe('late')
  const env = completes(w)[0]!
  expect(env.turn_id).toBe('turn-orphan')
  expect(env.request_text).toBeNull()
  expect(env.started_at).toBeNull()
  expect(env.coverage.missing_turn_start).toBe(true)
})

test('a clear starts a new lifecycle and resets loaded instructions', async ($, on) => {
  const w = world(on, { sessionId: 'sess-life' })
  await $.classic.SessionStart({ source: 'startup', session_id: 'sess-life', cwd: REPO })
  await $.classic.InstructionsLoaded({
    session_id: 'sess-life',
    cwd: REPO,
    file_path: `${REPO}/CLAUDE.md`,
    memory_type: 'Project',
    load_reason: 'session_start',
  })
  await $.turn.start({ text: 'first', turnId: 'turn-l1' })
  await $.turn.complete(finish('turn-l1'))

  await $.classic.SessionStart({ source: 'clear', session_id: 'sess-life', cwd: REPO })
  await $.turn.start({ text: 'second', turnId: 'turn-l2' })
  await $.turn.complete(finish('turn-l2'))

  const [first, second] = completes(w)
  expect(first!.instructions).toHaveLength(1)
  expect(first!.instructions[0].file_path).toBe(`${REPO}/CLAUDE.md`)
  expect(second!.lifecycle_source).toBe('clear')
  expect(second!.lifecycle_id).not.toBe(first!.lifecycle_id)
  expect(second!.lifecycle_id).not.toBe('unknown')
  expect(second!.instructions).toHaveLength(0)
})

test('long fields are cut and reported in coverage', async ($, on) => {
  const w = world(on, { sessionId: 'sess-long' })
  await $.turn.start({ text: 'q'.repeat(40000), turnId: 'turn-big' })
  await $.turn.complete(finish('turn-big', 'a'.repeat(70000)))

  const env = completes(w)[0]!
  expect(env.request_text.length).toBe(32000)
  expect(env.answer_text.length).toBe(64000)
  expect(env.coverage.truncated_fields).toContain('request_text')
  expect(env.coverage.truncated_fields).toContain('answer_text')
  expect(new TextEncoder().encode(JSON.stringify(env)).length).toBeLessThanOrEqual(512 * 1024)
})

test('a lost acknowledgement is resent once and the second send succeeds', async ($, on) => {
  const w = world(on, { sessionId: 'sess-retry', ingress: [OK_ACK, 'reject', OK_ACK] })
  await $.turn.start({ text: 'three', turnId: 'turn-x' })
  await $.turn.complete(finish('turn-x'))

  const sent = completes(w)
  expect(sent).toHaveLength(2)
  expect(sent[1]).toEqual(sent[0])
  expect(w.statuses[w.statuses.length - 1]).toBe('autotune: 3 queued')
})

test('an envelope ingress rejects as invalid is not resent', async ($, on) => {
  const w = world(on, { sessionId: 'sess-invalid', ingress: INVALID })
  await $.turn.complete(finish('turn-i'))

  expect(completes(w)).toHaveLength(1)
  expect(w.statuses[w.statuses.length - 1]).toBe('autotune: capture error (invalid_field)')
  expect(pending(w)).toEqual([])
})

test('a repository enrolled mid-session captures from its next turn', async ($, on) => {
  let enrolled = false
  const w = world(on, { sessionId: 'sess-enroll', enrolled: () => enrolled })
  await $.classic.SessionStart({ source: 'startup', session_id: 'sess-enroll', cwd: REPO })
  await $.turn.start({ text: 'before', turnId: 'turn-e1' })
  await $.turn.complete(finish('turn-e1'))
  expect(w.envelopes).toHaveLength(0)

  enrolled = true
  await $.turn.start({ text: 'after', turnId: 'turn-e2' })
  await $.turn.complete(finish('turn-e2'))
  const sent = completes(w)
  expect(sent).toHaveLength(1)
  expect(sent[0]!.turn_id).toBe('turn-e2')
  expect(sent[0]!.coverage.missing_turn_start).toBe(false)
})

test('an interrupted turn is recorded as aborted, also when only isAborted says so', async ($, on) => {
  const w = world(on, { sessionId: 'sess-abort' })
  await $.turn.start({ text: 'long job', turnId: 'turn-ab' })
  await $.turn.complete({ answer: '', durationMs: 5, isAborted: true, turnId: 'turn-ab', reason: 'aborted' })
  await $.turn.start({ text: 'again', turnId: 'turn-ab2' })
  const { reason: _drop, ...legacy } = { answer: '', durationMs: 5, isAborted: true, turnId: 'turn-ab2', reason: 'aborted' as const }
  await $.turn.complete(legacy as any)

  const [first, second] = completes(w)
  expect(first!.end_reason).toBe('aborted')
  expect(first!.refusal).toBeNull()
  expect(second!.end_reason).toBe('aborted')
})

test('a refusal carries its category and a capped explanation', async ($, on) => {
  const w = world(on, { sessionId: 'sess-refuse' })
  await $.turn.start({ text: 'do the bad thing', turnId: 'turn-rf' })
  await $.turn.complete({
    answer: '',
    durationMs: 5,
    isAborted: false,
    turnId: 'turn-rf',
    reason: 'refusal',
    refusal: { category: 'c'.repeat(300), explanation: 'e'.repeat(9000) },
  } as any)

  const env = completes(w)[0]!
  expect(env.end_reason).toBe('refusal')
  expect(env.refusal.category.length).toBe(200)
  expect(env.refusal.explanation.length).toBe(8000)
  expect(env.coverage.truncated_fields).toContain('refusal.explanation')
})

test('instructions a subagent loads are not part of the main lifecycle', async ($, on) => {
  const w = world(on, { sessionId: 'sess-sub-instr' })
  await $.classic.SessionStart({ source: 'startup', session_id: 'sess-sub-instr', cwd: REPO })
  await $.classic.InstructionsLoaded({
    session_id: 'sess-sub-instr',
    cwd: REPO,
    file_path: `${REPO}/CLAUDE.md`,
    memory_type: 'Project',
    load_reason: 'session_start',
  })
  await $.classic.InstructionsLoaded({
    session_id: 'sess-sub-instr',
    cwd: REPO,
    agent_id: 'agent-9',
    file_path: `${REPO}/src/foo/CLAUDE.md`,
    memory_type: 'Project',
    load_reason: 'nested_traversal',
  } as any)
  await $.turn.start({ text: 'explore', turnId: 'turn-si' })
  await $.turn.complete(finish('turn-si'))

  const env = completes(w)[0]!
  expect(env.instructions.map((i: any) => i.file_path)).toEqual([`${REPO}/CLAUDE.md`])
})

function skillPrompt(skill: string) {
  return { skill, text: `run ${skill}`, frontmatter: {}, source: 'projectSettings' } as any
}

test('only main-loop skills are credited to a turn', async ($, on) => {
  // The Skill tool stays running until the test has raised its skill.prompt.
  let entered!: () => void
  const inTool = new Promise<void>(resolve => { entered = resolve })
  let release!: () => void
  const done = new Promise<void>(resolve => { release = resolve })
  on('tool.call', async () => {
    entered()
    await done
    return { result: { ok: true }, text: 'ok', ref: 3 } as any
  })
  on('skill.prompt', (_, e) => ({ text: e.text }))
  const w = world(on, { sessionId: 'sess-skills' })
  // Typed `/typed` expands just before its turn starts.
  await $.skill.prompt(skillPrompt('typed'))
  await $.turn.start({ text: '/typed', turnId: 'turn-s1' })
  // A subagent preload during the turn: no main-loop Skill call is running.
  await $.skill.prompt(skillPrompt('subagent-preload'))
  const call = $.tool.call({ tool: 'Skill', skill: 'via-tool' } as any)
  await inTool
  await $.skill.prompt(skillPrompt('via-tool'))
  release()
  await call
  await $.turn.complete(finish('turn-s1'))

  const env = completes(w)[0]!
  expect(env.skills.map((s: any) => s.skill).sort()).toEqual(['typed', 'via-tool'])
})

test('a skill seen after the last turn is not carried into the next session', async ($, on) => {
  on('skill.prompt', (_, e) => ({ text: e.text }))
  const w = world(on, { sessionId: 'sess-carry' })
  await $.turn.start({ text: 'one', turnId: 'turn-c1' })
  await $.turn.complete(finish('turn-c1'))
  await $.skill.prompt(skillPrompt('background'))
  await $.session.end({ reason: 'clear', sessionId: 'sess-carry' } as any)
  await $.turn.start({ text: 'two', turnId: 'turn-c2' })
  await $.turn.complete(finish('turn-c2'))

  const last = completes(w).at(-1)!
  expect(last.turn_id).toBe('turn-c2')
  expect(last.skills).toEqual([])
})

test('a failed turn is resent oldest first at the next turn start, before its own envelope', async ($, on) => {
  const w = world(on, { sessionId: 'sess-pend', ingress: ['reject', 'reject', 'reject', 'reject', OK_ACK] })
  await $.turn.start({ text: 'one', turnId: 'turn-p1' })
  await $.turn.complete(finish('turn-p1', 'first answer'))
  expect(pending(w).map(env => env.kind)).toEqual(['turn_start', 'turn_complete'])

  const before = w.envelopes.length
  await $.turn.start({ text: 'two', turnId: 'turn-p2' })
  const after = w.envelopes.slice(before)
  expect(after.map(env => `${env.kind}:${env.turn_id}`)).toEqual([
    'turn_start:turn-p1',
    'turn_complete:turn-p1',
    'turn_start:turn-p2',
  ])
  expect(after[1]!.answer_text).toBe('first answer')
  expect(pending(w)).toEqual([])
  expect(w.store.has('autotune.pending')).toBe(false)
})

test('a resend that fails for a passing reason stays queued and stops the rest', async ($, on) => {
  const w = world(on, {
    sessionId: 'sess-busy',
    store: { 'autotune.pending': [keptComplete('old-1'), keptComplete('old-2')] },
    ingress: [BUSY, OK_ACK],
  })
  await $.turn.start({ text: 'now', turnId: 'turn-b' })

  // old-1 was tried once and kept; old-2 was not tried this time.
  expect(w.envelopes.map(env => env.turn_id)).toEqual(['old-1', 'turn-b'])
  expect(pending(w).map(env => env.turn_id)).toEqual(['old-1', 'old-2'])
})

test('a kept envelope ingress calls invalid is dropped, and an invalid send is never kept', async ($, on) => {
  const w = world(on, {
    sessionId: 'sess-drop',
    store: { 'autotune.pending': [keptComplete('bad-1')] },
    ingress: INVALID,
  })
  await $.turn.start({ text: 'now', turnId: 'turn-d' })
  await $.turn.complete(finish('turn-d'))

  expect(w.envelopes[0]!.turn_id).toBe('bad-1')
  expect(pending(w)).toEqual([])
})

test('an exit 3 refusal is kept even though it is not retried in the hook', async ($, on) => {
  const config = { stdout: '{"ok": false, "error": "config_invalid", "detail": "x"}', exitCode: 3 }
  const w = world(on, { sessionId: 'sess-config', ingress: config })
  await $.turn.complete(finish('turn-cfg'))

  expect(completes(w)).toHaveLength(1)
  expect(pending(w).map(env => env.turn_id)).toEqual(['turn-cfg'])
})

test('at most two kept envelopes are resent per hook', async ($, on) => {
  const w = world(on, {
    sessionId: 'sess-two',
    store: { 'autotune.pending': [keptComplete('k-1'), keptComplete('k-2'), keptComplete('k-3')] },
  })
  await $.turn.start({ text: 'now', turnId: 'turn-two' })

  expect(w.envelopes.map(env => env.turn_id)).toEqual(['k-1', 'k-2', 'turn-two'])
  expect(pending(w).map(env => env.turn_id)).toEqual(['k-3'])
})

test('session start resends kept envelopes', async ($, on) => {
  const w = world(on, {
    sessionId: 'sess-start',
    store: { 'autotune.pending': [keptComplete('s-1')] },
  })
  await $.session.start({ cwd: REPO } as any)

  expect(w.envelopes.map(env => env.turn_id)).toEqual(['s-1'])
  expect(pending(w)).toEqual([])
})

test('a worker-origin session never resends kept envelopes', async ($, on) => {
  const w = world(on, {
    sessionId: 'sess-wpend',
    env: { AUTOTUNE_ORIGIN: 'worker' },
    store: { 'autotune.pending': [keptComplete('w-1')] },
  })
  await $.session.start({ cwd: REPO } as any)
  await $.turn.start({ text: 'review', turnId: 'turn-wp' })

  expect(w.argvs).toHaveLength(0)
  expect(pending(w)).toHaveLength(1)
})

test('the queue keeps the newest 20 envelopes', async ($, on) => {
  const old = Array.from({ length: 20 }, (_, i) => keptComplete(`q-${i}`))
  const w = world(on, { sessionId: 'sess-cap', store: { 'autotune.pending': old }, ingress: 'reject' })
  await $.turn.complete(finish('turn-new'))

  const ids = pending(w).map(env => env.turn_id)
  expect(ids).toHaveLength(20)
  expect(ids[0]).toBe('q-1')
  expect(ids.at(-1)).toBe('turn-new')
})

test('the queue stays under 600 KiB by dropping the oldest', async ($, on) => {
  const w = world(on, { sessionId: 'sess-size', ingress: 'reject' })
  // About 190 KiB each: three fit, a fourth pushes out the first.
  for (const id of ['big-1', 'big-2', 'big-3', 'big-4']) {
    await $.turn.complete(finish(id, '\u4e00'.repeat(64000)))
  }
  const kept = pending(w)
  expect(new TextEncoder().encode(JSON.stringify(kept)).length).toBeLessThanOrEqual(600 * 1024)
  expect(kept.map(env => env.turn_id)).toEqual(['big-2', 'big-3', 'big-4'])
})

test('the command hint names every subcommand', async ($, on) => {
  const w = world(on, { sessionId: 'sess-cmd' })
  await $.session.start({ cwd: REPO } as any)

  const hint = w.commands[0]!.argumentHint!
  for (const part of ['status', 'review [id]', 'candidate [id]', 'apply <id>', 'revert <id>', 'consolidate', 'pause', 'resume', 'retry', 'cancel <job>', 'purge', 'enable', 'doctor']) {
    expect(hint).toContain(part)
  }
})
