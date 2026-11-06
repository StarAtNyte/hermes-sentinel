// Sentinel desktop half: the "Sentinel" page (sidebar entry + status-bar chip).
//
// SDK-only (catalog rule 8): imports are '@hermes/plugin-sdk', 'react' and 'react/jsx-runtime'
// only; no JSX (disk plugins load uncompiled); no DOM access outside the React tree; the
// backend is reached through ctx.rest / ctx.socket (/api/plugins/hermes-sentinel/...).
//
// Sections (the loader wants one file, so they are marked):
//   1. constants + pure helpers          (exported for tests)
//   2. controller: polling, REST, state  (exported for tests, no React inside)
//   3. UI components
//   4. plugin registration
import {
  Badge, Button, CopyButton, ROUTES_AREA, SIDEBAR_NAV_AREA, STATUSBAR_AREAS, host
} from '@hermes/plugin-sdk'
import { useEffect, useState, useSyncExternalStore } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

// ---------------------------------------------------------------------------------------
// 1. constants + pure helpers
// ---------------------------------------------------------------------------------------

export const PLUGIN_ID = 'hermes-sentinel'
export const ROUTE_PATH = '/sentinel'
const API_BASE = `/api/plugins/${PLUGIN_ID}`

export const GATEWAY_YAML = [
  'plugins:',
  '  entries:',
  '    hermes-sentinel:',
  '      allow_gateway_injection: true'
].join('\n')

export const RECIPES = [
  'package-at-door', 'pet-on-couch', 'print-failure',
  'training-loss-plateau', 'desk-intruder', 'stove-unattended'
]

export const MAX_RULE_TEXT = 500
const MAX_EVENTS = 50
const MAX_SNAPSHOT_CACHE = 80
const REST_TIMEOUT_MS = 8000
const WATCH_TIMEOUT_MS = 30000

/** Seconds between refreshes of each resource while the page is open. */
export const INTERVALS = { status: 2, sources: 3, rules: 4, events: 4, frame: 1 }
/** The status-bar chip alone only needs the status, and not often. */
const CHIP_STATUS_INTERVAL = 5

const clamp01 = n => Math.max(0, Math.min(1, Number.isFinite(n) ? n : 0))

export function pct(used, cap) {
  if (!Number.isFinite(used) || !Number.isFinite(cap) || cap <= 0) return 0
  return Math.round(clamp01(used / cap) * 100)
}

