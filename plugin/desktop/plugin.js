// Trove — Hermes Desktop tab (disk plugin, no build step).
// Two panes (source on the left, target on the right) with preview on both sides. Every action goes through the hash-chained
// receipt log. Per file: Read (text layers, OCR, fixed plausibility checks), Split (multi-page PDFs), Trace (receipt history).
// Per folder: Into knowledge (markdown twin + knowledge card in your wiki), Index files, Check changes.
// Trove never asks for or stores passwords: mail attachments and drive files arrive in the source folders through your Hermes agents.
// UI language: English by default, German as the second language through the Hermes plugin i18n (ctx.i18n.register).
import { host, Button, Badge, Input, SandboxedFrame, ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA, usePluginI18n, useI18n } from '@hermes/plugin-sdk'
import { useState, useEffect, useCallback, useRef } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'reraise-trove'
let rest = null
let storage = null
let files = null          // ctx.files (the app's folder picker); may be missing on older Hermes builds
let T = (k) => k          // ctx.i18n.t, for code outside React (notifications)
let MANDANT = ''          // comes from the backend (GET /status), no second source of truth
let PROFILE = ['default']
let PROFILE_LOADED = false
const DEFAULT_COLLECTION = 'quellen'  // Into knowledge without a collection name -> raw/trove/quellen, cards in wiki/quellen
const AFFILIATION_OPEN = 'offen'      // Into knowledge without an affiliation: marked "offen" (open)

const errText = (e) => String((e && e.message) || e)
const fmtB = (n) => n == null ? '–' : n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : n < 1073741824 ? (n / 1048576).toFixed(1) + ' MB' : (n / 1073741824).toFixed(2) + ' GB'
const fmtT = (t, locale) => { if (!t) return ''; try { return new Date(t * 1000).toLocaleString(locale || undefined) } catch (e) { return new Date(t * 1000).toLocaleString() } }
const baseName = (p) => String(p || '').split(/[\\/]/).pop()
const box = 'rounded border border-(--ui-stroke-secondary)'
const quiet = 'text-(--ui-text-tertiary)'
const selectCls = 'rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-xs'
// Bars use inline styles only: the host ships only the Tailwind classes it uses itself
const BAR = { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8, minWidth: 0 }
const QUIET_STYLE = { fontSize: 12, color: 'var(--ui-text-tertiary)' }
const GROUP = { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 4, padding: '2px 6px', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 4, minWidth: 0 }
const PAIR = { display: 'flex', gap: 4, whiteSpace: 'nowrap' }

function group(name, buttons) {
  const present = buttons.filter(Boolean)
  return present.length ? jsxs('div', { role: 'group', 'aria-label': name, style: GROUP, children: [jsx('span', { style: { ...QUIET_STYLE, fontSize: 11, marginRight: 2 }, children: name }), ...present] }, name) : null
}
// Small bar button; on = active view, disabled + title = reason
function btn(text, onClick, o = {}) {
  return jsx(Button, { size: 'sm', variant: o.on ? 'secondary' : 'ghost', title: o.title, disabled: o.disabled, onClick, children: text })
}
function Row(label, value) {
  return jsxs('div', { className: 'flex justify-between gap-3 text-xs', children: [jsx('span', { className: quiet, children: label }), jsx('span', { className: 'font-mono truncate', title: String(value ?? ''), children: String(value ?? '–') })] })
}
// Machine states from the backend -> readable text
function stateText(t, z) { return ({ neu: t('stNew'), schon_da: t('stThere'), dublette: t('stDup'), fehler: t('stError') })[z] || z || '' }

// ---------------------------------------------------------------- add a source folder
function AddSource({ onDone }) {
  const t = usePluginI18n(ID)
  const [name, setName] = useState(''); const [path, setPath] = useState(''); const [writable, setWritable] = useState(false)
  const [err, setErr] = useState(null); const [busy, setBusy] = useState(false)
  const canPick = !!(files && typeof files.requestFolder === 'function')
  const browse = async () => {
    setErr(null)
    try {
      const r = await files.requestFolder()   // the app's own folder picker; ctx.files is the documented way to choose folders
      if (r && r.ok && r.folder && r.folder.path) { setPath(r.folder.path); if (!name) setName(r.folder.name || '') }
    } catch (e) { setErr(errText(e)) }
  }
  const add = async () => {
    setBusy(true); setErr(null)
    try { await rest('/quellen', { method: 'POST', body: { mandant: MANDANT, art: 'ordner', name, pfad: path.trim(), nur_lesen: !writable } }); onDone(true) } catch (e) { setErr(errText(e)) } finally { setBusy(false) }
  }
  return jsxs('div', { className: box + ' p-2 flex flex-col gap-2 text-xs', children: [
    jsx('span', { className: quiet, children: t('addHint') }),
    jsxs('div', { className: 'flex gap-2', children: [
      jsx(Input, { placeholder: t('pathPlaceholder'), value: path, onChange: (e) => setPath(e.target.value), className: 'text-xs flex-1' }),
      canPick ? jsx(Button, { size: 'sm', variant: 'ghost', onClick: browse, children: t('browse') }) : null
    ] }),
    jsx(Input, { placeholder: t('namePlaceholder'), value: name, onChange: (e) => setName(e.target.value) }),
    jsxs('label', { className: 'flex gap-1 items-center ' + quiet, children: [jsx('input', { type: 'checkbox', checked: writable, onChange: (e) => setWritable(e.target.checked) }), t('writableCheck')] }),
    err ? jsx('div', { className: 'text-destructive', children: err }) : null,
    jsxs('div', { className: 'flex gap-2', children: [jsx(Button, { size: 'sm', disabled: busy || !path.trim(), onClick: add, children: busy ? t('checking') : t('addBtn') }), jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => onDone(false), children: t('cancel') })] })
  ] })
}

// ---------------------------------------------------------------- preview
function Preview({ source, entry }) {
  const t = usePluginI18n(ID)
  const [v, setV] = useState(null); const [page, setPage] = useState(0)
  useEffect(() => { setPage(0) }, [source, entry && entry.rel])
  useEffect(() => {
    setV(null)
    if (!source || !entry || entry.ordner) return
    let live = true
    rest('/vorschau?quelle=' + encodeURIComponent(source) + '&pfad=' + encodeURIComponent(entry.rel) + '&seite=' + page).then((r) => { if (live) setV(r) }).catch((e) => { if (live) setV({ art: 'fehler', hinweis: errText(e) }) })
    return () => { live = false }
  }, [source, entry && entry.rel, page])
  if (!entry) return jsx('div', { className: quiet + ' text-xs p-2', children: t('clickForPreview') })
  if (entry.ordner) return jsx('div', { className: quiet + ' text-xs p-2', children: t('folderLabel', entry.name) })
  if (!v) return jsx('div', { className: quiet + ' text-xs p-2', children: t('loadingPreview') })
  if (v.art === 'bild') return jsx('img', { src: v.data, alt: entry.name, className: 'max-h-full max-w-full object-contain' })
  if (v.art === 'pdf_seite') return jsxs('div', { className: 'flex h-full flex-col gap-1 min-h-0', children: [
    jsxs('div', { className: 'flex items-center gap-1 text-xs p-1', children: [
      jsx(Button, { size: 'sm', variant: 'ghost', disabled: v.seite <= 0, onClick: () => setPage(v.seite - 1), children: '‹' }),
      jsx('span', { children: t('pageOf', v.seite + 1, v.seiten) }),
      jsx(Button, { size: 'sm', variant: 'ghost', disabled: v.seite + 1 >= v.seiten, onClick: () => setPage(v.seite + 1), children: '›' })] }),
    jsx('div', { className: 'flex-1 min-h-0 overflow-auto', children: jsx('img', { src: v.data, alt: entry.name + ' ' + t('pageOf', v.seite + 1, v.seiten), className: 'w-full' }) })] })
  if (v.art === 'karte') return jsxs('div', { className: 'p-2 overflow-auto h-full', style: { fontSize: 11 }, children: [
    jsx('div', { className: 'mb-2 rounded border border-(--ui-stroke-secondary) p-1', children: v.kopf.map((z) => jsxs('div', { children: [jsx('span', { className: quiet, children: z[0] + ': ' }), z[1]] }, z[0])) }),
    jsx('pre', { className: 'whitespace-pre-wrap break-words font-sans', children: v.inhalt + (v.abgeschnitten ? '\n' + t('truncated') : '') })] })
  if (v.art === 'pdf') return jsx(SandboxedFrame, { src: v.data, title: entry.name, className: 'w-full h-full' })
  if (v.art === 'text') return jsx('pre', { className: 'whitespace-pre-wrap break-all p-2 font-mono', style: { fontSize: 11 }, children: v.inhalt + (v.abgeschnitten ? '\n' + t('truncated') : '') })
  return jsx('div', { className: quiet + ' text-xs p-2', children: t('noPreview') + ': ' + (v.hinweis || v.mime || '') + ' · ' + fmtB(v.groesse) })
}

