// Node-only test for hermes-sentinel/desktop/plugin.js with a stub SDK and a stub React.
// Run: node tests_tools/plugin_js_check.mjs   (exit 0 = pass). Driven by test_plugin_js.py.
import { register } from 'node:module'
import { pathToFileURL } from 'node:url'
import assert from 'node:assert/strict'
import path from 'node:path'

const STUBS = {
  '@hermes/plugin-sdk': `
    const C = n => ({ __c: n })
    export const Badge = C('Badge'), Button = C('Button'), CopyButton = C('CopyButton')
    export const ROUTES_AREA = 'routes', SIDEBAR_NAV_AREA = 'nav'
    export const STATUSBAR_AREAS = { right: 'statusBar.right' }
    export const host = { navigate: p => { globalThis.__nav = p } }`,
  react: `
    export const useState = i => [typeof i === 'function' ? i() : i, () => {}]
    export const useEffect = () => {}
    export const useSyncExternalStore = (sub, get) => get()`,
  'react/jsx-runtime': `
    export const jsx = (type, props) => ({ type, props })
    export const jsxs = jsx`
}
const hooks = `
const S = ${JSON.stringify(STUBS)}
export async function resolve(spec, ctx, next) {
  if (spec in S) return { url: 'stub:' + spec, shortCircuit: true }
  return next(spec, ctx)
}
export async function load(url, ctx, next) {
  if (url.startsWith('stub:')) return { format: 'module', source: S[url.slice(5)], shortCircuit: true }
  return next(url, ctx)
}`
register('data:text/javascript,' + encodeURIComponent(hooks))

globalThis.document = { visibilityState: 'visible' }
const file = path.resolve(import.meta.dirname, '../hermes-sentinel/desktop/plugin.js')
const m = await import(pathToFileURL(file).href)

// ---- render helper: expand function components, collect text -------------------------
function expand(node) {
  if (node == null || typeof node === 'boolean') return null
  if (typeof node === 'string' || typeof node === 'number') return node
  if (Array.isArray(node)) return node.map(expand)
  if (typeof node.type === 'function') return expand(node.type(node.props))
  const { children, ...props } = node.props || {}
  return { type: node.type.__c || node.type, props, children: expand(children) }
}
const text = n => (n == null ? '' : typeof n !== 'object' ? String(n) : Array.isArray(n) ? n.map(text).join(' ') : text(n.children))
const find = (n, pred, out = []) => {
  if (n == null || typeof n !== 'object') return out
  if (Array.isArray(n)) { n.forEach(c => find(c, pred, out)); return out }
  if (pred(n)) out.push(n)
  find(n.children, pred, out)
  return out
}

// ---- fake ctx ----------------------------------------------------------------------------
function makeCtx(routes) {
  const calls = []
  const intervals = []
  const ctx = {
    calls, intervals, registered: [], socketOpened: 0,
    rest: async (p, o = {}) => {
      calls.push({ p, o })
      const key = (o.method || 'GET') + ' ' + p.split('?')[0]
      const h = routes[key] ?? routes[p]
      if (!h) throw new Error('404: {"ok":false,"error":"not found"}')
      return typeof h === 'function' ? h(o, p) : h
    },
    socket: () => { ctx.socketOpened += 1; return () => {} },
    setInterval: (fn, ms) => { const it = { fn, ms, off: false }; intervals.push(it); return () => { it.off = true } },
    addEventListener: () => () => {},
    registerMany: cs => ctx.registered.push(...cs),
    register: c => ctx.registered.push(c)
  }
  return ctx
}
const settle = () => new Promise(r => setTimeout(r, 20))