/** "just now", "12s ago", "3m ago", "2h ago", "4d ago". */
export function fmtAge(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return 'never'
  const s = Math.max(0, Math.round(seconds))
  if (s < 5) return 'just now'
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

/** Relative time for an ISO timestamp (or epoch seconds) against `nowMs`. */
export function relTime(ts, nowMs = Date.now()) {
  if (ts == null || ts === '') return 'never'
  const ms = typeof ts === 'number' ? ts * 1000 : Date.parse(ts)
  if (!Number.isFinite(ms)) return 'unknown'
  return fmtAge((nowMs - ms) / 1000)
}

/** Backend errors surface as "<status>: {json}" or plain text; reduce to a readable line. */
export function errMessage(err) {
  const raw = String((err && err.message) || err || 'Request failed')
  const brace = raw.indexOf('{')
  if (brace >= 0) {
    try {
      const body = JSON.parse(raw.slice(brace))
      if (body && typeof body.error === 'string') return body.error
      if (body && typeof body.detail === 'string') return body.detail
    } catch { /* fall through to the raw text */ }
  }
  return raw.slice(0, 300)
}

export function validateWatch(text, maxLen = MAX_RULE_TEXT) {
  const t = String(text || '').trim()
  if (!t) return { ok: false, error: 'Describe what to watch for.' }
  if (t.length > maxLen) return { ok: false, error: `Keep it under ${maxLen} characters.` }
  return { ok: true, text: t }
}

const asList = v => (Array.isArray(v) ? v : [])
const enc = s => encodeURIComponent(String(s))

// ---------------------------------------------------------------------------------------
// 2. controller (polling + REST + state). No React in here.
// ---------------------------------------------------------------------------------------

const EMPTY_STATE = {
  status: null,
  sources: [],
  rules: [],
  events: [],
  loaded: { status: false, sources: false, rules: false, events: false },
  errors: { status: null, sources: null, rules: null, events: null },
  frames: {},      // source key -> { src, at }
  snaps: {},       // event id  -> data URL (or direct URL) of the snapshot
  thresholds: {},  // event id  -> threshold returned by the feedback endpoint
  busy: {},        // action key -> true while in flight
  notice: null,    // { kind: 'ok' | 'error', message }
  now: Date.now()
}

export function createController(ctx) {
  let state = { ...EMPTY_STATE, now: Date.now() }
  const listeners = new Set()
  const inflight = new Set()
  const lastRun = {}
  let pageViews = 0
  let chipViews = 0
  let timer = null
  let hidden = false
  let socketOff = null
  // 'data' once the backend answers ?encoding=data_url; 'direct' falls back to <img src>.
  let imageMode = 'unknown'

  const emit = () => { listeners.forEach(fn => fn()) }
  const set = patch => { state = { ...state, ...patch }; emit() }
  const setIn = (key, patch) => set({ [key]: { ...state[key], ...patch } })
  const getState = () => state
  const subscribe = fn => { listeners.add(fn); return () => listeners.delete(fn) }

  async function call(path, opts = {}) {
    const res = await ctx.rest(path, { timeoutMs: REST_TIMEOUT_MS, ...opts })
    if (res && res.ok === false) throw new Error(res.error || 'Request failed')
    return res
  }

  // Run `fn` for `key` unless one is already in flight (a slow camera must not stack calls).
  async function guarded(key, fn) {
    if (inflight.has(key)) return
    inflight.add(key)
    try { await fn() } finally { inflight.delete(key) }
  }

  const loaders = {
    status: () => guarded('status', async () => {
      try {
        const status = await call('/status')
        set({ status, loaded: { ...state.loaded, status: true }, errors: { ...state.errors, status: null } })
      } catch (err) {
        set({ loaded: { ...state.loaded, status: true }, errors: { ...state.errors, status: errMessage(err) } })
      }
    }),
    sources: () => guarded('sources', async () => {
      try {
        const sources = asList(await call('/sources'))
        set({ sources, loaded: { ...state.loaded, sources: true }, errors: { ...state.errors, sources: null } })
      } catch (err) {
        set({ loaded: { ...state.loaded, sources: true }, errors: { ...state.errors, sources: errMessage(err) } })
      }
    }),
    rules: () => guarded('rules', async () => {
      try {
        const rules = asList(await call('/rules'))
        set({ rules, loaded: { ...state.loaded, rules: true }, errors: { ...state.errors, rules: null } })
      } catch (err) {
        set({ loaded: { ...state.loaded, rules: true }, errors: { ...state.errors, rules: errMessage(err) } })
      }
    }),
    events: () => guarded('events', async () => {
      try {
        const events = asList(await call(`/events?limit=${MAX_EVENTS}`))
        set({ events, loaded: { ...state.loaded, events: true }, errors: { ...state.errors, events: null } })
      } catch (err) {
        set({ loaded: { ...state.loaded, events: true }, errors: { ...state.errors, events: errMessage(err) } })
      }
    })
  }

  // -- images: frames and snapshots ------------------------------------------------------
  // ctx.rest parses JSON, so the jpeg endpoints are first asked for a JSON data-URL form
  // (?encoding=data_url -> {ok, data_url}); a backend that only serves raw jpeg is reached
  // with a plain <img src> instead (works wherever the page origin can reach the API).
  function directUrl(path) { return `${API_BASE}${path}` }

  async function fetchImage(path) {
    if (imageMode !== 'direct') {
      try {
        const sep = path.includes('?') ? '&' : '?'
        const res = await ctx.rest(`${path}${sep}encoding=data_url`, { timeoutMs: REST_TIMEOUT_MS })
        if (res && typeof res.data_url === 'string' && res.data_url.startsWith('data:image/')) {
          imageMode = 'data'
          return { src: res.data_url, missing: false }
        }
        if (res && res.ok === false) return { src: null, missing: true }
      } catch (err) {
        if (/404|not found/i.test(String(err && err.message))) return { src: null, missing: true }
        if (imageMode === 'data') return { src: null, missing: true }
      }
      imageMode = 'direct'
    }
    return { src: null, direct: true }
  }

  async function refreshFrame(key) {
    await guarded(`frame:${key}`, async () => {
      const path = `/frame/${enc(key)}`
      const got = await fetchImage(path)
      const at = Date.now()
      const frame = got.direct
        ? { src: `${directUrl(path)}?t=${at}`, at }
        : { src: got.src, at, missing: got.missing }
      set({ frames: { ...state.frames, [key]: frame } })
    })
  }

  async function loadSnapshot(id) {
    if (state.snaps[id] !== undefined) return
    await guarded(`snap:${id}`, async () => {
      const path = `/events/${enc(id)}/snapshot`
      const got = await fetchImage(path)
      const src = got.direct ? directUrl(path) : got.src
      const keys = Object.keys(state.snaps)
      const snaps = keys.length >= MAX_SNAPSHOT_CACHE
        ? Object.fromEntries(keys.slice(keys.length - MAX_SNAPSHOT_CACHE + 20).map(k => [k, state.snaps[k]]))
        : { ...state.snaps }
      snaps[id] = src || ''
      set({ snaps })
    })
  }

  // -- polling ---------------------------------------------------------------------------
  function due(name, every, force) {
    const t = Date.now()
    if (!force && lastRun[name] && t - lastRun[name] < every * 1000 - 50) return false
    lastRun[name] = t
    return true
  }

  function tick(force = false) {
    if (hidden && !force) return
    set({ now: Date.now() })
    if (pageViews > 0) {
      if (due('status', INTERVALS.status, force)) loaders.status()
      if (due('sources', INTERVALS.sources, force)) loaders.sources()
      if (due('rules', INTERVALS.rules, force)) loaders.rules()
      if (due('events', INTERVALS.events, force)) loaders.events()
      if (due('frames', INTERVALS.frame, force)) {
        state.sources.filter(s => s.online).forEach(s => { refreshFrame(s.key) })
      }
      state.events.slice(0, MAX_EVENTS).filter(e => e.has_snapshot).forEach(e => { loadSnapshot(e.id) })
    } else if (chipViews > 0 && due('status', CHIP_STATUS_INTERVAL, force)) {
      loaders.status()
    }
  }

  function ensureTimer() {
    if (timer || (pageViews === 0 && chipViews === 0)) return
    timer = ctx.setInterval(() => tick(false), 1000)
    ctx.addEventListener(document, 'visibilitychange', onVisibility)
  }

  function onVisibility() {
    hidden = document.visibilityState === 'hidden'
    if (!hidden) tick(true)
  }

  function stopTimer() {
    if (pageViews > 0 || chipViews > 0 || !timer) return
    timer()
    timer = null
  }

  // The socket only accelerates polling; on OAuth remotes it is a no-op.
  function openSocket() {
    if (socketOff || typeof ctx.socket !== 'function') return
    try {
      socketOff = ctx.socket('/events', () => { loaders.events(); loaders.status() })
    } catch { socketOff = null }
  }

  function closeSocket() {
    if (pageViews > 0 || !socketOff) return
    try { socketOff() } catch { /* already closed */ }
    socketOff = null
  }

  /** Mount a viewer. kind: 'page' (everything) or 'chip' (status only). Returns release(). */
  function retain(kind) {
    if (kind === 'page') { pageViews += 1; openSocket() } else { chipViews += 1 }
    ensureTimer()
    tick(true)
    let released = false
    return () => {
      if (released) return
      released = true
      if (kind === 'page') pageViews -= 1; else chipViews -= 1
      closeSocket()
      stopTimer()
    }
  }

  // -- actions ---------------------------------------------------------------------------
  const say = (kind, message) => set({ notice: { kind, message } })
  const clearNotice = () => set({ notice: null })

  async function withBusy(key, fn) {
    if (state.busy[key]) return null
    set({ busy: { ...state.busy, [key]: true } })
    try { return await fn() } finally {
      const busy = { ...state.busy }
      delete busy[key]
      set({ busy })
    }
  }

  const replaceRule = rule => set({ rules: state.rules.map(r => (r.id === rule.id ? rule : r)) })

  async function ruleAction(id, verb) {
    return withBusy(`rule:${id}`, async () => {
      try {
        const path = `/rules/${enc(id)}`
        const res = verb === 'delete'
          ? await call(path, { method: 'DELETE' })
          : await call(`${path}/${verb}`, { method: 'POST' })
        if (verb === 'delete') set({ rules: state.rules.filter(r => r.id !== id) })
        else if (res.rule) replaceRule(res.rule)
        say('ok', verb === 'delete' ? 'Rule deleted.' : verb === 'pause' ? 'Rule paused.' : 'Rule resumed.')
        loaders.status()
        return true
      } catch (err) {
        say('error', errMessage(err))
        return false
      }
    })
  }

  async function watch({ text, source, recipe }) {
    const v = validateWatch(text)
    if (!v.ok) { say('error', v.error); return false }
    const body = { text: v.text }
    if (source) body.source = source
    if (recipe) body.recipe = recipe
    return withBusy('watch', async () => {
      try {
        const res = await call('/watch', { method: 'POST', body, timeoutMs: WATCH_TIMEOUT_MS })
        if (res.rule) set({ rules: [res.rule, ...state.rules.filter(r => r.id !== res.rule.id)] })
        say('ok', res.note ? `Watching. ${res.note}` : 'Watching.')
        loaders.status(); loaders.rules(); loaders.sources()
        return true
      } catch (err) {
        say('error', errMessage(err))
        return false
      }
    })
  }

  async function feedback(id, verdict) {
    if (verdict !== 'correct' && verdict !== 'false_alarm') return false
    return withBusy(`fb:${id}`, async () => {
      try {
        const res = await call(`/events/${enc(id)}/feedback`, { method: 'POST', body: { verdict } })
        set({
          events: state.events.map(e => (e.id === id ? { ...e, ...(res.event || {}), feedback: verdict } : e)),
          thresholds: { ...state.thresholds, [id]: res.threshold }
        })
        return true
      } catch (err) {
        say('error', errMessage(err))
        return false
      }
    })
  }

  return {
    getState, subscribe, retain, tick, refreshFrame, loadSnapshot, clearNotice,
    refresh: () => tick(true),
    pause: id => ruleAction(id, 'pause'),
    resume: id => ruleAction(id, 'resume'),
    remove: id => ruleAction(id, 'delete'),
    watch, feedback
  }
}

// ---------------------------------------------------------------------------------------
// 3. UI components
// ---------------------------------------------------------------------------------------

const h = (type, props, ...children) => jsxs(type, { ...props, children })
const el = (type, props) => jsx(type, props || {})

// Plugin layout only; every colour is a theme variable so it follows dark/light/skins.
const CSS = `
.hs-page{display:flex;flex-direction:column;gap:16px;padding:16px;height:100%;overflow-y:auto;color:var(--ui-text-primary);font-size:13px}
.hs-page *{box-sizing:border-box}
.hs-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.hs-grow{flex:1;min-width:0}
.hs-title{font-size:15px;font-weight:600}
.hs-h2{font-size:12px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--ui-text-tertiary);margin:0 0 8px}
.hs-muted{color:var(--ui-text-tertiary)}
.hs-card{border-radius:6px;box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--ui-stroke-secondary) 60%,transparent);padding:10px;min-width:0}
.hs-banner{border-radius:6px;padding:10px;box-shadow:inset 0 0 0 1px var(--ui-accent);background:color-mix(in srgb,var(--ui-accent) 8%,transparent)}
.hs-pre{margin:8px 0;padding:8px;border-radius:4px;background:var(--ui-bg-quaternary);font-family:var(--font-mono,monospace);font-size:12px;white-space:pre;overflow-x:auto}
.hs-meter{height:6px;border-radius:3px;background:var(--ui-bg-quaternary);overflow:hidden}
.hs-meter>span{display:block;height:100%;background:var(--ui-accent)}
.hs-meters{display:flex;gap:16px;flex-wrap:wrap}
.hs-meter-box{min-width:140px;flex:1}
.hs-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px}
.hs-tile{display:flex;flex-direction:column;gap:6px}
.hs-frame{position:relative;aspect-ratio:16/9;border-radius:4px;overflow:hidden;background:var(--ui-bg-quaternary);display:flex;align-items:center;justify-content:center;color:var(--ui-text-tertiary)}
.hs-frame img{width:100%;height:100%;object-fit:cover;display:block}
.hs-frame[data-offline=true] img{opacity:.35}
.hs-offline{position:absolute;top:6px;left:6px}
.hs-list{display:flex;flex-direction:column;gap:8px;list-style:none;margin:0;padding:0}
.hs-rule{display:flex;flex-direction:column;gap:6px}
.hs-text{overflow-wrap:anywhere}
.hs-form{display:flex;flex-direction:column;gap:8px}
.hs-field{display:flex;flex-direction:column;gap:4px;min-width:140px;flex:1}
.hs-field label{font-size:11px;color:var(--ui-text-tertiary)}
.hs-input{width:100%;padding:6px 8px;border-radius:4px;border:0;background:var(--ui-bg-quaternary);color:var(--ui-text-primary);font:inherit;box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--ui-stroke-secondary) 50%,transparent)}
.hs-input:focus-visible,.hs-frame:focus-visible,.hs-page [tabindex]:focus-visible{outline:2px solid var(--ui-accent);outline-offset:1px}
.hs-event{display:flex;gap:10px;align-items:flex-start}
.hs-thumb{width:96px;height:56px;flex-shrink:0;border-radius:4px;object-fit:cover;background:var(--ui-bg-quaternary);display:flex;align-items:center;justify-content:center;color:var(--ui-text-tertiary);font-size:11px}
.hs-conf{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--ui-text-tertiary)}
.hs-conf .hs-meter{width:80px}
.hs-steps{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:4px}
.hs-error{color:var(--ui-text-secondary)}
.hs-notice{padding:6px 10px;border-radius:4px;background:var(--ui-bg-quaternary)}
.hs-notice[data-kind=error]{box-shadow:inset 0 0 0 1px var(--ui-accent)}
.hs-sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
`

function useSentinel(controller) {
  return useSyncExternalStore(controller.subscribe, controller.getState, controller.getState)
}

function useRetain(controller, kind) {
  useEffect(() => controller.retain(kind), [controller, kind])
}

function Meter({ label, used, cap }) {
  const value = pct(used, cap)
  return h('div', { className: 'hs-meter-box' },
    h('div', { className: 'hs-row' },
      el('span', { className: 'hs-grow', children: label }),
      el('span', { className: 'hs-muted', children: `${used ?? 0} / ${cap ?? 0}` })),
    el('div', {
      className: 'hs-meter', role: 'meter', 'aria-label': `${label} vision budget`,
      'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': value,
      children: el('span', { style: { width: `${value}%` } })
    }))
}

function DeliveryBanner({ delivery }) {
  if (!delivery || !delivery.blocked) return null
  return h('div', { className: 'hs-banner', role: 'alert' },
    h('div', { className: 'hs-row' },
      el(Badge, { variant: 'warn', children: 'Alerts blocked' }),
      el('span', { className: 'hs-grow', children: delivery.reason || 'Hermes refused to deliver the last alert.' })),
    el('div', { className: 'hs-muted', children: 'Add this to ~/.hermes/config.yaml and restart Hermes:' }),
    el('pre', { className: 'hs-pre', tabIndex: 0, 'aria-label': 'Config snippet', children: GATEWAY_YAML }),
    el(CopyButton, { text: GATEWAY_YAML, label: 'Copy config snippet', showLabel: true, buttonVariant: 'outline', buttonSize: 'xs' }))
}

function StatusBar({ status, error, loaded }) {
  if (!loaded) return el('div', { className: 'hs-muted', role: 'status', children: 'Loading status...' })
  if (error && !status) return el('div', { className: 'hs-error', role: 'alert', children: `Status unavailable: ${error}` })
  const eng = (status && status.engine) || {}
  const budget = (status && status.budget) || {}
  const state = eng.running ? 'Running' : eng.standby ? 'Standby' : 'Stopped'
  return h('div', { className: 'hs-card hs-row', 'aria-label': 'Sentinel status' },
    el(Badge, { variant: eng.running ? 'success' : 'muted', children: state }),
    eng.since ? el('span', { className: 'hs-muted', children: `since ${relTime(eng.since)}` }) : null,
    el('span', { className: 'hs-muted', children: `${status.sources ?? 0} sources, ${status.rules ?? 0} rules` }),
    budget.paused ? el(Badge, { variant: 'warn', children: 'Budget paused' }) : null,
    el('span', { className: 'hs-grow' }),
    h('div', { className: 'hs-meters' },
      el(Meter, { label: 'This hour', used: budget.hour_used, cap: budget.hour_cap }),
      el(Meter, { label: 'Today', used: budget.day_used, cap: budget.day_cap })),
    status.version ? el('span', { className: 'hs-muted', children: `v${status.version}` }) : null)
}

function SourceTile({ source, frame, now }) {
  const online = !!source.online
  const age = frame && frame.at ? Math.max(0, (now - frame.at) / 1000) : source.last_frame_age_s
  const showImg = frame && frame.src
  return h('li', { className: 'hs-card hs-tile' },
    h('div', { className: 'hs-row' },
      el('strong', { className: 'hs-grow hs-text', children: source.label || source.key }),
      el(Badge, { variant: 'outline', size: 'xs', children: source.kind })),
    h('div', {
      className: 'hs-frame', 'data-offline': String(!online), role: 'img', tabIndex: 0,
      'aria-label': `${source.label || source.key}: ${online ? 'live' : 'offline'}, last frame ${fmtAge(age)}`
    },
      showImg
        ? el('img', { src: frame.src, alt: '', draggable: false })
        : el('span', { children: online ? 'Waiting for frame...' : 'No frame' }),
      online ? null : el(Badge, { className: 'hs-offline', variant: 'destructive', children: 'Offline' })),
    h('div', { className: 'hs-row hs-muted' },
      el('span', { children: `Last seen ${fmtAge(age)}` }),
      el('span', { className: 'hs-grow' }),
      el('span', { children: `${source.rules ?? 0} rule${source.rules === 1 ? '' : 's'}` })),
    source.error ? el('div', { className: 'hs-error', role: 'alert', children: source.error }) : null)
}

function Onboarding({ sources }) {
  return h('div', { className: 'hs-card' },
    el('div', { className: 'hs-title', children: 'Nothing is being watched yet' }),
    h('ol', { className: 'hs-steps' },
      el('li', { children: sources.length ? 'Pick a source below (cameras and screens are detected automatically).' : 'Connect a camera or choose the screen as a source.' }),
      el('li', { children: 'Describe what to watch for in plain English, for example "a package at the front door".' }),
      el('li', { children: 'Sentinel checks frames locally and only asks the vision model when something changes. Confirmed events show up here and in chat.' })))
}

function SourcesSection({ s, controller }) {
  let body
  if (!s.loaded.sources) body = el('div', { className: 'hs-muted', role: 'status', children: 'Loading sources...' })
  else if (s.errors.sources && !s.sources.length) body = el('div', { className: 'hs-error', role: 'alert', children: `Could not load sources: ${s.errors.sources}` })
  else if (!s.sources.length) body = el('div', { className: 'hs-muted', children: 'No sources found. Connect a camera or enable screen capture, then refresh.' })
  else body = el('ul', { className: 'hs-grid hs-list', 'aria-label': 'Sources', children: s.sources.map(src => el(SourceTile, { key: src.key, source: src, frame: s.frames[src.key], now: s.now })) })
  return h('section', { 'aria-label': 'Sources' },
    h('div', { className: 'hs-row' },
      el('h2', { className: 'hs-h2 hs-grow', children: 'Sources' }),
      el(Button, { size: 'xs', variant: 'ghost', onClick: controller.refresh, 'aria-label': 'Refresh now', children: 'Refresh' })),
    body)
}

function RuleRow({ rule, controller, busy }) {
  const [confirming, setConfirming] = useState(false)
  const paused = !!rule.paused
  return h('li', { className: 'hs-card hs-rule' },
    h('div', { className: 'hs-row' },
      el('span', { className: 'hs-grow hs-text', children: rule.text }),
      el(Badge, { variant: 'outline', children: rule.mode }),
      el(Badge, { variant: paused ? 'muted' : 'success', children: paused ? 'paused' : rule.status })),
    h('div', { className: 'hs-row hs-muted' },
      el('span', { children: `Source ${rule.source}` }),
      el('span', { children: `${rule.vision_calls_hour ?? 0} vision calls/h` }),
      el('span', { children: `Last event ${relTime(rule.last_event_at)}` }),
      el('span', { children: `Cooldown ${rule.cooldown_s}s` }),
      rule.hold_s ? el('span', { children: `Hold ${rule.hold_s}s` }) : null),
    h('div', { className: 'hs-row' },
      el(Button, {
        size: 'xs', variant: 'secondary', disabled: busy,
        'aria-label': `${paused ? 'Resume' : 'Pause'} rule: ${rule.text}`,
        onClick: () => (paused ? controller.resume(rule.id) : controller.pause(rule.id)),
        children: paused ? 'Resume' : 'Pause'
      }),
      confirming
        ? h('span', { className: 'hs-row' },
          el(Button, {
            size: 'xs', variant: 'destructive', disabled: busy, 'aria-label': `Confirm delete rule: ${rule.text}`,
            onClick: () => { setConfirming(false); controller.remove(rule.id) }, children: 'Confirm delete'
          }),
          el(Button, { size: 'xs', variant: 'ghost', onClick: () => setConfirming(false), children: 'Cancel' }))
        : el(Button, {
          size: 'xs', variant: 'ghost', disabled: busy, 'aria-label': `Delete rule: ${rule.text}`,
          onClick: () => setConfirming(true), children: 'Delete'
        })))
}

function WatchForm({ sources, controller, busy }) {
  const [text, setText] = useState('')
  const [source, setSource] = useState('')
  const [recipe, setRecipe] = useState('')
  const submit = async ev => {
    if (ev && ev.preventDefault) ev.preventDefault()
    if (await controller.watch({ text, source, recipe })) { setText(''); setRecipe('') }
  }
  return h('form', { className: 'hs-card hs-form', onSubmit: submit, 'aria-label': 'Watch something' },
    h('div', { className: 'hs-field' },
      el('label', { htmlFor: 'hs-watch-text', children: 'Watch for' }),
      el('input', {
        id: 'hs-watch-text', className: 'hs-input', value: text, maxLength: MAX_RULE_TEXT,
        placeholder: 'e.g. tell me when a package is left at the door',
        onChange: e => setText(e.target.value)
      })),
    h('div', { className: 'hs-row' },
      h('div', { className: 'hs-field' },
        el('label', { htmlFor: 'hs-watch-source', children: 'Source' }),
        h('select', { id: 'hs-watch-source', className: 'hs-input', value: source, onChange: e => setSource(e.target.value) },
          el('option', { value: '', children: 'Default source' }),
          ...sources.map(s => el('option', { key: s.key, value: s.key, children: s.label || s.key })))),
      h('div', { className: 'hs-field' },
        el('label', { htmlFor: 'hs-watch-recipe', children: 'Recipe (optional)' }),
        h('select', { id: 'hs-watch-recipe', className: 'hs-input', value: recipe, onChange: e => setRecipe(e.target.value) },
          el('option', { value: '', children: 'None' }),
          ...RECIPES.map(r => el('option', { key: r, value: r, children: r })))),
      el(Button, { type: 'submit', size: 'sm', disabled: busy || !text.trim(), children: busy ? 'Starting...' : 'Watch' })))
}

function RulesSection({ s, controller }) {
  let body
  if (!s.loaded.rules) body = el('div', { className: 'hs-muted', role: 'status', children: 'Loading rules...' })
  else if (s.errors.rules && !s.rules.length) body = el('div', { className: 'hs-error', role: 'alert', children: `Could not load rules: ${s.errors.rules}` })
  else if (!s.rules.length) body = el(Onboarding, { sources: s.sources })
  else body = el('ul', { className: 'hs-list', 'aria-label': 'Rules', children: s.rules.map(r => el(RuleRow, { key: r.id, rule: r, controller, busy: !!s.busy[`rule:${r.id}`] })) })
  return h('section', { 'aria-label': 'Rules' },
    el('h2', { className: 'hs-h2', children: 'Rules' }),
    body,
    el(WatchForm, { sources: s.sources, controller, busy: !!s.busy.watch }))
}

function Confidence({ value }) {
  const v = Math.round(clamp01(value) * 100)
  return h('div', { className: 'hs-conf' },
    el('div', {
      className: 'hs-meter', role: 'meter', 'aria-label': 'Confidence',
      'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': v,
      children: el('span', { style: { width: `${v}%` } })
    }),
    el('span', { children: `${v}%` }))
}

function EventRow({ event, snap, threshold, busy, controller, now }) {
  const fb = event.feedback
  const mark = verdict => controller.feedback(event.id, verdict)
  return h('li', { className: 'hs-card hs-event' },
    snap
      ? el('img', { className: 'hs-thumb', src: snap, alt: `Snapshot for: ${event.caption || event.rule_text}`, draggable: false })
      : el('div', { className: 'hs-thumb', 'aria-hidden': 'true', children: event.has_snapshot ? '...' : 'no image' }),
    h('div', { className: 'hs-grow', style: { display: 'flex', flexDirection: 'column', gap: 4 } },
      h('div', { className: 'hs-row' },
        el('strong', { className: 'hs-grow hs-text', children: event.caption || event.rule_text }),
        el('span', { className: 'hs-muted', title: String(event.ts), children: relTime(event.ts, now) })),
      el('div', { className: 'hs-muted hs-text', children: event.rule_text }),
      el(Confidence, { value: event.confidence }),
      h('div', { className: 'hs-row' },
        el(Button, {
          size: 'xs', variant: fb === 'correct' ? 'default' : 'secondary', disabled: busy, 'aria-pressed': fb === 'correct',
          'aria-label': 'Mark event correct', onClick: () => mark('correct'), children: 'Correct'
        }),
        el(Button, {
          size: 'xs', variant: fb === 'false_alarm' ? 'default' : 'secondary', disabled: busy, 'aria-pressed': fb === 'false_alarm',
          'aria-label': 'Mark event as false alarm', onClick: () => mark('false_alarm'), children: 'False alarm'
        }),
        threshold != null
          ? el('span', { className: 'hs-muted', role: 'status', children: `Threshold now ${Number(threshold).toFixed(2)}` })
          : null)))
}

function EventsSection({ s, controller }) {
  let body
  if (!s.loaded.events) body = el('div', { className: 'hs-muted', role: 'status', children: 'Loading events...' })
  else if (s.errors.events && !s.events.length) body = el('div', { className: 'hs-error', role: 'alert', children: `Could not load events: ${s.errors.events}` })
  else if (!s.events.length) body = el('div', { className: 'hs-muted', children: 'No events yet. When a rule matches, the moment shows up here with a snapshot.' })
  else {
    const sorted = [...s.events].sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts))
    body = el('ul', {
      className: 'hs-list', 'aria-label': 'Events, newest first',
      children: sorted.map(e => el(EventRow, {
        key: e.id, event: e, snap: s.snaps[e.id], threshold: s.thresholds[e.id],
        busy: !!s.busy[`fb:${e.id}`], controller, now: s.now
      }))
    })
  }
  return h('section', { 'aria-label': 'Events' }, el('h2', { className: 'hs-h2', children: 'Events' }), body)
}

