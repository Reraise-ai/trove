// Render smoke test for plugin/desktop/plugin.js (optional: needs node plus react@18, react-dom@18, react-test-renderer@18).
// Usage: NODE_PATH-free; run  node tools/ui_smoke.mjs <dir-with-node_modules>   (default: ./node_modules)
// Stubs @hermes/plugin-sdk and a fake backend, renders the page with the real plugin.js in English and German and checks the output.
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'

const nm = path.resolve(process.argv[2] || 'node_modules')
const require = createRequire(path.join(nm, 'x.js'))
const React = require('react'); const TR = require('react-test-renderer'); const jsxRuntime = require('react/jsx-runtime')
const here = path.dirname(new URL(import.meta.url).pathname)
let src = fs.readFileSync(path.join(here, '..', 'plugin', 'desktop', 'plugin.js'), 'utf8')
src = src.replace(/^import .*$/gm, '')   // imports come from globalThis.__T
src = "const { host, Button, Badge, Input, SandboxedFrame, ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA, usePluginI18n, useI18n, useState, useEffect, useCallback, useRef, jsx, jsxs } = globalThis.__T\n" + src
src += "\nexport { TrovePage, Side, IntoKnowledge, ResultPanel, Preview, AddSource, Tasks, Search, Integrations, EN, DE }\n"

let locale = 'en'; const bundles = {}; const notes = []; let storageData = {}
const tr = (loc, key, args) => { const v = (bundles[loc] || {})[key] ?? (bundles.en || {})[key] ?? key; return typeof v === 'function' ? v(...args) : v }
const calls = []
const fake = {
  '/status': { ok: true, mandant: 'haus', profile: ['default', 'helper'], protokolle: [{ mandant: 'haus', zeilen: 3 }] },
  '/quellen': { quellen: [{ id: 'a1', art: 'ordner', name: 'Inbox', pfad: '/tmp/inbox', nur_lesen: true }, { id: 'b2', art: 'ordner', name: 'Archive', pfad: '/tmp/archive', nur_lesen: false }] },
  '/liste': { quelle: 'a1', pfad: '', eintraege: [{ name: 'a.pdf', ordner: false, groesse: 2048, mtime: 1700000000, pfad: '/tmp/inbox/a.pdf', dublette: true, fundorte: 2 }, { name: 'sub', ordner: true, groesse: 0, mtime: 1700000000, pfad: '/tmp/inbox/sub' }], indexiert: false },
  '/auftraege': { auftraege: [{ id: 't1', status: 'blocked', titel: 'Check' }] },
  '/integrationen': { settings: '/settings?tab=plugins&plugin=reraise-trove', mail: { mailboxes: [{ id: 'imap', kind: 'imap', active: false, missing: ['imap_host'] }, { id: 'gmail', kind: 'gmail', active: false, missing: ['TROVE_GMAIL_CLIENT_ID'], signin: 'hermes auth add reraise-trove:gmail' }] },
    docuseal: { active: false, missing: ['docuseal_url'] }, erpnext: { active: false, missing: ['erpnext_url'] }, reraise: { active: false, link: 'https://reraise.ai' } },
}
const opened = []
globalThis.__T = {
  host: { notify: (n) => notes.push(n), navigate() {}, restartGateway() {} },
  Button: (p) => React.createElement('button', { disabled: p.disabled, title: p.title, onClick: p.onClick }, p.children),
  Badge: (p) => React.createElement('span', null, p.children), Input: (p) => React.createElement('input', { placeholder: p.placeholder, value: p.value, onChange: p.onChange }),
  SandboxedFrame: () => null, ROUTES_AREA: 'routes', SIDEBAR_NAV_AREA: 'nav', PALETTE_AREA: 'palette',
  usePluginI18n: () => (k, ...a) => tr(locale, k, a), useI18n: () => ({ locale }),
  useState: React.useState, useEffect: React.useEffect, useCallback: React.useCallback, useRef: React.useRef, jsx: jsxRuntime.jsx, jsxs: jsxRuntime.jsxs,
}
const file = path.join(here, '..', '.ui_smoke_tmp.mjs'); fs.writeFileSync(file, src)
const mod = await import(pathToFileURL(file).href); fs.unlinkSync(file)
let failures = 0
const ok = (c, m) => { if (!c) { failures++; console.log('FAIL', m) } else console.log('ok  ', m) }
const settle = () => new Promise((r) => setTimeout(r, 30))
const text = (tree) => JSON.stringify(tree.toJSON())
async function render(el) { let t; await TR.act(async () => { t = TR.create(el); await settle() }); return t }

const ctx = {
  rest: async (p, o = {}) => { const k = p.split('?')[0]; calls.push([k, o.method || 'GET', o.body]); if (fake[k] && !o.method) return fake[k]; return { ergebnis: [], lauf: 'r1' } },
  storage: { get: (k, d) => (k in storageData ? storageData[k] : d), set: (k, v) => { storageData[k] = v } },
  files: { requestFolder: async () => ({ ok: true, folder: { id: 'f', name: 'Picked', path: '/tmp/picked' } }) },
  i18n: { register: (b) => { Object.assign(bundles, b) }, t: (k, ...a) => tr(locale, k, a) },
  registerMany: (items) => { ctx.items = items },
  os: { openExternal: async (u) => { opened.push(u); return true } },
}
mod.default.register(ctx)
ok(ctx.items.length === 5 && ctx.items.every((i) => i.id), 'register() contributes 5 areas')
ok(Object.keys(bundles.en).length === Object.keys(bundles.de).length, 'en and de bundles have the same key count')

