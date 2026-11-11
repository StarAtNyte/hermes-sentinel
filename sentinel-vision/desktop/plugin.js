// Sentinel desktop half: the "Sentinel" page (sidebar entry + status-bar chip).
//
// SDK-only (catalog rule 8): imports are '@hermes/plugin-sdk', 'react' and 'react/jsx-runtime'
// only; no JSX (disk plugins load uncompiled); no DOM access outside the React tree; the
// backend is reached through ctx.rest (/api/plugins/sentinel-vision/...).
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

export const PLUGIN_ID = 'sentinel-vision'
export const ROUTE_PATH = '/sentinel'
const API_BASE = `/api/plugins/${PLUGIN_ID}`

export const GATEWAY_YAML = [
  'plugins:',
  '  entries:',
  '    sentinel-vision:',
  '      allow_gateway_injection: true'
].join('\n')

export const RECIPES = [
  'package-at-door', 'pet-on-couch', 'print-failure',
  'training-loss-plateau', 'desk-intruder', 'stove-unattended'
]

export const MAX_RULE_TEXT = 500
export const MAX_SOURCE = 2048
const MAX_EVENTS = 50
const MAX_SNAPSHOT_CACHE = 80
const REST_TIMEOUT_MS = 8000
const WATCH_TIMEOUT_MS = 30000

/** Seconds between refreshes of each resource while the page is open. */
export const DIRECT_RETRY_MS = 60000
const INTERVALS = { status: 2, sources: 3, rules: 4, events: 4, frame: 1 }
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

/** "1 rule", "2 rules". */
export function plural(count, word) {
  const n = Number.isFinite(count) ? count : 0
  return `${n} ${word}${n === 1 ? '' : 's'}`
}

/**
 * Short display label for a source: its label when it has one, otherwise the key with a
 * file path reduced to its basename. `full` is the tooltip text (the whole key, or the
 * parent directory for file sources).
 */
