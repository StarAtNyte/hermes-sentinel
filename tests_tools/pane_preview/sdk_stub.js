// Stand-in for @hermes/plugin-sdk: same export names and prop shapes as the real Badge, Button
// and CopyButton, with plain CSS in place of the app's Tailwind theme. Not the real host.
import { createElement as h, useState } from 'react'

export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'nav'
export const STATUSBAR_AREAS = { right: 'statusBar.right' }
export const host = { navigate: (path) => { window.__navigated = path } }

export function Badge({ variant = 'default', size, className = '', ...props }) {
  return h('span', { 'data-slot': 'badge', className: `sdk-badge sdk-badge-${variant} ${className}`, ...props })
}

export function Button({ variant = 'default', size = 'default', className = '', type = 'button', ...props }) {
  return h('button', { 'data-slot': 'button', type, className: `sdk-btn sdk-btn-${variant} sdk-size-${size} ${className}`, ...props })
}

export function CopyButton({ text, label = 'Copy', ...props }) {
  const [done, setDone] = useState(false)
  const onClick = () => {
    navigator.clipboard?.writeText(text).catch(() => {})
    setDone(true)
    setTimeout(() => setDone(false), 1200)
  }
  return h(Button, { variant: 'ghost', size: 'xs', onClick, ...props }, done ? 'Copied' : label)
}