// ---------------------------------------------------------------- Into knowledge
// lock: ONE useRef of the parent so two runs never start at once
function IntoKnowledge({ r }) {
  const t = usePluginI18n(ID)
  const [collection, setCollection] = useState(''); const [affiliation, setAffiliation] = useState('')
  const [cards, setCards] = useState(true); const [follow, setFollow] = useState(false)
  const [res, setRes] = useState(null); const [more, setMore] = useState(false); const [wholeOk, setWholeOk] = useState(false)
  const lock = useRef(false)
  const collectionEff = collection.trim().toLowerCase() || DEFAULT_COLLECTION
  const running = !!(res && res.laeuft)
  const picked = r.dateien || []; const whole = picked.length === 0
  const go = async () => {
    if (lock.current) return
    if (whole && !wholeOk) { setWholeOk(true); return }
    lock.current = true
    setRes({ laeuft: true })
    const body = { mandant: MANDANT, quelle: r.quelle, pfad: r.pfad, sammlung: collectionEff, sichtbarkeit: 'privat', zugehoerigkeit: affiliation.trim() || AFFILIATION_OPEN, karten: cards, nachfuehren: follow }
    if (!whole) body.dateien = picked
    try {
      const z = await rest('/wissen', { method: 'POST', body, timeoutMs: 7200000 })
      setRes(z)
      if (z.wolke_hinweis) host.notify({ kind: 'warning', message: 'Trove: ' + z.wolke_hinweis })
    } catch (e) { setRes({ ausfall: errText(e) }) } finally { lock.current = false; setWholeOk(false) }
  }
  const line = (e, i) => jsx('div', {
    className: 'truncate', title: e.md || e.fehler,
    children: (e.fehler ? t('errorWord') + ' ' : '') + baseName(e.datei) + ' → ' + stateText(t, e.zustand)
      + (e.karte ? ' · ' + t('cardWord') + ' ' + e.karte : e.grund ? ' · ' + t('noCard') + ': ' + e.grund : '') + (e.fehler ? ' ' + e.fehler : '')
  }, i)
  const explain = (whole ? t('wholeFolder', r.datei || '/', r.anzahl || 0) : t('selectedFiles', picked.length, picked.map((x) => x.split('/').pop()).join(', '))) + ' ' + t('knowledgeExplain')
  const summary = (z) => t('summary', z.dateien, z.neu, z.schon_da, z.dubletten || 0, z.karten, z.fehler)
    + (z.nachgefuehrt ? ' · ' + t('followed') : '') + ' · ' + t('qmdState', z.qmd && z.qmd.uebersprungen ? t('qmdSkipped') : ((z.qmd && z.qmd.dateien_im_index) || 0) + ' ' + t('inIndex'))
  let out = null
  if (running) out = jsx('div', { children: t('readingNow') })
  else if (res && res.ausfall) out = jsx('div', { className: 'text-destructive', children: res.ausfall })
  else if (res) out = jsxs('div', { children: [
    jsx('div', { className: 'font-medium', children: summary(res) }),
    res.wolke_hinweis ? jsx('div', { className: 'font-medium text-destructive', children: '☁ ' + res.wolke_hinweis }) : null,
    ...(res.hinweise || []).map((h, i) => jsx('div', { className: 'text-destructive', children: h }, 'h' + i)),
    ...(res.eintraege || []).slice(0, 40).map(line)] })
  const label = running ? t('running') : whole ? (wholeOk ? t('yesWhole', r.anzahl || 0) : t('intoWhole')) : t('intoSelected', picked.length)
  return jsxs('div', { className: 'flex flex-col gap-1', children: [
    jsx('div', { className: quiet, children: explain }),
    jsxs('div', { className: 'flex gap-2 items-center flex-wrap', children: [
      jsx(Input, { placeholder: t('affiliationPlaceholder'), value: affiliation, onChange: (e) => setAffiliation(e.target.value), className: 'text-xs w-56', title: t('affiliationTip') }),
      jsxs('label', { className: 'text-xs flex items-center gap-1', children: [jsx('input', { type: 'checkbox', checked: cards, onChange: (e) => setCards(e.target.checked) }), t('makeCards')] }),
      jsx(Button, { size: 'sm', disabled: running, title: t('noMandatory', DEFAULT_COLLECTION, AFFILIATION_OPEN), onClick: go, children: label }),
      wholeOk && !running ? jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setWholeOk(false), children: t('cancel') }) : null,
      jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setMore(!more), children: more ? t('less') : t('more') })
    ] }),
    more ? jsxs('div', { className: 'flex gap-2 items-center flex-wrap', children: [
      jsx(Input, { placeholder: t('collectionPlaceholder', DEFAULT_COLLECTION), value: collection, onChange: (e) => setCollection(e.target.value), className: 'text-xs w-44' }),
      jsxs('label', { className: 'text-xs flex items-center gap-1', title: t('followTip'), children: [jsx('input', { type: 'checkbox', checked: follow, onChange: (e) => setFollow(e.target.checked) }), t('followCheck')] })
    ] }) : null,
    wholeOk && !running ? jsx('div', { className: 'font-medium text-destructive', children: t('wholeWarn', r.anzahl || 0) }) : null,
    out
  ] })
}

function ResultPanel({ r, back }) {
  const t = usePluginI18n(ID)
  const z = (k, v) => jsxs('div', { children: [jsx('span', { className: quiet, children: k + ': ' }), jsx('span', { children: String(v ?? '–') })] }, k)
  const ev = (e) => jsx('div', { children: '#' + e.nr + ' ' + e.zeit + ' ' + e.ereignis + ' ' + (e.quelle || '') + (e.ziel ? ' → ' + e.ziel : '') + (e.meldung ? ' [' + e.meldung + ']' : '') }, e.nr)
  let body
  if (r.laeuft) body = jsx('div', { children: t('runningOcr') })
  else if (r.fehler) body = jsx('div', { className: 'text-destructive', children: r.fehler })
  else if (r.art === 'lesen') body = jsxs('div', { children: [
    z(t('method'), r.methode + ', ' + t('nPages', r.seiten) + ', ' + r.dauer_s + ' s'), z('sha256', r.sha256),
    ...Object.entries(r.felder || {}).map(([k, v]) => z(k, v)),
    jsx('div', { className: 'mt-1 font-medium', children: t('verification') + ': ' + (r.verprobung.sicher ? t('passedSure') : r.verprobung.bestanden ? t('passed') : t('needsReview')) }),
    ...(r.verprobung.gruende || []).map((g, i) => jsx('div', { children: '· ' + g }, i)),
    jsx('div', { className: quiet + ' mt-1', children: t('twin') + ': ' + r.zwilling }),
    jsx('pre', { className: 'mt-1 whitespace-pre-wrap ' + quiet, style: { fontSize: 10 }, children: r.text_anfang })
  ] })
  else if (r.art === 'stapel') body = jsxs('div', { children: [
    z(t('batch'), r.datei + ' → ' + (r.ziel || '') + ' · ' + t('splitSummary', (r.belege || []).length, r.seiten, r.dauer_s)),
    ...(r.belege || []).map((b) => jsx('div', { children: '#' + b.nr + ' ' + t('pagesRange', b.seiten.join('–')) + ' ' + (b.nummer ? '№ ' + b.nummer + ' ' : '') + b.arten.join('/') + (b.buchungsfassung ? ' (' + t('bookingVersion') + ')' : '') + ' → ' + b.datei }, b.nr))
  ] })
  else body = jsxs('div', { children: [
    ['direkt', 'rueckwaerts', 'vorwaerts', 'gleicher_inhalt'].map((k) => jsxs('div', { children: [jsx('div', { className: quiet + ' mt-1', children: t('trace_' + k) + ': ' + ((r[k] || []).length) }), ...(r[k] || []).slice(0, 30).map(ev)] }, k))
  ] })
  const title = r.art === 'spur' ? t('traceTitle', r.datei || '') : t('art_' + r.art) + ': ' + (r.datei || '')
  return jsxs('div', { className: 'p-2 font-mono', style: { fontSize: 11 }, children: [jsx('div', { className: 'font-medium mb-1', children: title }), body, jsx(Button, { size: 'sm', variant: 'ghost', onClick: back, children: t('backToPreview') })] })
}