export function sourceName(source) {
  const key = String((source && source.key) || '')
  const label = source && source.label ? String(source.label) : ''
  const raw = label || key
  const isUrl = /^[a-z][a-z0-9+.-]*:\/\//i.test(raw) && !/^file:/i.test(raw)
  const isPath = !isUrl && (/^file:/i.test(raw) || raw.includes('/') || raw.includes('\\'))
  if (!isPath) return { name: raw, full: raw }
  const path = raw.replace(/^file:(\/\/)?/i, '').replace(/[\\/]+$/, '')
  const cut = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'))
  if (cut < 0) return { name: path || raw, full: raw }
  return { name: path.slice(cut + 1) || raw, full: `${path.slice(0, cut) || '/'} (${raw})` }
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
  setup: null,     // GET /setup: { ready, to_fix, summary, checks: [{id,title,status,detail,fix}] }
  sources: [],
  rules: [],
  events: [],
  loaded: { status: false, sources: false, rules: false, events: false, setup: false },
  errors: { status: null, sources: null, rules: null, events: null, setup: null },
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
  // 'data' once the backend answers ?encoding=data_url; 'direct' falls back to <img src> when
  // the reply cannot be parsed as the JSON form (an old backend serving raw jpeg). Direct mode
  // is retried after DIRECT_RETRY_MS, and transport errors alone never switch to it.
  let imageMode = 'unknown'
  let directSince = 0

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
    // Fetched once per page view (and by the Check devices button): probing opens webcams.
    setup: (probe = false) => guarded('setup', async () => {
      try {
        const setup = await call(probe ? '/setup?probe=1' : '/setup', { timeoutMs: WATCH_TIMEOUT_MS })
        set({ setup, loaded: { ...state.loaded, setup: true }, errors: { ...state.errors, setup: null } })
      } catch (err) {
        set({ loaded: { ...state.loaded, setup: true }, errors: { ...state.errors, setup: errMessage(err) } })
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

  const isNotFound = err => /\b404\b|not found/i.test(String(err && err.message))
  // The bridge could not parse the body: the backend answered with something that is not JSON.
  const isParseFailure = err => /json|parse|unexpected token|image\/jpeg/i.test(String(err && err.message))

  async function fetchImage(path) {
    if (imageMode === 'direct' && Date.now() - directSince > DIRECT_RETRY_MS) imageMode = 'unknown'
    if (imageMode === 'direct') return { src: null, direct: true }
    try {
      const sep = path.includes('?') ? '&' : '?'
      const res = await ctx.rest(`${path}${sep}encoding=data_url`, { timeoutMs: REST_TIMEOUT_MS })
      if (res && typeof res.data_url === 'string' && res.data_url.startsWith('data:image/')) {
        imageMode = 'data'
        return { src: res.data_url, missing: false }
      }
      if (res && res.ok === false) return { src: null, missing: true }
    } catch (err) {
      if (isNotFound(err)) return { src: null, missing: true }
      if (!isParseFailure(err)) {
        // Transport / server failure: keep the current mode and try again on the next tick.
        return { src: null, missing: true }
      }
    }
    imageMode = 'direct'
    directSince = Date.now()
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
      if (!state.loaded.setup && due('setup', 30, force)) loaders.setup(false)
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

  /** Mount a viewer. kind: 'page' (everything) or 'chip' (status only). Returns release(). */
  function retain(kind) {
    if (kind === 'page') pageViews += 1; else chipViews += 1
    ensureTimer()
    tick(true)
    let released = false
    return () => {
      if (released) return
      released = true
      if (kind === 'page') pageViews -= 1; else chipViews -= 1
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
    if (source && source.trim()) body.source = source.trim()
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

  async function checkSetup(probe = true) {
    return withBusy('setup', async () => {
      await loaders.setup(probe)
      return !state.errors.setup
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
    watch, feedback, checkSetup
  }
}

// ---------------------------------------------------------------------------------------
// 3. UI components
// ---------------------------------------------------------------------------------------

// React wants `key` as the third jsx() argument, never inside the spread props object.
const h = (type, props, ...children) => {
  const { key, ...rest } = props || {}
  return jsxs(type, { ...rest, children }, key)
}
const el = (type, props) => {
  const { key, ...rest } = props || {}
  return jsx(type, rest, key)
}

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
.hs-tile{display:flex;flex-direction:column;gap:6px}
.hs-frame{position:relative;aspect-ratio:16/9;border-radius:4px;overflow:hidden;background:var(--ui-bg-quaternary);display:flex;align-items:center;justify-content:center;color:var(--ui-text-tertiary)}
.hs-frame img{width:100%;height:100%;object-fit:cover;display:block}
.hs-frame[data-offline=true] img{opacity:.35}
.hs-offline{position:absolute;top:6px;left:6px}
.hs-list{display:flex;flex-direction:column;gap:8px;list-style:none;margin:0;padding:0}
.hs-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,340px));gap:10px}
.hs-rule{display:flex;flex-direction:column;gap:6px}
.hs-text{overflow-wrap:anywhere}
.hs-clip{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
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
.hs-fix{margin-top:2px;white-space:pre-wrap;overflow-wrap:anywhere;color:var(--ui-text-secondary)}
.hs-fix-code{margin:4px 0 0}
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

/** Checks that are not ok, worst first. Pure (exported for tests). */
export function setupIssues(setup) {
  const checks = setup && Array.isArray(setup.checks) ? setup.checks : []
  const rank = { fail: 0, warn: 1 }
  return checks.filter(c => c && (c.status === 'fail' || c.status === 'warn'))
    .sort((a, b) => rank[a.status] - rank[b.status])
}

function fixBlock(fix) {
  if (!String(fix).includes('\n')) return el('div', { className: 'hs-fix', children: fix })
  return el('pre', { className: 'hs-pre hs-fix-code', tabIndex: 0, 'aria-label': 'Suggested fix', children: String(fix) })
}

function SetupCard({ setup, busy, controller }) {
  const issues = setupIssues(setup)
  if (!issues.length) return null
  return h('section', { className: 'hs-card hs-form', 'aria-label': 'Setup' },
    h('div', { className: 'hs-row' },
      el('h2', { className: 'hs-h2 hs-grow', style: { margin: 0 }, children: 'Setup' }),
      el(Button, {
        size: 'xs', variant: 'ghost', disabled: busy, onClick: () => controller.checkSetup(true),
        'aria-label': 'Check cameras and screens now', children: busy ? 'Checking...' : 'Check devices'
      })),
    el('div', { className: 'hs-muted', role: 'status', children: setup.summary || '' }),
    el('ul', {
      className: 'hs-list', 'aria-label': 'Setup checks',
      children: issues.map(c => h('li', { key: c.id, className: 'hs-text' },
        h('div', { className: 'hs-row' },
          el(Badge, { variant: c.status === 'fail' ? 'destructive' : 'warn', size: 'xs', children: c.status === 'fail' ? 'Fix' : 'Review' }),
          el('strong', { children: c.title }),
          el('span', { className: 'hs-muted', children: c.detail })),
        c.fix ? fixBlock(c.fix) : null))
    }),
    el('div', { className: 'hs-muted', children: 'The same checklist is available as /sentinel setup in chat.' }))
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
    el('span', { className: 'hs-muted', children: `${plural(status.sources, 'source')}, ${plural(status.rules, 'rule')}` }),
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
  const { name, full } = sourceName(source)
  return h('li', { className: 'hs-card hs-tile' },
    h('div', { className: 'hs-row' },
      el('strong', { className: 'hs-grow hs-clip', title: full, children: name }),
      el(Badge, { variant: 'outline', size: 'xs', children: source.kind })),
    h('div', {
      className: 'hs-frame', 'data-offline': String(!online), role: 'img', tabIndex: 0,
      'aria-label': `${name}: ${online ? 'live' : 'offline'}, last frame ${fmtAge(age)}`
    },
      showImg
        ? el('img', { src: frame.src, alt: '', draggable: false })
        : el('span', { children: online ? 'Waiting for frame...' : 'No frame' }),
      online ? null : el(Badge, { className: 'hs-offline', variant: 'destructive', children: 'Offline' })),
    h('div', { className: 'hs-row hs-muted' },
      el('span', { children: `Last seen ${fmtAge(age)}` }),
      el('span', { className: 'hs-grow' }),
      el('span', { children: plural(source.rules, 'rule') })),
    source.error ? el('div', { className: 'hs-error', role: 'alert', children: source.error }) : null)
}

function Onboarding({ sources }) {
  return h('div', { className: 'hs-card' },
    el('div', { className: 'hs-title', children: 'Nothing is being watched yet' }),
    h('ol', { className: 'hs-steps' },
      el('li', { children: sources.length ? 'Sources appear here once a rule uses them. Name one in the Source field below, such as camera:0 or screen.' : 'Name a source in the Source field below, such as camera:0 or screen, or leave it empty to use the default.' }),
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

const shortStatus = status => typeof status === 'string' && status.length > 0 && status.length <= 24

function RuleRow({ rule, controller, busy }) {
  const [confirming, setConfirming] = useState(false)
  const paused = !!rule.paused
  const source = sourceName({ key: rule.source })
  return h('li', { className: 'hs-card hs-rule' },
    h('div', { className: 'hs-row' },
      el('span', { className: 'hs-grow hs-text', children: rule.text }),
      el(Badge, { variant: 'outline', children: rule.mode }),
      paused || shortStatus(rule.status)
        ? el(Badge, { variant: !paused && rule.status === 'watching' ? 'success' : 'muted', children: paused ? 'paused' : rule.status })
        : null),
    !paused && !shortStatus(rule.status)
      ? el('div', { className: 'hs-muted hs-text', children: String(rule.status) }) : null,
    h('div', { className: 'hs-row hs-muted' },
      el('span', { title: source.full, children: `Source ${source.name}` }),
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
  // Suggestions are never the masked keys the API returns (those cannot be used as a source).
  const suggestions = [...new Set(['camera:0', 'screen', ...sources
    .filter(s => s && typeof s.key === 'string' && !s.key.includes('***') && !s.key.includes('%2A'))
    .map(s => s.key)])].slice(0, 12)
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
        el('input', {
          id: 'hs-watch-source', className: 'hs-input', value: source, maxLength: MAX_SOURCE,
          list: suggestions.length ? 'hs-watch-source-list' : undefined, autoComplete: 'off',
          placeholder: 'default source (e.g. camera:0, screen)',
          onChange: e => setSource(e.target.value)
        }),
        suggestions.length
          ? h('datalist', { id: 'hs-watch-source-list' },
            ...suggestions.map(v => el('option', { key: v, value: v }))) : null),
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
    el(SetupCard, { setup: s.setup, busy: !!s.busy.setup, controller }),
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
