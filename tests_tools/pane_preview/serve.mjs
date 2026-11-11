// Preview server: bundles the pane against the SDK stub, serves it, and proxies
// /api/plugins/sentinel-vision/* to a live backend with the session token added server-side.
// Usage: node serve.mjs --token-file <path> [--backend http://127.0.0.1:9219] [--port 8765]
import { createServer, request } from 'node:http'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'

const here = import.meta.dirname
const args = Object.fromEntries(process.argv.slice(2).reduce((acc, v, i, all) => (v.startsWith('--') ? [...acc, [v.slice(2), all[i + 1]]] : acc), []))
const backend = new URL(args.backend || 'http://127.0.0.1:9219')
const port = Number(args.port || 8765)
const token = readFileSync(args['token-file'], 'utf8').trim()
const hermesModules = path.join(process.env.HOME, '.hermes/hermes-agent/node_modules')
const esbuild = createRequire(path.join(hermesModules, 'x.js'))('esbuild')

const built = await esbuild.build({
  entryPoints: [path.join(here, 'harness.js')], bundle: true, write: false, format: 'iife',
  nodePaths: [hermesModules], alias: { '@hermes/plugin-sdk': path.join(here, 'sdk_stub.js') },
  define: { 'process.env.NODE_ENV': '"development"' }, jsx: 'automatic'
})
const bundle = built.outputFiles[0].text
const files = { '/': ['index.html', 'text/html'], '/host.css': ['host.css', 'text/css'] }

function proxy(req, res) {
  const upstream = request({
    host: backend.hostname, port: backend.port, path: req.url, method: req.method,
    headers: { ...req.headers, host: backend.host, 'x-hermes-session-token': token }
  }, (reply) => { res.writeHead(reply.statusCode, reply.headers); reply.pipe(res) })
  upstream.on('error', (error) => { res.writeHead(502); res.end(String(error)) })
  req.pipe(upstream)
}

createServer((req, res) => {
  const route = req.url.split('?')[0]
  if (route.startsWith('/api/plugins/sentinel-vision/')) return proxy(req, res)
  if (route === '/bundle.js') { res.writeHead(200, { 'content-type': 'text/javascript' }); return res.end(bundle) }
  const entry = files[route]
  if (!entry) { res.writeHead(404); return res.end('not found') }
  res.writeHead(200, { 'content-type': entry[1] })
  return res.end(readFileSync(path.join(here, entry[0])))
}).listen(port, '127.0.0.1', () => console.log(`pane preview on http://127.0.0.1:${port}`))