const SOURCES = [
  { key: 'camera:0', label: 'Door cam', kind: 'camera', online: true, last_frame_age_s: 1, rules: 1, error: null },
  { key: 'screen', label: 'Screen', kind: 'screen', online: false, last_frame_age_s: null, rules: 0, error: 'no display' }
]
const RULE = { id: 'r1', text: 'package at door', source: 'camera:0', mode: 'trigger', status: 'watching', paused: false, last_event_at: null, vision_calls_hour: 3, cooldown_s: 60, hold_s: 0 }
const EVENTS = [
  { id: 'e1', rule_id: 'r1', rule_text: 'package at door', ts: '2026-11-06T10:00:00Z', caption: 'old', confidence: 0.5, feedback: null, has_snapshot: true },
  { id: 'e2', rule_id: 'r1', rule_text: 'package at door', ts: '2026-11-06T11:00:00Z', caption: 'newest', confidence: 0.9, feedback: null, has_snapshot: false }
]
const STATUS = {
  engine: { running: true, standby: false, since: '2026-11-06T09:00:00Z' },
  delivery: { blocked: true, reason: 'injection refused' },
  budget: { hour_used: 5, hour_cap: 20, day_used: 10, day_cap: 100, paused: false },
  sources: 2, rules: 1, version: '0.1.0'
}
const SETUP = {
  ok: true, ready: false, to_fix: 1, to_review: 1, probed: false,
  summary: 'Ready: no - 1 thing to fix (1 to review)',
  checks: [
    { id: 'deps', title: 'Python packages', status: 'ok', detail: 'all importable', fix: '' },
    { id: 'vision', title: 'Vision model', status: 'warn', detail: 'uses your default model; it must accept images', fix: 'set auxiliary.sentinel_vision' },
    { id: 'sources', title: 'Cameras and screens', status: 'fail', detail: 'none found', fix: 'Connect a webcam' }
  ]
}
const base = () => ({
  'GET /setup': (o, p) => (p.includes('probe=1') ? { ...SETUP, probed: true } : SETUP),
  'GET /status': STATUS, 'GET /sources': SOURCES, 'GET /rules': [RULE], 'GET /events': EVENTS,
  'GET /frame/camera%3A0': { ok: true, data_url: 'data:image/jpeg;base64,AAAA' },
  'GET /events/e1/snapshot': { ok: true, data_url: 'data:image/jpeg;base64,BBBB' },
  'POST /events/e1/feedback': (o) => ({ ok: true, event: { ...EVENTS[0], feedback: o.body.verdict }, threshold: 0.73 }),
  'POST /rules/r1/pause': { ok: true, rule: { ...RULE, paused: true } },
  'DELETE /rules/r1': { ok: true, rule: RULE },
  'POST /watch': (o) => ({ ok: true, rule: { ...RULE, id: 'r2', text: o.body.text }, note: 'using default camera' })
})

// 1. helpers
assert.equal(m.fmtAge(null), 'never')
assert.equal(m.fmtAge(2), 'just now')
assert.equal(m.fmtAge(125), '2m ago')
assert.equal(m.pct(5, 20), 25)
assert.equal(m.pct(5, 0), 0)
assert.equal(m.pct(50, 20), 100)
assert.equal(m.errMessage(new Error('400: {"ok":false,"error":"bad id"}')), 'bad id')
assert.equal(m.validateWatch('   ').ok, false)
assert.equal(m.validateWatch('x'.repeat(501)).ok, false)
assert.equal(m.validateWatch(' hi ').text, 'hi')

// 2. registration
{
  const ctx = makeCtx(base())
  m.default.register(ctx)
  assert.equal(m.default.id, 'hermes-sentinel')
  const areas = ctx.registered.map(c => c.area).sort()
  assert.deepEqual(areas, ['nav', 'routes', 'statusBar.right'])
}