// ---------------------------------------------------------------- one side (source or target)
function Side({ side, sources, state, setState, selection, setSelection, reload, onSourcesChanged }) {
  const t = usePluginI18n(ID)
  const { locale } = useI18n()
  const [list, setList] = useState(null); const [error, setError] = useState(null); const [adding, setAdding] = useState(false); const [active, setActive] = useState(null)
  const [filter, setFilter] = useState('all'); const [panel, setPanel] = useState(null); const [changes, setChanges] = useState(null)
  const [askWritable, setAskWritable] = useState(false)
  const q = sources.find((x) => x.id === state.quelle)
  const knowledgeReason = !state.quelle ? t('pickSourceFirst') : !q ? t('sourceLoading') : null
  const load = useCallback(() => {
    if (!q) { setList(null); return }
    setError(null)
    rest('/liste?quelle=' + encodeURIComponent(q.id) + '&pfad=' + encodeURIComponent(state.pfad || '')).then((r) => {
      r.eintraege.forEach((e) => { e.rel = (state.pfad ? state.pfad + '/' : '') + e.name }); setList(r)
    }).catch((e) => setError(errText(e)))
  }, [state.quelle, state.pfad, q && q.id])
  useEffect(() => { load() }, [load, reload])
  useEffect(() => { setAskWritable(false) }, [state.quelle])
  const go = (path) => { setState({ ...state, pfad: path }); setSelection([]); setActive(null); setPanel(null) }
  const parts = (state.pfad || '').split('/').filter(Boolean)
  const entries = list ? list.eintraege : []
  const visible = entries.filter((e) => filter === 'all' || e.ordner || (filter === 'only' ? e.dublette : !e.dublette))
  const picked = entries.filter((e) => !e.ordner && selection.includes(e.name)).map((e) => e.rel)
  const intoKnowledge = () => knowledgeReason ? null : setPanel({ art: 'wissen', datei: state.pfad || q.name, quelle: q.id, pfad: state.pfad || '', dateien: picked, anzahl: entries.filter((e) => !e.ordner).length })
  const toggle = (name) => setSelection(selection.includes(name) ? selection.filter((n) => n !== name) : [...selection, name])
  const register = async () => {
    setError(null)
    try {
      const r = await rest('/register', { method: 'POST', body: { mandant: MANDANT, quelle: q.id, pfad: state.pfad || '' } })
      host.notify({ kind: 'info', message: 'Trove: ' + t('registerDone', r.neu, r.gesamt) }); load()
    } catch (e) { setError(errText(e)) }
  }
  const checkChanges = async () => {
    setError(null); setChanges({ laeuft: true })
    try { const r = await rest('/aenderungen', { method: 'POST', body: { mandant: MANDANT, quelle: q.id, pfad: state.pfad || '' }, timeoutMs: 3600000 }); setChanges(r); load() } catch (e) { setChanges(null); setError(errText(e)) }
  }
  const showTrace = async (e) => {
    try { setPanel({ art: 'spur', datei: e.name, ...(await rest('/spur?datei=' + encodeURIComponent(e.pfad))) }) } catch (x) { setPanel({ art: 'spur', datei: e.name, fehler: errText(x) }) }
  }
  const fileAction = async (art) => {
    const e = entries.find((x) => x.name === active)
    if (!e || e.ordner) { host.notify({ kind: 'warning', message: 'Trove: ' + t('clickFileFirst') }); return }
    setPanel({ art, laeuft: true, datei: e.name })
    try {
      const path = art === 'lesen' ? '/lesen' : '/stapel'
      setPanel({ art, datei: e.name, ...(await rest(path, { method: 'POST', body: { mandant: MANDANT, quelle: q.id, pfad: e.rel }, timeoutMs: 1800000 })) })
      load()
    } catch (x) { setPanel({ art, datei: e.name, fehler: errText(x) }) }
  }
  const setWritable = async (writable) => {
    setError(null)
    try { await rest('/quellen/' + q.id + '/schreibbar', { method: 'POST', body: { mandant: MANDANT, nur_lesen: !writable } }); setAskWritable(false); onSourcesChanged() } catch (e) { setError(errText(e)) }
  }
  const fileBtn = (text, art, title) => btn(text, () => fileAction(art), active ? { title } : { title: t('clickFileFirst'), disabled: true })
  const preview = jsx(Preview, { source: q && q.id, entry: entries.find((e) => e.name === active) })
  const bottom = panel && panel.art === 'wissen' ? jsx(IntoKnowledge, { r: panel }) : panel ? jsx(ResultPanel, { r: panel, back: () => setPanel(null) }) : preview
  return jsxs('div', { className: 'flex flex-col gap-2 min-w-0 h-full', children: [
    jsxs('div', { 'data-zeile': 'quelle', style: BAR, children: [
      jsx('span', { className: 'text-xs font-medium', children: side === 'links' ? t('sourceLeft') : t('targetRight') }),
      jsxs('select', { className: selectCls, style: { flex: '1 1 120px', minWidth: 0, maxWidth: 240 }, value: state.quelle || '', onChange: (e) => { setState({ quelle: e.target.value, pfad: '' }); setSelection([]); setActive(null); setPanel(null) }, children: [
        jsx('option', { value: '', children: t('chooseSource') }),
        ...sources.map((x) => jsx('option', { value: x.id, children: '📁 ' + x.name + ' ' + (x.nur_lesen ? t('readOnlyTag') : t('writableTag')) }, x.id))
      ] }),
      q ? btn('×', async () => { try { await rest('/quellen/' + q.id, { method: 'DELETE' }); setState({ quelle: '', pfad: '' }); onSourcesChanged() } catch (e) { setError(errText(e)) } }, { title: t('removeSource') }) : null,
      q && q.nur_lesen && !askWritable ? btn(t('makeWritable'), () => setAskWritable(true), { title: t('makeWritableTip') }) : null,
      q && !q.nur_lesen ? btn(t('makeReadOnly'), () => setWritable(false), { title: t('makeReadOnlyTip') }) : null,
      btn(t('addFolder'), () => setAdding(!adding))
    ] }),
    askWritable ? jsxs('div', { className: box + ' p-2 text-xs flex gap-2 items-center flex-wrap', children: [
      jsx('span', { className: 'text-destructive', children: t('writableWarn', q ? q.name : '') }),
      jsx(Button, { size: 'sm', onClick: () => setWritable(true), children: t('yesWritable') }),
      jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setAskWritable(false), children: t('cancel') })] }) : null,
    adding ? jsx(AddSource, { onDone: (ok) => { setAdding(false); if (ok) onSourcesChanged() } }) : null,
    jsxs('div', { 'data-zeile': 'pfad', className: 'text-xs', style: BAR, children: [
      q ? jsxs('span', { title: q.pfad, style: { flex: '1 1 140px', minWidth: 140, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: [
        jsx('button', { className: 'underline', onClick: () => go(''), children: q.pfad }),
        ...parts.map((p, i) => jsxs('span', { children: [' / ', jsx('button', { className: 'underline', onClick: () => go(parts.slice(0, i + 1).join('/')), children: p })] }, i))] }) : jsx('span', { style: { flex: '1 1 140px' } }),
      q ? btn(list && list.indexiert ? t('indexed') : t('indexFiles'), register, { title: t('indexTip') }) : null,
      q ? btn(t('checkChanges'), checkChanges, { title: t('checkChangesTip') }) : null,
      q ? jsxs('select', { className: selectCls, value: filter, onChange: (e) => setFilter(e.target.value), children: [jsx('option', { value: 'all', children: t('fAll') }), jsx('option', { value: 'only', children: t('fDup') }), jsx('option', { value: 'without', children: t('fNoDup') })] }) : null,
      jsx(Button, { size: 'sm', disabled: !!knowledgeReason, title: knowledgeReason ? t('notNow', knowledgeReason) : t('intoKnowledgeTip'), onClick: intoKnowledge, children: knowledgeReason ? t('intoKnowledge') + ' – ' + knowledgeReason : picked.length ? t('intoKnowledgeN', picked.length) : t('intoKnowledgeAll') })
    ] }),
    error ? jsx('div', { className: 'text-xs text-destructive', children: error }) : null,
    changes ? jsxs('div', { className: box + ' p-2 text-xs', children: [
      jsx('span', { className: 'font-medium', children: changes.laeuft ? t('checkingChanges') : t('changesRun', changes.lauf) + (changes.erster_lauf ? ' ' + t('baselineTaken') : '') + ': ' + Object.entries(changes.zusammen || {}).map(([k, v]) => k + ' ' + v).join(' · ') }),
      !changes.laeuft ? jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setChanges(null), children: '×' }) : null,
      ...(!changes.laeuft ? ['neu', 'weg', 'verschoben', 'aussen_geaendert', 'durch_trove'].flatMap((k) => (changes.befunde[k] || []).slice(0, 15).map((b, i) => jsx('div', { className: 'font-mono truncate', children: k + ': ' + (b.pfad || (b.von + ' → ' + b.nach)) }, k + i))) : [])
    ] }) : null,
    jsxs('div', { 'data-liste': side, className: box + ' flex-1 min-h-0 overflow-auto text-xs', style: { minHeight: 160 }, children: [
      !q ? jsx('div', { className: quiet + ' p-2', children: t('noSource') }) : !list ? jsx('div', { className: quiet + ' p-2', children: t('loading') }) : null,
      state.pfad ? jsx('div', { className: 'px-2 py-1 cursor-pointer hover:bg-(--ui-row-hover-background)', onDoubleClick: () => go(parts.slice(0, -1).join('/')), children: '⬆ ..' }) : null,
      ...visible.map((e) => jsxs('div', { className: 'flex items-center gap-2 px-2 py-0.5 cursor-pointer hover:bg-(--ui-row-hover-background) ' + (active === e.name ? 'bg-(--ui-bg-secondary)' : ''),
        onClick: () => { setActive(e.name); setPanel(null) }, onDoubleClick: () => e.ordner ? go(e.rel) : showTrace(e), children: [
        jsx('input', { type: 'checkbox', checked: selection.includes(e.name), onChange: () => toggle(e.name), onClick: (ev) => ev.stopPropagation() }),
        jsx('span', { className: 'flex-1 truncate', title: e.pfad, children: (e.ordner ? '📁 ' : '') + e.name }),
        e.dublette ? jsx(Badge, { children: t('foundNTimes', e.fundorte) }) : null,
        jsx('span', { className: quiet + ' w-16 text-right', children: e.ordner ? '' : fmtB(e.groesse) }),
        jsx('span', { className: quiet + ' w-32 text-right', children: fmtT(e.mtime, locale) })
      ] }, e.name))
    ] }),
    jsxs('div', { 'data-zeile': 'fuss', className: 'text-xs', style: BAR, children: [
      jsx('span', { className: quiet, style: { whiteSpace: 'nowrap' }, children: list ? t('entriesMarked', list.eintraege.length, selection.length) : '' }),
      fileBtn(t('read'), 'lesen', t('readTip')),
      fileBtn(t('split'), 'stapel', t('splitTip')),
      jsxs('span', { 'data-paar': true, style: { ...PAIR, marginLeft: 'auto' }, children: [
        btn(t('all'), () => setSelection(entries.map((e) => e.name)), { disabled: !list }),
        btn(t('clearSelection'), () => setSelection([]), { disabled: !selection.length })] })
    ] }),
    jsx('div', { className: box + ' overflow-auto shrink-0', style: { height: 220 }, children: bottom })
  ] })
}

