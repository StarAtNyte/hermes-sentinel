// Mounts the plugin's registered route/nav/status contributions in a stub host.
import { createRoot } from 'react-dom/client'
import { createElement as h } from 'react'
import plugin from '../../sentinel-vision/desktop/plugin.js'

const API = '/api/plugins/sentinel-vision'
const contributions = []
const stops = []

async function rest(path, opts = {}) {
  const init = { method: opts.method || 'GET', headers: {} }
  if (opts.body !== undefined) {
    init.body = JSON.stringify(opts.body)
    init.headers['content-type'] = 'application/json'
  }
  const response = await fetch(API + path, init)
  const body = await response.text()
  if (!response.ok) throw new Error(`${response.status}: ${body}`)
  return JSON.parse(body)
}

const ctx = {
  rest,
  socket: () => () => {},
  registerMany: (items) => contributions.push(...items),
  register: (item) => contributions.push(item),
  addEventListener: (target, name, fn) => { target.addEventListener(name, fn); return () => target.removeEventListener(name, fn) },
  setInterval: (fn, ms) => { const id = window.setInterval(fn, ms); stops.push(id); return () => window.clearInterval(id) }
}
plugin.register(ctx)

const pick = (area) => contributions.filter((item) => item.area === area)
const [page] = pick('routes')
const status = pick('statusBar.right')
const nav = pick('nav')
const bar = h('div', { className: 'host-bar' },
  h('span', { className: 'host-nav' }, nav.map((n) => n.data.label).join(' ')),
  status.map((s) => h('span', { key: s.id }, s.render())))
createRoot(document.getElementById('root')).render(
  h('div', { className: 'host' }, h('main', { className: 'host-main' }, page.render()), bar))