// 3. controller: polling, frames, snapshots, feedback, actions
{
  const ctx = makeCtx(base())
  const c = m.createController(ctx)
  const release = c.retain('page')
  await settle()
  let s = c.getState()
  assert.equal(s.status.version, '0.1.0')
  assert.equal(s.sources.length, 2)
  assert.equal(s.rules.length, 1)
  assert.equal(s.events.length, 2)
  assert.equal(ctx.intervals.length, 1)
  assert.equal(ctx.intervals[0].ms, 1000)
  c.tick(true)
  await settle()
  s = c.getState()
  // only the online source is fetched; cache-free data_url mode
  assert.ok(ctx.calls.some(x => x.p.startsWith('/frame/camera%3A0?encoding=data_url')))
  assert.ok(!ctx.calls.some(x => x.p.startsWith('/frame/screen')))
  assert.equal(s.frames['camera:0'].src, 'data:image/jpeg;base64,AAAA')
  assert.equal(s.snaps.e1, 'data:image/jpeg;base64,BBBB')
  assert.equal(s.snaps.e2, undefined) // no snapshot flagged

  // page render: banner with exact YAML, newest event first
  const tree = expand(m.SentinelPage({ controller: c }))
  const flat = text(tree)
  assert.match(flat, /Alerts blocked/)
  assert.ok(find(tree, n => n.type === 'pre' && text(n) === m.GATEWAY_YAML).length === 1)
  assert.ok(find(tree, n => n.type === 'CopyButton' && n.props.text === m.GATEWAY_YAML).length === 1)
  assert.ok(flat.indexOf('newest') < flat.search(/\bold\b/))
  assert.ok(find(tree, n => n.type === 'img' && n.props.src === 'data:image/jpeg;base64,BBBB').length === 1)
  assert.match(flat, /Offline/)
  assert.match(flat, /25%|5 \/ 20/)
  assert.ok(find(tree, n => n.props && n.props['aria-label'] === 'Mark event correct').length === 2)

  // feedback
  assert.equal(await c.feedback('e1', 'false_alarm'), true)
  s = c.getState()
  assert.equal(s.thresholds.e1, 0.73)
  assert.equal(s.events.find(e => e.id === 'e1').feedback, 'false_alarm')
  assert.equal(await c.feedback('e1', 'bogus'), false)

  // pause / delete
  await c.pause('r1')
  assert.equal(c.getState().rules[0].paused, true)
  await c.remove('r1')
  assert.equal(c.getState().rules.length, 0)
  assert.equal(c.getState().errors.rules, null)

  // watch: validation blocks empty, success prepends the rule, body carries optional fields
  assert.equal(await c.watch({ text: '  ' }), false)
  assert.equal(c.getState().notice.kind, 'error')
  assert.equal(await c.watch({ text: 'cat on couch', source: 'camera:0', recipe: 'pet-on-couch' }), true)
  const post = ctx.calls.filter(x => x.p === '/watch').at(-1)
  assert.deepEqual(post.o.body, { text: 'cat on couch', source: 'camera:0', recipe: 'pet-on-couch' })
  assert.match(c.getState().notice.message, /default camera/)

  release()
  assert.equal(ctx.intervals[0].off, true)
  assert.equal(ctx.socketOpened, 0) // the backend serves no websocket: polling only
}

// 4. engine down / errors degrade gracefully
{
  const ctx = makeCtx({})
  const c = m.createController(ctx)
  c.retain('page')
  await settle()
  const s = c.getState()
  assert.equal(s.loaded.status, true)
  assert.equal(s.errors.status, 'not found')
  assert.deepEqual(s.sources, [])
  // error-only state still renders (no throw) and shows the error
  assert.match(text(expand(m.SentinelPage({ controller: c }))), /Status unavailable: not found/)
}

// 5. image fallback: only a parse failure means 'direct'; 404 stays missing; transport errors retry
{
  const r = base()
  r['GET /frame/camera%3A0'] = () => { throw new Error('Expected JSON but got image/jpeg') }
  const ctx = makeCtx(r)
  const c = m.createController(ctx)
  c.retain('page')
  await settle()
  c.tick(true)
  await settle()
  assert.match(c.getState().frames['camera:0'].src, /^\/api\/plugins\/hermes-sentinel\/frame\/camera%3A0\?t=\d+$/)
}
{
  // 404 -> missing, never direct
  const r = base()
  r['GET /frame/camera%3A0'] = () => { throw new Error('404: {"ok":false,"error":"no frame available"}') }
  const c = m.createController(makeCtx(r))
  c.retain('page'); await settle(); c.tick(true); await settle()
  assert.equal(c.getState().frames['camera:0'].missing, true)
  assert.equal(c.getState().frames['camera:0'].src, null)
}
{
  // one transient transport error does not latch direct mode; the next tick recovers
  const r = base()
  let n = 0
  r['GET /frame/camera%3A0'] = () => {
    n += 1
    if (n === 1) throw new Error('fetch failed: ECONNRESET')
    return { ok: true, data_url: 'data:image/jpeg;base64,CCCC' }
  }
  const c = m.createController(makeCtx(r))
  c.retain('page'); await settle(); c.tick(true); await settle()
  assert.equal(c.getState().frames['camera:0'].src, null)
  assert.equal(c.getState().frames['camera:0'].missing, true)
  c.tick(true); await settle()
  assert.equal(c.getState().frames['camera:0'].src, 'data:image/jpeg;base64,CCCC')
}