// ---------------------------------------------------------------- search (register + receipt log)
function Search() {
  const t = usePluginI18n(ID)
  const [q, setQ] = useState(''); const [r, setR] = useState(null); const [open, setOpen] = useState(false)
  const run = async () => { if (q.trim().length < 2) return; setOpen(true); setR({ laeuft: true }); try { setR(await rest('/suche?q=' + encodeURIComponent(q.trim()))) } catch (e) { setR({ fehler: errText(e) }) } }
  const line = (text, k) => jsx('div', { className: 'truncate', title: text, children: text }, k)
  return jsxs('div', { style: { position: 'relative' }, children: [
    jsxs('div', { className: 'flex gap-2 items-center', children: [
      jsx(Input, { placeholder: t('searchPlaceholder'), value: q, onChange: (e) => setQ(e.target.value), onKeyDown: (e) => { if (e.key === 'Enter') run() }, className: 'text-xs flex-1' }),
      jsx(Button, { size: 'sm', onClick: run, children: t('search') }),
      r ? jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setOpen(!open), children: open ? t('collapse') : t('expand') }) : null
    ] }),
    // overlays the bars below instead of pushing them down
    open && r ? jsx('div', { 'data-suchergebnis': true, className: box + ' overflow-auto p-2 font-mono', style: { fontSize: 11, maxHeight: 260, position: 'absolute', top: '100%', left: 0, right: 0, marginTop: 4, zIndex: 20, background: 'var(--ui-bg-chrome)', boxShadow: '0 6px 16px rgba(0,0,0,.18)' }, children:
      r.laeuft ? t('searching') : r.fehler ? jsx('span', { className: 'text-destructive', children: r.fehler }) : r.hinweis ? r.hinweis : jsxs('div', { children: [
        ...Object.entries(r.liegt || {}).map(([where, paths]) => jsxs('div', { className: 'mb-1', children: [jsx('div', { className: 'font-medium', children: t('foundUnder', where, paths.length) }), ...paths.map((p, i) => line(p, i))] }, where)),
        (r.protokoll && r.protokoll.length) ? jsxs('div', { children: [jsx('div', { className: 'font-medium', children: t('receiptLogN', r.protokoll.length) }), ...r.protokoll.slice(0, 20).map((e) => line('#' + e.nr + ' ' + e.zeit + ' ' + e.ereignis + ' ' + (e.quelle || '') + (e.ziel ? ' → ' + e.ziel : '') + (e.meldung ? ' [' + e.meldung + ']' : ''), e.nr))] }) : null
      ] }) }) : null
  ] })
}

