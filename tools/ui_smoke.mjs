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
src += "\nexport { sortEntries, TrovePage, Side, IntoKnowledge, ResultPanel, Preview, AddSource, Tasks, Search, Integrations, EN, DE }\n"

let locale = 'en'; const bundles = {}; const notes = []; let storageData = {}
const tr = (loc, key, args) => { const v = (bundles[loc] || {})[key] ?? (bundles.en || {})[key] ?? key; return typeof v === 'function' ? v(...args) : v }
const calls = []; const postFake = {}
const fake = {
  '/status': { ok: true, mandant: 'haus', profile: ['default', 'helper'], protokolle: [{ mandant: 'haus', zeilen: 3 }] },
  '/quellen': { quellen: [{ id: 'a1', art: 'ordner', name: 'Inbox', pfad: '/tmp/inbox', nur_lesen: true, warnung: 'This folder contains settings' }, { id: 'b2', art: 'ordner', name: 'Archive', pfad: '/tmp/archive', nur_lesen: false }] },
  '/liste': { quelle: 'a1', pfad: '', eintraege: [{ name: 'a.pdf', ordner: false, groesse: 2048, mtime: 1700000000, pfad: '/tmp/inbox/a.pdf', dublette: true, fundorte: 2 }, { name: 'sub', ordner: true, groesse: 0, mtime: 1700000000, pfad: '/tmp/inbox/sub' }], indexiert: false },
  '/auftraege': { auftraege: [{ id: 't1', status: 'blocked', titel: 'Check' }] },
  '/integrationen': { settings: '/settings?tab=plugins&plugin=reraise-trove', mail: { mailboxes: [{ id: 'imap', kind: 'imap', active: false, missing: ['imap_host'] }, { id: 'gmail', kind: 'gmail', active: false, missing: ['TROVE_GMAIL_CLIENT_ID'], signin: 'hermes auth add reraise-trove:gmail' }] },
    docuseal: { active: false, missing: ['docuseal_url'] }, erpnext: { active: false, missing: ['erpnext_url'] }, reraise: { active: false, link: 'https://reraise.ai' } },
}
const opened = []
globalThis.__T = {
  host: { notify: (n) => notes.push(n), navigate() {}, restartGateway() {} },
  Button: (p) => React.createElement('button', { disabled: p.disabled, title: p.title, onClick: p.onClick }, p.children),
  Badge: (p) => React.createElement('span', null, p.children), Input: (p) => React.createElement('input', { placeholder: p.placeholder, value: p.value, onChange: p.onChange, onKeyDown: p.onKeyDown }),
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
  rest: async (p, o = {}) => {
    const k = p.split('?')[0]; calls.push([k, o.method || 'GET', o.body]); if (fake[k] && !o.method) return fake[k]
    if (o.method && postFake[k]) return typeof postFake[k] === 'function' ? postFake[k]() : postFake[k]
    return { ergebnis: [], lauf: 'r1' }
  },
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
  ok(s.includes(loc === 'en' ? 'Whoever may write here (agents too)' : 'Wer hier schreiben darf (auch Agenten)'), loc + ': warning shown at the source that has one')
  ok(tree.root.findAll((n) => n.props && n.props['data-zeile'] === 'warnung').length === 1, loc + ': exactly one warning (Inbox only, not Archive)')
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
  // new folder / rename: only on the writable pane (right = Archive), disabled with a reason on the read-only pane (left)
  const nf = btns.filter((b) => /^(New folder|Neuer Ordner)$/.test(JSON.stringify(b.props.children).replace(/"/g, '')))
  ok(nf.length === 2 && nf[0].props.disabled && !nf[1].props.disabled, loc + ': new folder disabled on read-only pane, enabled on writable pane')
  const rn = btns.filter((b) => /^(Rename|Umbenennen)$/.test(JSON.stringify(b.props.children).replace(/"/g, '')))
  ok(rn.length === 2 && rn.every((b) => b.props.disabled), loc + ': rename disabled until an entry is clicked (and on read-only pane)')
  ok(tree.root.findAllByType('select').length >= 6, loc + ': sort selector present on each pane')
  const mvBack = btns.filter((b) => /← Move|← Verschieben/.test(JSON.stringify(b.props.children)))
  ok(mvBack.length === 1 && mvBack[0].props.disabled, loc + ': ← move disabled (target read-only)')
}
const callsBeforeClicks = calls.slice()   // everything issued by rendering alone
{
  // interaction: new folder on the writable pane issues exactly one POST /ordner-neu, rename is blocked on the read-only pane
  locale = 'en'
  const tree = await render(React.createElement(mod.TrovePage, {}))
  const nfBtn = tree.root.findAllByType('button').filter((b) => JSON.stringify(b.props.children) === '"New folder"')[1]
  await TR.act(async () => { nfBtn.props.onClick(); await settle() })
  const inp = tree.root.findAllByType('input').find((i) => i.props.placeholder === 'name')
  ok(!!inp, 'new folder opens an inline name field (no window.prompt)')
  await TR.act(async () => { inp.props.onChange({ target: { value: '  Taxes 2026 ' } }); await settle() })
  const okBtn = tree.root.findAllByType('button').find((b) => JSON.stringify(b.props.children) === '"OK"')
  await TR.act(async () => { okBtn.props.onClick(); await settle() })
  const posts = calls.filter((c) => c[0] === '/ordner-neu')
  ok(posts.length === 1 && posts[0][2].name === 'Taxes 2026' && posts[0][2].quelle === 'b2', 'new folder posts to /ordner-neu for the writable source')
}
{
  const L = [{ name: 'b.txt', ordner: false, groesse: 5, mtime: 30 }, { name: 'a.pdf', ordner: false, groesse: 9, mtime: 10 }, { name: 'z', ordner: true, groesse: 0, mtime: 1 }, { name: 'c.md', ordner: false, groesse: 1, mtime: 20 }, { name: 'File10.txt', ordner: false, groesse: 2, mtime: 5 }, { name: 'File2.txt', ordner: false, groesse: 3, mtime: 6 }]
  const names = (key, dir) => mod.sortEntries(L, { key, dir }).map((e) => e.name).join(',')
  ok(names('name', 'asc') === 'z,a.pdf,b.txt,c.md,File2.txt,File10.txt' && names('name', 'desc') === 'z,File10.txt,File2.txt,c.md,b.txt,a.pdf', 'sort by name (folders first, natural order, both directions)')
  ok(names('size', 'desc').split(',').slice(0, 2).join() === 'z,a.pdf' && names('date', 'asc').split(',').slice(1, 3).join() === 'File10.txt,File2.txt', 'sort by size and date keeps folders first')
  ok(names('type', 'asc').split(',').slice(1, 3).join() === 'c.md,a.pdf', 'sort by type')
}
locale = 'en'
const AddNode = await render(React.createElement(mod.AddSource, { onDone() {} }))
ok(text(AddNode).includes('Browse'), 'AddSource offers the folder picker when ctx.files exists')
const tick = (tree) => tree.root.findAllByType('input').find((i) => i.props.type === 'checkbox').props.checked
ok(tick(AddNode) === false, 'AddSource: Writable not ticked while neue_ordner_nur_lesen is on (default)')
fake['/status'] = { ...fake['/status'], neue_ordner_nur_lesen: false }
const hint = (tree) => text(tree).includes('read-only unless Writable is ticked') && !text(tree).includes('read-only until')
ok(hint(AddNode), 'AddSource: hint says read-only unless ticked (setting on)')
const AddAus = await render(React.createElement(mod.AddSource, { onDone() {} }))
ok(tick(AddAus) === true, 'AddSource: Writable ticked when neue_ordner_nur_lesen is off')
ok(hint(AddAus), 'AddSource: no "read-only until" claim when neue_ordner_nur_lesen is off')
delete fake['/status'].neue_ordner_nur_lesen
const k = await render(React.createElement(mod.IntoKnowledge, { r: { art: 'wissen', quelle: 'a1', pfad: '', datei: 'Inbox', dateien: [], anzahl: 3 } }))
ok(text(k).includes('WHOLE folder'), 'IntoKnowledge asks about the whole folder')
const rp = await render(React.createElement(mod.ResultPanel, { r: { art: 'lesen', datei: 'a.pdf', methode: 'ocr', seiten: 1, dauer_s: 1, sha256: 'x', felder: {}, verprobung: { sicher: true, bestanden: true, gruende: [] }, zwilling: 'z.md', text_anfang: '' }, back() {} }))
ok(text(rp).includes('passed (sure)'), 'ResultPanel (read) renders')
const tk = await render(React.createElement(mod.Tasks, {}))
ok(text(tk).includes('Release'), 'Tasks lists a blocked card with a Release button')
// new card: the label follows the switch (freigabe_pflicht in every GET /auftraege); one click = exactly one POST /auftrag with confirm and client_id
class FakeSR { constructor() { FakeSR.last = this } start() { this.started = true } stop() { this.stopped = true; if (this.onend) this.onend() } abort() { this.stop() } }
globalThis.window = globalThis; globalThis.SpeechRecognition = FakeSR
const btnText = (tree, re) => tree.root.findAllByType('button').find((b) => re.test(JSON.stringify(b.props.children)))
for (const [loc, pflicht, label, hint] of [['en', false, 'Create card and start', 'starts right away'], ['en', true, 'Create card (waits for release)', 'Release'],
                                         ['de', false, 'Karte anlegen und starten', 'startet sofort'], ['de', true, 'Karte anlegen (wartet auf Freigabe)', 'Freigeben']]) {
  locale = loc
  fake['/auftraege'] = { auftraege: [{ id: 't1', status: 'blocked', titel: 'Check' }], freigabe_pflicht: pflicht }
  const t0 = await render(React.createElement(mod.Tasks, {}))
  await TR.act(async () => { btnText(t0, /^"(\+ Task|\+ Auftrag)"$/).props.onClick(); await settle() })
  const ti = t0.root.findAllByType('input').find((i) => /Title|Titel/.test(i.props.placeholder))
  await TR.act(async () => { ti.props.onChange({ target: { value: 'Smoke ' + loc + pflicht } }); await settle() })
  ok(JSON.stringify(t0.toJSON()).includes(label) && JSON.stringify(t0.toJSON()).includes(hint), loc + ' switch ' + (pflicht ? 'ON' : 'OFF') + ': button says "' + label + '"')
  // dictation: text is appended, create is blocked while recording
  const dict = btnText(t0, /^"(Dictate|Diktieren)"$/)
  await TR.act(async () => { dict.props.onClick(); await settle() })
  ok(FakeSR.last && FakeSR.last.started && FakeSR.last.lang === 'de-DE', loc + ': dictation starts speech recognition in de-DE')
  const createBtn = () => btnText(t0, new RegExp(label.replace(/[()]/g, '\\$&')))
  ok(createBtn().props.disabled === true, loc + ': create is disabled while recording')
  await TR.act(async () => { FakeSR.last.onresult({ resultIndex: 0, results: [Object.assign([{ transcript: ' Hallo Welt ' }], { isFinal: true })] }); await settle() })
  const n0 = calls.filter((c) => c[0] === '/auftrag').length
  await TR.act(async () => { ti.props.onKeyDown({ key: 'Enter' }); await settle() })
  ok(calls.filter((c) => c[0] === '/auftrag').length === n0, loc + ': Enter does not create while recording')
  await TR.act(async () => { FakeSR.last.stop(); await settle() })
  ok(createBtn().props.disabled === false, loc + ': create is enabled again after the recording ended')
  await TR.act(async () => { createBtn().props.onClick(); await settle() })
  const posts = calls.filter((c) => c[0] === '/auftrag' && c[1] === 'POST').slice(n0)
  ok(posts.length === 1 && posts[0][2].confirm === true && typeof posts[0][2].client_id === 'string' && /^[A-Za-z0-9_-]{8,64}$/.test(posts[0][2].client_id) && posts[0][2].text === 'Hallo Welt' && !('quelle' in posts[0][2]),
     loc + ': one click = exactly one POST /auftrag with confirm, client_id and the dictated text, no origin flag')
}
{
  // the tab's 20-s refresh carries the switch: the label follows a change without reloading the page
  const takte = []; const echtIv = globalThis.setInterval
  globalThis.setInterval = (fn) => { takte.push(fn); return 0 }
  locale = 'en'
  fake['/auftraege'] = { auftraege: [], freigabe_pflicht: false }
  const tw = await render(React.createElement(mod.Tasks, {}))
  globalThis.setInterval = echtIv
  await TR.act(async () => { btnText(tw, /^"\+ Task"$/).props.onClick(); await settle() })
  const tiw = tw.root.findAllByType('input').find((i) => /Title/.test(i.props.placeholder))
  await TR.act(async () => { tiw.props.onChange({ target: { value: 'Switch' } }); await settle() })
  ok(!!btnText(tw, /Create card and start/), 'switch OFF from GET /auftraege: "Create card and start"')
  fake['/auftraege'] = { auftraege: [], freigabe_pflicht: true }
  await TR.act(async () => { takte.forEach((f) => f()); await settle() })
  ok(!!btnText(tw, /Create card \(waits for release\)/) && !btnText(tw, /Create card and start/), 'switch turned ON: the next refresh changes the label, no reload')
  // two creates in a row in the same tab: two different client_ids; a response with "fehler" shows its text; "wiederholt" shows a note
  fake['/auftraege'] = { auftraege: [], freigabe_pflicht: false }
  const ids = []
  const anlegen = async (titel) => {
    if (!tw.root.findAllByType('input').some((i) => /Title/.test(i.props.placeholder))) await TR.act(async () => { btnText(tw, /^"\+ Task"$/).props.onClick(); await settle() })
    const ti = tw.root.findAllByType('input').find((i) => /Title/.test(i.props.placeholder))
    if (titel !== null) await TR.act(async () => { ti.props.onChange({ target: { value: titel } }); await settle() })
    const n0 = calls.filter((c) => c[0] === '/auftrag' && c[1] === 'POST').length
    await TR.act(async () => { btnText(tw, /Create card/).props.onClick(); await settle() })
    const p = calls.filter((c) => c[0] === '/auftrag' && c[1] === 'POST').slice(n0)
    ids.push(p.length === 1 ? p[0][2].client_id : null)
  }
  postFake['/auftrag'] = { id: 't7', status: 'blocked', fehler: 'created, start failed (card stays blocked, use Release): FEHLERTEXT-A11' }
  await anlegen('Erste')
  ok(text(tw).includes('FEHLERTEXT-A11'), 'a create answered with "fehler": the text is shown in the tab')
  postFake['/auftrag'] = { id: 't8', status: 'started', wiederholt: true }
  await anlegen('Zweite')
  ok(ids[0] && ids[1] && ids[0] !== ids[1], 'two creates in a row in the same tab use two different client_ids')
  ok(text(tw).includes('had already created task t8'), 'a repeated create ("wiederholt") says that the task existed')
  // a failed create: the retry with the same text keeps the client_id, a changed text gets a new one
  postFake['/auftrag'] = () => { throw new Error('503 Task list busy, try again') }
  await anlegen('Dritte')
  await anlegen(null)
  ok(ids[2] && ids[3] === ids[2], 'retry after a failure without changes: same client_id')
  await anlegen('Dritte, geaendert')
  ok(ids[4] && ids[4] !== ids[2], 'retry after a failure with a changed text: new client_id')
  delete postFake['/auftrag']
}
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
// K1: consequential actions ask first
fake['/integrationen'] = { ...fake['/integrationen'], erpnext: { active: true, missing: [] } }
const it3 = await render(React.createElement(mod.Integrations, { left: { quelle: 'a1', pfad: '' } }))
await TR.act(async () => { it3.root.findAllByType('button').find((b) => /show/.test(JSON.stringify(b.props.children))).props.onClick(); await settle() })
const dt = it3.root.findAllByType('button').find((b) => /Create doctypes/.test(JSON.stringify(b.props.children)))
await TR.act(async () => { dt.props.onClick(); await settle() })
ok(!calls.some((c) => c[0] === '/integrationen/erpnext/doctypes') && text(it3).includes('Continue?'), 'Create doctypes asks before doing anything')
const yes = it3.root.findAllByType('button').find((b) => /Yes, do it/.test(JSON.stringify(b.props.children)))
await TR.act(async () => { yes.props.onClick(); await settle() })
ok(calls.filter((c) => c[0] === '/integrationen/erpnext/doctypes' && c[2] && c[2].confirm === true).length === 1, 'Create doctypes runs once after confirmation and sends confirm: true')
// recognition model: shown with mode and pin; Test calls the test route, Pin asks first
ok(!calls.some((c) => c[0].startsWith('/erkennung')), 'recognition model: no call while not configured')
fake['/integrationen'] = { ...fake['/integrationen'], erkennung: { mode: 'off', model: '', fallback: 'rules', pin: null, problems: ['OCR model \'unlimited-ocr-3b:latest\' is not pinned yet: press \'Test model\'.'], ocr: { model: 'unlimited-ocr-3b:latest', use: 'scans', pin: null } } }
for (const loc of ['en', 'de']) {
  locale = loc
  const it4 = await render(React.createElement(mod.Integrations, { left: { quelle: 'a1', pfad: '' } }))
  await TR.act(async () => { it4.root.findAllByType('button').find((b) => /show|zeigen/.test(JSON.stringify(b.props.children))).props.onClick(); await settle() })
  const s4 = text(it4)
  ok(s4.includes('unlimited-ocr-3b:latest') && s4.includes(loc === 'en' ? 'not pinned yet' : 'noch nicht festgelegt'), loc + ': recognition model shows the OCR model and its pin state')
  const pinB = it4.root.findAllByType('button').find((b) => /"(Pin|Festlegen)"/.test(JSON.stringify(b.props.children)))
  await TR.act(async () => { pinB.props.onClick(); await settle() })
  ok(!calls.some((c) => c[0] === '/erkennung/pin'), loc + ': Pin asks before pinning')
  if (loc === 'en') {
    const testB = it4.root.findAllByType('button').find((b) => /Test model/.test(JSON.stringify(b.props.children)))
    await TR.act(async () => { testB.props.onClick(); await settle() })
    ok(calls.filter((c) => c[0] === '/erkennung/test' && c[1] === 'POST').length === 1, 'Test model calls /erkennung/test once')
  }
}
ok(calls.some((c) => c[0] === '/liste') && calls.some((c) => c[0] === '/status'), 'backend calls issued')
ok(!callsBeforeClicks.some((c) => c[1] === 'POST' && ['/aktion', '/wissen', '/register', '/umbenennen', '/ordner-neu'].includes(c[0])), 'rendering alone never triggers a write action')
console.log(failures ? failures + ' FAILED' : 'UI SMOKE PASSED')
process.exit(failures ? 1 : 0)