export function SentinelPage({ controller }) {
  useRetain(controller, 'page')
  const s = useSentinel(controller)
  return h('div', { className: 'hs-page' },
    el('style', { children: CSS }),
    h('div', { className: 'hs-row' }, el('span', { className: 'hs-title', children: 'Sentinel' })),
    el(StatusBar, { status: s.status, error: s.errors.status, loaded: s.loaded.status }),
    el(DeliveryBanner, { delivery: s.status && s.status.delivery }),
    s.notice
      ? h('div', { className: 'hs-notice hs-row', role: s.notice.kind === 'error' ? 'alert' : 'status', 'data-kind': s.notice.kind },
        el('span', { className: 'hs-grow', children: s.notice.message }),
        el(Button, { size: 'xs', variant: 'ghost', onClick: controller.clearNotice, 'aria-label': 'Dismiss message', children: 'Dismiss' }))
      : null,
    el(SourcesSection, { s, controller }),
    el(RulesSection, { s, controller }),
    el(EventsSection, { s, controller }))
}

export function StatusChip({ controller }) {
  useRetain(controller, 'chip')
  const s = useSentinel(controller)
  const eng = (s.status && s.status.engine) || {}
  const blocked = !!(s.status && s.status.delivery && s.status.delivery.blocked)
  const label = !s.status ? 'Sentinel' : `Sentinel: ${eng.running ? 'on' : eng.standby ? 'standby' : 'off'}${blocked ? ' (alerts blocked)' : ''}`
  return el('button', {
    type: 'button', className: 'px-1.5 text-[0.6875rem]', style: { color: 'var(--ui-text-tertiary)' },
    'aria-label': `${label}. Open Sentinel`,
    onClick: () => { if (host && host.navigate) host.navigate(ROUTE_PATH) },
    children: label
  })
}

// ---------------------------------------------------------------------------------------
// 4. plugin registration
// ---------------------------------------------------------------------------------------

export default {
  id: PLUGIN_ID,
  name: 'Sentinel',
  register(ctx) {
    const controller = createController(ctx)
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, title: 'Sentinel', data: { path: ROUTE_PATH }, render: () => el(SentinelPage, { controller }) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, title: 'Sentinel', data: { path: ROUTE_PATH, label: 'Sentinel', codicon: 'eye' } },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 140, render: () => el(StatusChip, { controller }) }
    ])
  }
}