// ---------------------------------------------------------------- tasks for your agents (Hermes Kanban)
function Tasks() {
  const t = usePluginI18n(ID)
  const [list, setList] = useState([]); const [title, setTitle] = useState(''); const [text, setText] = useState(''); const [assignee, setAssignee] = useState('')
  const [open, setOpen] = useState(false); const [error, setError] = useState(null); const [busy, setBusy] = useState(false)
  const load = useCallback(() => rest('/auftraege').then((r) => { setList(r.auftraege || []); if (r.fehler) setError(r.fehler) }).catch((e) => setError(errText(e))), [])
  useEffect(() => { load(); const iv = setInterval(load, 20000); return () => clearInterval(iv) }, [load])
  const titleEff = title.trim() || text.trim().split('\n')[0].slice(0, 80)
  const create = async () => {
    if (!titleEff || busy) return
    setBusy(true); setError(null)
    try {
      await rest('/auftrag', { method: 'POST', body: { mandant: MANDANT, titel: titleEff, text, assignee: (PROFILE_LOADED && assignee && !PROFILE.includes(assignee)) ? '' : assignee, kontext: { links: storage.get('links', {}), rechts: storage.get('rechts', {}) } } })
      setTitle(''); setText(''); setOpen(false); load()
    } catch (e) { setError(errText(e)) } finally { setBusy(false) }
  }
  const act = async (id, what) => { setError(null); try { await rest('/auftrag/' + id + '/' + what, { method: 'POST', body: { mandant: MANDANT } }); load() } catch (e) { setError(errText(e)) } }
  const color = (s) => s === 'done' ? 'text-(--ui-text-success)' : s === 'blocked' ? 'text-(--ui-text-warning)' : s === 'running' ? 'text-(--ui-text-info)' : quiet
  return jsxs('div', { className: box + ' p-2 text-xs flex flex-col gap-1 shrink-0', children: [
    jsxs('div', { className: 'flex items-center gap-2', children: [
      jsx('span', { className: 'font-medium', children: t('tasksTitle') }),
      jsx('span', { className: quiet, children: t('nOpen', list.filter((a) => a.status !== 'archived').length) }),
      jsx('span', { className: 'flex-1' }),
      jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setOpen(!open), children: open ? t('close') : t('newTask') })
    ] }),
    open ? jsxs('div', { className: 'flex flex-col gap-1', children: [
      jsx(Input, { placeholder: t('taskTitlePlaceholder'), value: title, onChange: (e) => setTitle(e.target.value), onKeyDown: (e) => { if (e.key === 'Enter') create() } }),
      jsx('textarea', { className: 'rounded border border-(--ui-stroke-secondary) bg-transparent p-1', style: { minHeight: 60 }, placeholder: t('taskTextPlaceholder'), value: text, onChange: (e) => setText(e.target.value) }),
      jsxs('div', { className: 'flex gap-2 items-center', children: [
        jsx('span', { className: quiet, children: t('agent') }),
        jsxs('select', { className: selectCls, value: assignee, onChange: (e) => setAssignee(e.target.value), children: [jsx('option', { value: '', children: t('defaultAgent') }), ...(PROFILE_LOADED ? PROFILE : []).map((p) => jsx('option', { value: p, children: p }, p))] }),
        jsx(Button, { size: 'sm', disabled: busy || !titleEff, onClick: create, children: busy ? t('creating') : !titleEff ? t('titleFirst') : t('createCard') }),
        jsx('span', { className: quiet, children: t('startsAfterRelease') })
      ] })
    ] }) : null,
    error ? jsx('div', { className: 'text-destructive', children: error }) : null,
    ...list.filter((a) => a.status !== 'archived').slice(0, 12).map((a) => jsxs('div', { className: 'flex items-center gap-2', children: [
      jsx('span', { className: 'font-mono ' + color(a.status), children: a.status }),
      jsx('span', { className: 'font-mono', children: a.id }),
      jsx('span', { className: 'flex-1 truncate', title: a.ergebnis_kommentar || a.zusammenfassung || '', children: a.titel + (a.zusammenfassung ? ' — ' + a.zusammenfassung : a.ergebnis_kommentar ? ' — ' + a.ergebnis_kommentar : '') }),
      a.status === 'blocked' ? jsx(Button, { size: 'sm', onClick: () => act(a.id, 'freigeben'), children: t('release') }) : null,
      ['done', 'blocked', 'review'].includes(a.status) ? jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => act(a.id, 'archivieren'), children: t('archive') }) : null
    ] }, a.id))
  ] })
}

// ---------------------------------------------------------------- page
function TrovePage({ only }) {
  const t = usePluginI18n(ID)
  const [sources, setSources] = useState([]); const [status, setStatus] = useState(null)
  const [left, setLeftRaw] = useState(() => storage.get('links', { quelle: '', pfad: '' })); const [right, setRightRaw] = useState(() => storage.get('rechts', { quelle: '', pfad: '' }))
  const [selL, setSelL] = useState([]); const [selR, setSelR] = useState([]); const [reload, setReload] = useState(0); const [run, setRun] = useState(null)
  const [confirm, setConfirm] = useState(null)
  const setLeft = (z) => { storage.set('links', z); setLeftRaw(z) }; const setRight = (z) => { storage.set('rechts', z); setRightRaw(z) }
  // fetch sources; if the backend does not answer (restart), retry for up to 60 s instead of silently losing the source
  const loadSources = useCallback(async () => { for (let i = 0; i < 30; i++) { try { const r = await rest('/quellen'); setSources(r.quellen); return } catch (e) { await new Promise((ok) => setTimeout(ok, 2000)) } } }, [])
  const loadStatus = useCallback(() => rest('/status').then((s) => { MANDANT = s.mandant || MANDANT; PROFILE = Array.isArray(s.profile) && s.profile.length ? s.profile : PROFILE; PROFILE_LOADED = true; setStatus(s) }).catch(() => setStatus({ fehler: true })), [])
  useEffect(() => { loadSources(); loadStatus() }, [])
  useEffect(() => { if (!confirm) return; const iv = setTimeout(() => setConfirm(null), 6000); return () => clearTimeout(iv) }, [confirm])
  const srcOf = (z) => sources.find((x) => x.id === z.quelle)
  const doAction = async (art, dir) => {
    const from = dir === 'lr' ? left : right, to = dir === 'lr' ? right : left, names = dir === 'lr' ? selL : selR
    if (!from.quelle || !to.quelle) { host.notify({ kind: 'warning', message: 'Trove: ' + t('needBothSides') }); return }
    if (art !== 'sync' && names.length === 0) { host.notify({ kind: 'warning', message: 'Trove: ' + t('markFirst') }); return }
    setConfirm(null); setRun({ art, laeuft: true })
    try {
      const r = await rest('/aktion', { method: 'POST', body: { mandant: MANDANT, art, von: { quelle: from.quelle, pfad: from.pfad }, nach: { quelle: to.quelle, pfad: to.pfad }, namen: art === 'sync' ? [] : names }, timeoutMs: 3600000 })
      setRun({ art, ergebnis: r.ergebnis, lauf: r.lauf }); setReload((n) => n + 1); setSelL([]); setSelR([])
      host.notify({ kind: 'info', message: 'Trove: ' + t('actionDone', t('act_' + art), r.lauf) })
    } catch (e) { setRun({ art, fehler: errText(e) }) }
  }
  // Writing is only allowed into folders marked writable; moving also removes the original, so it needs a writable source AND target.
  const move = (art, dir, label) => {
    const from = srcOf(dir === 'lr' ? left : right), to = srcOf(dir === 'lr' ? right : left)
    const reason = !to ? t('pickTargetFirst') : to.nur_lesen ? t('targetReadOnly') : (art === 'verschieben' && from && from.nur_lesen) ? t('sourceReadOnly') : null
    const key = art + dir
    const armed = confirm === key
    const text = armed ? t('confirmMove') : label
    return jsx(Button, { size: 'sm', variant: dir === 'rl' ? 'ghost' : undefined, disabled: !!reason, title: reason || (art === 'verschieben' ? t('moveTip') : art === 'sync' ? t('syncTip') : t('copyTip')),
      onClick: () => art === 'verschieben' && !armed ? setConfirm(key) : doAction(art, dir), children: text }, key)
  }
  const mid = !status || status.fehler ? jsx('div', { 'data-mitte': 'wait', className: 'flex flex-col justify-center items-center px-1 text-xs w-28 text-center ' + quiet, children: status ? '–' : t('loading') })
    : jsxs('div', { className: 'flex flex-col justify-center items-center gap-1 px-1 text-xs w-28 text-center ' + quiet, children: [
      move('kopieren', 'lr', t('copyRight')), move('verschieben', 'lr', t('moveRight')), move('sync', 'lr', t('syncRight')),
      jsx('div', { className: 'h-3' }), move('kopieren', 'rl', t('copyLeft')), move('verschieben', 'rl', t('moveLeft')),
      jsx('div', { className: 'pt-2', children: t('writeHint') })] })
  const result = run ? jsxs('div', { className: box + ' p-2', children: [
    jsx('span', { className: 'font-medium', children: t('act_' + run.art) + (run.laeuft ? ' ' + t('running') : run.fehler ? ' ' + t('errorColon') + ' ' : ' ' + t('done') + (run.lauf ? ' · ' + t('runId', run.lauf) : '')) }),
    run.fehler ? jsx('span', { className: 'text-destructive', children: run.fehler }) : null,
    run.ergebnis ? jsx('span', { className: 'font-mono ml-2', children: run.ergebnis.map((r) => r.name + ': ' + Object.entries(r).filter(([k]) => k !== 'name').map(([k, v]) => k + '=' + v).join(' ')).join(' | ') }) : null
  ] }) : null
  const connection = !status ? t('connecting') : status.fehler ? t('backendDown') : t('backendUp')
  const header = jsxs('div', { 'data-zeile': 'kopf', style: { ...BAR, alignItems: 'flex-start', columnGap: 16 }, children: [
    jsxs('div', { style: { display: 'flex', alignItems: 'baseline', gap: 8, paddingTop: 2 }, children: [
      jsx('span', { style: { fontSize: 18, fontWeight: 600 }, children: 'Trove' }),
      jsx('span', { style: QUIET_STYLE, children: t('clientLabel', (status && status.mandant) || MANDANT || '…') })] }),
    jsx('div', { style: { flex: '1 1 320px', maxWidth: 720, marginLeft: 'auto', minWidth: 0 }, children: jsx(Search, {}) })
  ] })
  const tools = jsxs('div', { 'data-zeile': 'werkzeuge', style: BAR, children: [
    group(t('view'), [btn(t('onlyLeft'), () => host.navigate('/trove/links'), { title: t('onlyLeftTip'), on: only === 'links' }),
      btn(t('onlyRight'), () => host.navigate('/trove/rechts'), { title: t('onlyRightTip'), on: only === 'rechts' }),
      btn(t('both'), () => host.navigate('/trove'), { title: t('bothTip'), on: !only })])
  ] })
  const messages = jsxs('div', { 'data-zeile': 'meldungen', role: 'status', style: { display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12 }, children: [
    jsxs('div', { style: BAR, children: [
      jsx(Badge, { children: connection }),
      status && status.protokolle ? jsx('span', { style: QUIET_STYLE, children: t('receiptLogLine', status.protokolle.map((p) => p.mandant + ': ' + p.zeilen).join(' · ')) }) : null,
      status && status.fehler ? jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => { host.notify({ kind: 'info', message: 'Trove: ' + t('restarting') }); host.restartGateway() }, children: t('restartBackend') }) : null
    ] }),
    result
  ] })
  const sideL = jsx(Side, { side: 'links', sources, state: left, setState: setLeft, selection: selL, setSelection: setSelL, reload, onSourcesChanged: loadSources })
  const sideR = jsx(Side, { side: 'rechts', sources, state: right, setState: setRight, selection: selR, setSelection: setSelR, reload, onSourcesChanged: loadSources })
  const tasks = jsx(Tasks, {})
  const rootCls = 'flex h-full min-h-0 w-full min-w-0 flex-col gap-2 p-3 overflow-auto'
  const field = (inner) => jsx('div', { className: 'flex-1 flex flex-col min-w-0', style: { minHeight: 560 }, children: inner })
  const column = (inner) => jsx('div', { style: { flex: '1 1 300px', minWidth: 0, display: 'flex', flexDirection: 'column' }, children: inner })
  if (only === 'links') return jsxs('div', { className: rootCls, children: [header, tools, messages, field(sideL), tasks] })
  if (only === 'rechts') return jsxs('div', { className: rootCls, children: [header, tools, messages, field(sideR), tasks] })
  return jsxs('div', { className: rootCls, children: [header, tools, messages,
    // columns wrap below each other in a narrow window instead of overflowing sideways
    jsxs('div', { 'data-spalten': true, style: { flex: '1 0 auto', display: 'flex', flexWrap: 'wrap', gap: 8, minWidth: 0, minHeight: 560 }, children: [column(sideL), mid, column(sideR)] }), tasks] })
}