storageData = { links: { quelle: 'a1', pfad: '' }, rechts: { quelle: 'b2', pfad: '' } }
for (const loc of ['en', 'de']) {
  locale = loc
  const tree = await render(React.createElement(mod.TrovePage, {}))
  const s = text(tree)
  ok(s.includes('Inbox') && s.includes('a.pdf'), loc + ': page renders sources and files')
  ok(s.includes(loc === 'en' ? 'Backend connected' : 'Backend verbunden'), loc + ': connection badge')
  ok(s.includes(loc === 'en' ? 'Copy' : 'Kopieren'), loc + ': copy buttons present')
  ok(s.includes(loc === 'en' ? '(read-only)' : '(nur lesen)'), loc + ': read-only tag shown')
  ok(s.includes(new Date(1700000000 * 1000).toLocaleString(loc)), loc + ': dates follow the active locale')
  if (loc === 'en') ok(!/[äöüß]/.test(s.replace(/"/g, '')), 'en: no umlauts anywhere in the rendered page')
  const btns = tree.root.findAllByType('button')
  const into = btns.find((b) => /Into knowledge|Ins Wissen/.test(JSON.stringify(b.props.children)))
  ok(!!into, loc + ': knowledge button present')
  // left (Inbox) is read-only, right (Archive) is writable: copy → is allowed, move → is not (it would delete read-only originals)
  const cp = btns.filter((b) => /Copy →|Kopieren →/.test(JSON.stringify(b.props.children)))
  ok(cp.length === 1 && !cp[0].props.disabled, loc + ': copy → enabled (target writable)')
  const mv = btns.filter((b) => /Move →|Verschieben →/.test(JSON.stringify(b.props.children)))
  ok(mv.length === 1 && mv[0].props.disabled, loc + ': move → disabled (source read-only)')
  const mvBack = btns.filter((b) => /← Move|← Verschieben/.test(JSON.stringify(b.props.children)))
  ok(mvBack.length === 1 && mvBack[0].props.disabled, loc + ': ← move disabled (target read-only)')
}
locale = 'en'
const AddNode = await render(React.createElement(mod.AddSource, { onDone() {} }))
ok(text(AddNode).includes('Browse'), 'AddSource offers the folder picker when ctx.files exists')
const k = await render(React.createElement(mod.IntoKnowledge, { r: { art: 'wissen', quelle: 'a1', pfad: '', datei: 'Inbox', dateien: [], anzahl: 3 } }))
ok(text(k).includes('WHOLE folder'), 'IntoKnowledge asks about the whole folder')
const rp = await render(React.createElement(mod.ResultPanel, { r: { art: 'lesen', datei: 'a.pdf', methode: 'ocr', seiten: 1, dauer_s: 1, sha256: 'x', felder: {}, verprobung: { sicher: true, bestanden: true, gruende: [] }, zwilling: 'z.md', text_anfang: '' }, back() {} }))
ok(text(rp).includes('passed (sure)'), 'ResultPanel (read) renders')
const tk = await render(React.createElement(mod.Tasks, {}))
ok(text(tk).includes('Release'), 'Tasks lists a blocked card with a Release button')
// optional integrations: off by default; Reraise without a key is only a link (no upload call possible)
for (const loc of ['en', 'de']) {
  locale = loc
  const it = await render(React.createElement(mod.Integrations, { left: { quelle: 'a1', pfad: '' } }))
  const show = it.root.findAllByType('button').find((b) => /show|zeigen/.test(JSON.stringify(b.props.children)))
  await TR.act(async () => { show.props.onClick(); await settle() })
  const s2 = text(it)
  ok(s2.includes(loc === 'en' ? 'needs in Hermes: docuseal_url' : 'braucht in Hermes: docuseal_url'), loc + ': integrations show what is missing in Hermes')
  const labels = it.root.findAllByType('button').map((b) => JSON.stringify(b.props.children)).join(' ')
  ok(!/Publish|veröffentlichen|Preview cards|Karten-Vorschau|Mirror now|Jetzt spiegeln|Send for signature|Fetch|abholen|Templates|Vorlagen/.test(labels), loc + ': no action buttons while unconfigured')
  const visit = it.root.findAllByType('button').find((b) => /reraise\.ai/.test(JSON.stringify(b.props.children)))
  await TR.act(async () => { visit.props.onClick(); await settle() })
}
ok(opened.length === 2 && opened.every((u) => u === 'https://reraise.ai'), 'Reraise without key opens only https://reraise.ai')
ok(!calls.some((c) => c[0].startsWith('/integrationen/')), 'unconfigured integrations never call an action endpoint')
fake['/integrationen'] = { ...fake['/integrationen'], reraise: { active: true, link: 'https://reraise.ai' } }
locale = 'en'
const it2 = await render(React.createElement(mod.Integrations, { left: { quelle: 'a1', pfad: '' } }))
await TR.act(async () => { it2.root.findAllByType('button').find((b) => /show/.test(JSON.stringify(b.props.children))).props.onClick(); await settle() })
ok(text(it2).includes('Preview cards (left folder)'), 'with a Reraise key: card preview offered')
ok(calls.some((c) => c[0] === '/liste') && calls.some((c) => c[0] === '/status'), 'backend calls issued')
ok(!calls.some((c) => c[1] === 'POST' && ['/aktion', '/wissen', '/register'].includes(c[0])), 'rendering alone never triggers a write action')
console.log(failures ? failures + ' FAILED' : 'UI SMOKE PASSED')
process.exit(failures ? 1 : 0)