// 5b. long rule status renders in a wrapping span, short state stays a Badge; free-text source
{
  const r = base()
  const long = 'source offline: ' + 'x'.repeat(80)
  r['GET /rules'] = [{ ...RULE, status: long }]
  const c = m.createController(makeCtx(r))
  c.retain('page'); await settle()
  const tree = expand(m.SentinelPage({ controller: c }))
  assert.equal(find(tree, n => n.type === 'Badge' && String(n.children).includes('source offline')).length, 0)
  assert.equal(find(tree, n => n.type === 'div' && text(n) === long).length, 1)
  const form = find(tree, n => n.type === 'form')[0]
  assert.equal(find(form, n => n.type === 'select' && n.props.id === 'hs-watch-source').length, 0)
  const input = find(form, n => n.type === 'input' && n.props.id === 'hs-watch-source')[0]
  assert.match(input.props.placeholder, /camera:0/)
  const opts = find(form, n => n.type === 'datalist').flatMap(d => d.children).map(o => o.props.value)
  assert.ok(opts.length > 0 && opts.every(v => !v.includes('***') && !v.includes('%2A')))
  assert.doesNotMatch(text(tree), /detected automatically/)
}

// 6. chip-only viewer polls status only
{
  const ctx = makeCtx(base())
  const c = m.createController(ctx)
  c.retain('chip')
  await settle()
  assert.ok(ctx.calls.every(x => x.p === '/status'))
}

// 7. empty onboarding
{
  const r = base()
  r['GET /rules'] = []; r['GET /events'] = []; r['GET /sources'] = []
  const c = m.createController(makeCtx(r))
  c.retain('page'); await settle()
  const flat = text(expand(m.SentinelPage({ controller: c })))
  assert.match(flat, /Nothing is being watched yet/)
  assert.match(flat, /No events yet/)
}

// 8. setup card: fetched once without probing, worst first, hidden when everything is ok,
//    Check devices probes; a failing /setup never breaks the page
{
  const ctx = makeCtx(base())
  const c = m.createController(ctx)
  c.retain('page'); await settle()
  assert.equal(ctx.calls.filter(x => x.p === '/setup').length, 1)
  c.tick(true); await settle()
  assert.equal(ctx.calls.filter(x => x.p.startsWith('/setup')).length, 1) // not polled
  assert.deepEqual(m.setupIssues(c.getState().setup).map(i => i.id), ['sources', 'vision'])
  let tree = expand(m.SentinelPage({ controller: c }))
  const card = find(tree, n => n.type === 'section' && n.props['aria-label'] === 'Setup')[0]
  assert.ok(card)
  const flat = text(card)
  assert.match(flat, /Ready: no - 1 thing to fix/)
  assert.match(flat, /Connect a webcam/)
  assert.doesNotMatch(flat, /Python packages/) // ok checks are not listed
  assert.ok(flat.indexOf('Cameras and screens') < flat.indexOf('Vision model'))
  const btn = find(card, n => n.type === 'Button')[0]
  assert.equal(btn.props['aria-label'], 'Check cameras and screens now')
  assert.equal(await c.checkSetup(true), true)
  assert.ok(ctx.calls.some(x => x.p === '/setup?probe=1'))
  assert.equal(c.getState().setup.probed, true)
}
{
  const r = base()
  r['GET /setup'] = { ...SETUP, checks: SETUP.checks.map(k => ({ ...k, status: 'ok' })) }
  const c = m.createController(makeCtx(r))
  c.retain('page'); await settle()
  const tree = expand(m.SentinelPage({ controller: c }))
  assert.equal(find(tree, n => n.type === 'section' && n.props['aria-label'] === 'Setup').length, 0)
}
{
  const r = base()
  delete r['GET /setup'] // 404: no card, page still renders
  const c = m.createController(makeCtx(r))
  c.retain('page'); await settle()
  assert.equal(c.getState().errors.setup, 'not found')
  const tree = expand(m.SentinelPage({ controller: c }))
  assert.equal(find(tree, n => n.type === 'section' && n.props['aria-label'] === 'Setup').length, 0)
  assert.match(text(tree), /Sources/)
}

console.log('plugin.js checks passed')