export default {
  id: ID,
  name: 'Trove',
  description: 'File hub: two panes with preview; copy/move/sync through a hash-chained receipt log; read, split and trace files; build knowledge cards in your wiki.',
  register(ctx) {
    rest = ctx.rest; storage = ctx.storage; files = ctx.files || null
    T = (k, ...a) => ctx.i18n.t(k, ...a)
    ctx.i18n.register({ en: EN, de: DE })
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/trove' }, render: () => jsx(TrovePage, {}) },
      { id: 'page-links', area: ROUTES_AREA, data: { path: '/trove/links' }, render: () => jsx(TrovePage, { only: 'links' }) },
      { id: 'page-rechts', area: ROUTES_AREA, data: { path: '/trove/rechts' }, render: () => jsx(TrovePage, { only: 'rechts' }) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, order: 71, data: { codicon: 'archive', label: 'Trove', path: '/trove' } },
      { id: 'open', area: PALETTE_AREA, data: { id: 'trove.open', label: T('paletteOpen'), keywords: ['trove', 'files', 'receipts'], run: () => host.navigate('/trove') } }
    ])
  }
}

// ---------------------------------------------------------------- strings (English is the default; German is the second language)
// Keys used in code: t("key", ...args). Function values are interpolators. Resolution: active locale -> en -> raw key.
// <i18n:en>
const EN = {
  paletteOpen: 'Trove: open the file hub',
  connecting: 'connecting …', backendUp: 'Backend connected', backendDown: 'Backend not reachable', restartBackend: 'Restart backend', restarting: 'Restarting the backend …',
  receiptLogLine: (s) => 'Receipt log ' + s, clientLabel: (n) => 'Client ' + n,
  view: 'View', onlyLeft: 'left only', onlyRight: 'right only', both: 'both',
  onlyLeftTip: 'Show only the left pane (e.g. in a second window)', onlyRightTip: 'Show only the right pane (e.g. in a second window)', bothTip: 'Show both panes side by side',
  loading: 'loading …', running: 'running …', done: 'done', errorColon: 'error:', runId: (id) => 'run ' + id, actionDone: (what, id) => what + ' finished (' + id + ')',
  act_kopieren: 'Copy', act_verschieben: 'Move', act_sync: 'Sync', act_lesen: 'Read', act_stapel: 'Split', act_spur: 'Trace',
  needBothSides: 'both sides need a source.', markFirst: 'tick some entries first.',
  copyRight: 'Copy →', moveRight: 'Move →', syncRight: 'Sync →', copyLeft: '← Copy', moveLeft: '← Move',
  copyTip: 'Copy the ticked entries to the other side, verified by checksum read-back', moveTip: 'Move the ticked entries; the original is deleted only after the copy is verified',
  syncTip: 'Compare the whole left folder with the right one: copy missing files, never overwrite',
  confirmMove: 'Click again: delete originals after copy',
  pickTargetFirst: 'choose a target source first', targetReadOnly: 'the target is read-only (mark it writable first)', sourceReadOnly: 'moving deletes originals, so the source must be writable too',
  writeHint: 'Trove only writes into folders you marked writable.',
  sourceLeft: 'Source (left)', targetRight: 'Target (right)', chooseSource: '– choose a source –', readOnlyTag: '(read-only)', writableTag: '(writable)',
  removeSource: 'Remove this source from Trove (files stay untouched)',
  makeWritable: 'Make writable', makeWritableTip: 'Allow Trove to write into this folder (copy, move, sync, knowledge twins)', makeReadOnly: 'Make read-only', makeReadOnlyTip: 'Stop Trove from writing into this folder',
  writableWarn: (n) => 'Allow Trove to write into "' + n + '"? Copy, move (deletes originals after verification) and sync will work there.', yesWritable: 'Yes, make writable',
  addFolder: '+ Folder', addHint: 'Add a folder from this computer. Folders are read-only until you mark them writable.', pathPlaceholder: 'Folder path, e.g. ~/Documents/Receipts', namePlaceholder: 'Name (optional)',
  browse: 'Browse …', writableCheck: 'Writable (Trove may copy/move into it)', addBtn: 'Add folder', checking: 'checking …', cancel: 'Cancel',
  noSource: 'No source chosen.', pickSourceFirst: 'choose a source first', sourceLoading: 'source loading …', notNow: (r) => 'Not possible right now: ' + r,
  indexed: 'Index ✓', indexFiles: 'Index files', indexTip: 'Compute checksums (detects duplicates)', registerDone: (n, g) => 'index: ' + n + ' new, ' + g + ' total',
  checkChanges: 'Check changes', checkChangesTip: 'Take a snapshot and compare it with the last run (new / gone / moved / changed outside Trove)',
  checkingChanges: 'Checking changes …', changesRun: (id) => 'Run ' + id, baselineTaken: '(baseline recorded)',
  fAll: 'all', fDup: 'duplicates only', fNoDup: 'without duplicates', foundNTimes: (n) => 'found ' + n + '×',
  intoKnowledge: 'Into knowledge', intoKnowledgeAll: 'Into knowledge (whole folder)', intoKnowledgeN: (n) => 'Into knowledge (' + n + ' ticked)',
  intoKnowledgeTip: 'Ticked files (otherwise the whole folder) into your knowledge wiki: one markdown twin per file, bound to the original by checksum. Originals stay unchanged.',
  entriesMarked: (n, m) => n + ' entries, ' + m + ' ticked', read: 'Read', split: 'Split', all: 'all', clearSelection: 'Clear selection',
  readTip: 'Reading ladder (e-invoice, text layer, otherwise local OCR) and fixed plausibility checks; writes a twin and a receipt-log entry',
  splitTip: 'Split a multi-page PDF into single documents (needs Stirling-PDF)', clickFileFirst: 'click a file first',
  clickForPreview: 'Click a file for a preview. Double-click shows its trace; double-click a folder opens it.', folderLabel: (n) => 'Folder ' + n, loadingPreview: 'loading preview …',
  pageOf: (p, n) => 'page ' + p + ' / ' + n, truncated: '… (truncated)', noPreview: 'No preview',
  wholeFolder: (f, n) => 'WHOLE folder "' + f + '" (' + n + ' files, including subfolders)', selectedFiles: (n, names) => n + ' selected file(s): ' + names,
  knowledgeExplain: 'into knowledge: one markdown twin per file (bound to the original by checksum) plus one knowledge card in your wiki (status: to-review) that records the original path. Originals stay unchanged.',
  affiliationPlaceholder: 'Affiliation (optional, e.g. My company)', affiliationTip: 'Optional. Empty = "offen" (open).', makeCards: 'Create knowledge cards',
  noMandatory: (c, a) => 'No mandatory fields: without input the collection is "' + c + '" and the affiliation "' + a + '".',
  intoWhole: 'Into knowledge (whole folder)', yesWhole: (n) => 'Yes, whole folder (' + n + ' files)', intoSelected: (n) => 'Into knowledge (' + n + ' selected)',
  more: 'More …', less: 'Less', collectionPlaceholder: (c) => 'Collection (empty = ' + c + ')', followCheck: 'Remember folder for the continuous run',
  followTip: 'Writes the folder to Trove\'s watch list. Only an optional scheduler job (tools/installieren.py --dauerlauf) ever uses it; see the README.',
  wholeWarn: (n) => 'Nothing ticked: the WHOLE folder will be processed (' + n + ' files, with subfolders). Tick files to process only those.',
  readingNow: 'reading, writing markdown, building the index … (OCR takes a few seconds per page)',
  summary: (f, n, s, d, k, e) => f + ' files: ' + n + ' new, ' + s + ' already there, ' + d + ' duplicates, ' + k + ' cards, ' + e + ' errors',
  followed: 'folder remembered for the continuous run', qmdState: (s) => 'index: ' + s, qmdSkipped: 'skipped (qmd not installed)', inIndex: 'in index',
  errorWord: 'ERROR', cardWord: 'card', noCard: 'no card', stNew: 'new', stThere: 'already there', stDup: 'duplicate', stError: 'error',
  runningOcr: 'running … (OCR can take a few seconds per page)', method: 'Method', nPages: (n) => n + ' pages', verification: 'Verification',
  passedSure: 'passed (sure)', passed: 'passed', needsReview: 'needs review', twin: 'Twin', batch: 'Batch', splitSummary: (n, p, s) => n + ' documents from ' + p + ' pages, ' + s + ' s',
  pagesRange: (r) => 'pages ' + r, bookingVersion: 'booking version without attachments', backToPreview: 'back to preview',
  traceTitle: (f) => 'Trace (receipt log): ' + f, art_lesen: 'Read', art_stapel: 'Split',
  trace_direkt: 'direct', trace_rueckwaerts: 'backwards', trace_vorwaerts: 'forwards', trace_gleicher_inhalt: 'same content elsewhere',
  searchPlaceholder: 'Search the index and the receipt log: file name, invoice number, sender, sha256 …', search: 'Search', collapse: 'hide', expand: 'show', searching: 'searching …',
  foundUnder: (w, n) => 'located ' + w + ' (' + n + '):', receiptLogN: (n) => 'Receipt log (' + n + '):',
  tasksTitle: 'Tasks for your agents (Hermes Kanban)', nOpen: (n) => n + ' open', close: 'close', newTask: '+ Task',
  taskTitlePlaceholder: 'Title (e.g. review the batch in the left source and list duplicates)', taskTextPlaceholder: 'Task text: what exactly should be done. The card stays blocked until you release it.',
  agent: 'Agent:', defaultAgent: '(Hermes default)', creating: 'creating …', titleFirst: 'enter a title or text first', createCard: 'Create card (blocked)',
  startsAfterRelease: 'The agent starts only after you press "Release" in the list.', release: 'Release', archive: 'Archive'
}
// </i18n:en>
// <i18n:de>
const DE = {
  paletteOpen: 'Trove: Datei-Zentrale öffnen',
  connecting: 'verbinde …', backendUp: 'Backend verbunden', backendDown: 'Backend nicht erreichbar', restartBackend: 'Backend neu starten', restarting: 'Backend wird neu gestartet …',
  receiptLogLine: (s) => 'Belegprotokoll ' + s, clientLabel: (n) => 'Mandant ' + n,
  view: 'Ansicht', onlyLeft: 'nur links', onlyRight: 'nur rechts', both: 'beide',
  onlyLeftTip: 'Nur die linke Seite zeigen (z. B. im zweiten Fenster)', onlyRightTip: 'Nur die rechte Seite zeigen (z. B. im zweiten Fenster)', bothTip: 'Beide Seiten nebeneinander',
  loading: 'lädt …', running: 'läuft …', done: 'fertig', errorColon: 'Fehler:', runId: (id) => 'Lauf ' + id, actionDone: (what, id) => what + ' fertig (' + id + ')',
  act_kopieren: 'Kopieren', act_verschieben: 'Verschieben', act_sync: 'Sync', act_lesen: 'Lesen', act_stapel: 'Zerlegen', act_spur: 'Spur',
  needBothSides: 'beide Seiten brauchen eine Quelle.', markFirst: 'erst Einträge markieren.',
  copyRight: 'Kopieren →', moveRight: 'Verschieben →', syncRight: 'Sync →', copyLeft: '← Kopieren', moveLeft: '← Verschieben',
  copyTip: 'Markierte Einträge auf die andere Seite kopieren, per Prüfsummen-Gegenprobe verifiziert', moveTip: 'Markierte Einträge verschieben; das Original wird erst nach verifizierter Kopie gelöscht',
  syncTip: 'Ganzen linken Ordner mit dem rechten abgleichen: fehlende Dateien kopieren, nie überschreiben',
  confirmMove: 'Nochmal klicken: Originale nach Kopie löschen',
  pickTargetFirst: 'erst eine Ziel-Quelle wählen', targetReadOnly: 'das Ziel ist schreibgeschützt (erst „schreibbar“ machen)', sourceReadOnly: 'Verschieben löscht Originale, also muss auch die Quelle schreibbar sein',
  writeHint: 'Trove schreibt nur in Ordner, die du als schreibbar markiert hast.',
  sourceLeft: 'Quelle (links)', targetRight: 'Ziel (rechts)', chooseSource: '– Quelle wählen –', readOnlyTag: '(nur lesen)', writableTag: '(schreibbar)',
  removeSource: 'Diese Quelle aus Trove entfernen (Dateien bleiben unberührt)',
  makeWritable: 'Schreibbar machen', makeWritableTip: 'Trove erlauben, in diesen Ordner zu schreiben (Kopieren, Verschieben, Sync, Wissens-Zwillinge)', makeReadOnly: 'Nur lesen', makeReadOnlyTip: 'Trove das Schreiben in diesen Ordner verbieten',
  writableWarn: (n) => 'Trove erlauben, in „' + n + '“ zu schreiben? Kopieren, Verschieben (löscht Originale nach Prüfung) und Sync funktionieren dort dann.', yesWritable: 'Ja, schreibbar machen',
  addFolder: '+ Ordner', addHint: 'Ordner von diesem Rechner hinzufügen. Ordner sind schreibgeschützt, bis du sie als schreibbar markierst.', pathPlaceholder: 'Ordnerpfad, z. B. ~/Dokumente/Belege', namePlaceholder: 'Name (optional)',
  browse: 'Durchsuchen …', writableCheck: 'Schreibbar (Trove darf hineinkopieren/-verschieben)', addBtn: 'Ordner hinzufügen', checking: 'prüfe …', cancel: 'Abbrechen',
  noSource: 'Keine Quelle gewählt.', pickSourceFirst: 'erst Quelle wählen', sourceLoading: 'Quelle lädt …', notNow: (r) => 'Gerade nicht möglich: ' + r,
  indexed: 'Index ✓', indexFiles: 'Indexieren', indexTip: 'Prüfsummen rechnen (erkennt Dubletten)', registerDone: (n, g) => 'Index: ' + n + ' neu, ' + g + ' gesamt',
  checkChanges: 'Änderungen prüfen', checkChangesTip: 'Stand aufnehmen und mit dem letzten Lauf vergleichen (neu / weg / verschoben / außerhalb von Trove geändert)',
  checkingChanges: 'Änderungen werden geprüft …', changesRun: (id) => 'Lauf ' + id, baselineTaken: '(Grundstand aufgenommen)',
  fAll: 'alle', fDup: 'nur Dubletten', fNoDup: 'ohne Dubletten', foundNTimes: (n) => n + '× vorhanden',
  intoKnowledge: 'Ins Wissen', intoKnowledgeAll: 'Ins Wissen (ganzer Ordner)', intoKnowledgeN: (n) => 'Ins Wissen (' + n + ' angehakt)',
  intoKnowledgeTip: 'Angehakte Dateien (sonst den ganzen Ordner) ins Wissens-Wiki: je Datei ein Markdown-Zwilling, per Prüfsumme ans Original gebunden. Originale bleiben unverändert.',
  entriesMarked: (n, m) => n + ' Einträge, ' + m + ' markiert', read: 'Lesen', split: 'Zerlegen', all: 'alle', clearSelection: 'Auswahl leeren',
  readTip: 'Lese-Treppe (E-Rechnung, Textlayer, sonst lokale OCR) und feste Plausibilitätsprüfung; schreibt Zwilling und Belegprotokoll-Eintrag',
  splitTip: 'Mehrseitiges PDF in einzelne Belege zerlegen (braucht Stirling-PDF)', clickFileFirst: 'erst eine Datei anklicken',
  clickForPreview: 'Datei anklicken für die Vorschau. Doppelklick zeigt die Spur, Doppelklick auf einen Ordner öffnet ihn.', folderLabel: (n) => 'Ordner ' + n, loadingPreview: 'Vorschau lädt …',
  pageOf: (p, n) => 'Seite ' + p + ' / ' + n, truncated: '… (gekürzt)', noPreview: 'Keine Vorschau',
  wholeFolder: (f, n) => 'GANZER Ordner „' + f + '“ (' + n + ' Dateien, auch Unterordner)', selectedFiles: (n, names) => n + ' ausgewählte Datei(en): ' + names,
  knowledgeExplain: 'ins Wissen: je Datei ein Markdown-Zwilling (per Prüfsumme ans Original gebunden) plus eine Wissenskarte im Wiki (Status: to-review) mit dem Originalpfad. Originale bleiben unverändert.',
  affiliationPlaceholder: 'Zugehörigkeit (optional, z. B. Meine Firma)', affiliationTip: 'Optional. Leer = „offen“.', makeCards: 'Wissenskarten erzeugen',
  noMandatory: (c, a) => 'Keine Pflichtfelder: ohne Angaben Sammlung „' + c + '“, Zugehörigkeit „' + a + '“.',
  intoWhole: 'Ins Wissen (ganzer Ordner)', yesWhole: (n) => 'Ja, ganzen Ordner (' + n + ' Dateien)', intoSelected: (n) => 'Ins Wissen (' + n + ' ausgewählt)',
  more: 'Mehr …', less: 'Weniger', collectionPlaceholder: (c) => 'Sammlung (leer = ' + c + ')', followCheck: 'Ordner für den Dauerlauf vormerken',
  followTip: 'Trägt den Ordner in Troves Beobachtungsliste ein. Nur ein optionaler Zeitplan-Job (tools/installieren.py --dauerlauf) nutzt sie; siehe README.',
  wholeWarn: (n) => 'Nichts angehakt: der GANZE Ordner wird verarbeitet (' + n + ' Dateien, mit Unterordnern). Häkchen setzen, um nur diese zu verarbeiten.',
  readingNow: 'liest, schreibt Markdown, baut den Index … (OCR braucht je Seite einige Sekunden)',
  summary: (f, n, s, d, k, e) => f + ' Dateien: ' + n + ' neu, ' + s + ' schon da, ' + d + ' Dubletten, ' + k + ' Karten, ' + e + ' Fehler',
  followed: 'Ordner für den Dauerlauf vorgemerkt', qmdState: (s) => 'Index: ' + s, qmdSkipped: 'übersprungen (qmd nicht installiert)', inIndex: 'im Index',
  errorWord: 'FEHLER', cardWord: 'Karte', noCard: 'keine Karte', stNew: 'neu', stThere: 'schon da', stDup: 'Dublette', stError: 'Fehler',
  runningOcr: 'läuft … (OCR kann je Seite einige Sekunden brauchen)', method: 'Methode', nPages: (n) => n + ' Seiten', verification: 'Verprobung',
  passedSure: 'bestanden (sicher)', passed: 'bestanden', needsReview: 'Prüfliste', twin: 'Zwilling', batch: 'Stapel', splitSummary: (n, p, s) => n + ' Belege aus ' + p + ' Seiten, ' + s + ' s',
  pagesRange: (r) => 'Seiten ' + r, bookingVersion: 'Buchungsfassung ohne Anhang', backToPreview: 'zurück zur Vorschau',
  traceTitle: (f) => 'Spur (Belegprotokoll): ' + f, art_lesen: 'Lesen', art_stapel: 'Zerlegen',
  trace_direkt: 'direkt', trace_rueckwaerts: 'rückwärts', trace_vorwaerts: 'vorwärts', trace_gleicher_inhalt: 'gleicher Inhalt anderswo',
  searchPlaceholder: 'Index und Belegprotokoll durchsuchen: Dateiname, Rechnungsnummer, Absender, sha256 …', search: 'Suchen', collapse: 'zu', expand: 'auf', searching: 'suche …',
  foundUnder: (w, n) => 'liegt ' + w + ' (' + n + '):', receiptLogN: (n) => 'Belegprotokoll (' + n + '):',
  tasksTitle: 'Aufträge an deine Agenten (Hermes Kanban)', nOpen: (n) => n + ' offen', close: 'schließen', newTask: '+ Auftrag',
  taskTitlePlaceholder: 'Titel (z. B. Belegstapel in der linken Quelle sichten und Dubletten listen)', taskTextPlaceholder: 'Auftragstext: was genau getan werden soll. Die Karte bleibt gesperrt, bis du sie freigibst.',
  agent: 'Agent:', defaultAgent: '(Hermes-Standard)', creating: 'lege an …', titleFirst: 'erst Titel oder Text eingeben', createCard: 'Karte anlegen (gesperrt)',
  startsAfterRelease: 'Der Agent startet erst nach „Freigeben“ in der Liste.', release: 'Freigeben', archive: 'Archivieren'
}
// </i18n:de>
