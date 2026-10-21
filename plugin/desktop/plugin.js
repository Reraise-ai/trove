// Trove — Hermes-Desktop-Tab (Disk-Plugin, kein Build)
// Zwei Seiten (links Quelle, rechts Ziel), Vorschau auf beiden Seiten, Aktionen laufen ueber das Belegprotokoll (F8).
// Je Datei: Lesen (F3 Treppe + F4 Verprobung), Zerlegen (F6 Stapel), Historie (F11 ERPNext-Spiegel), Unterschrift (F7 DocuSeal),
// zu Reraise (F16). Ordner: Ins Wissen (F16: Markdown-Zwilling, QMD-Sammlung, Beleg-Karten privat/oeffentlich),
// zu Reraise hochladen (nur oeffentliche Zwillinge).
// Kopf: Suche ueber beide Welten (F15), Mail abholen (F5), ERPNext-Spiegel (F11), Unterschriften holen (F7).
// Multimonitor: Routen /trove/links und /trove/rechts zeigen je eine Seite; ein zweites Hermes-Fenster kann sie auf dem
// zweiten Monitor anzeigen (gleicher Zustand ueber ctx.storage).
import { host, Button, Badge, Input, SandboxedFrame, ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA } from '@hermes/plugin-sdk'
import { useState, useEffect, useCallback, useRef } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'trove'
let rest = null
let storage = null
let MANDANT = ''      // kommt vom Backend (GET /status), keine zweite Wahrheit
let KARTENZIEL = 'wiki'  // fail-closed wie der Server: Kundenmodus, bis /status ausdruecklich brett meldet
// Knoepfe, die je Einrichtung fehlen koennen: erst nach GET /status zeigen (kein Aufblitzen im Kundenmodus).
// reraise: status.reraise_knopf (fehlt = alte Fassung = an); mail/erpnext: im Kundenmodus nur, wenn eingerichtet
let ZEIGE = { docuseal: false, reraise: false, mail: false, erpnext: false }
let PROFILE = ['default']
let HERMES_CLI = true
let PROFILE_GELADEN = false

let WISSEN_EINZELN = false  // Backend kann Einzeldateien (status.wissen_einzeln); sonst Auswahl sperren, nie still den ganzen Ordner
let WISSEN_OHNE_PFLICHT = false  // Backend setzt fehlende Sammlung/Zugehoerigkeit selbst (status.wissen_ohne_pflicht)
const SAMMLUNG_STANDARD = 'quellen'  // Ins Wissen ohne Sammlung -> raw/trove/quellen, Karte wiki/quellen
const ZUG_OFFEN = 'offen'  // Ins Wissen ohne Zugehoerigkeit: als offen gekennzeichnet, die Belegbuchung fragt spaeter nach

function statusUebernehmen(s) { MANDANT = s.mandant || MANDANT; KARTENZIEL = s.kartenziel === 'brett' ? 'brett' : 'wiki'; ZEIGE = { docuseal: KARTENZIEL !== 'wiki', reraise: s.reraise_knopf !== false, mail: KARTENZIEL !== 'wiki' || s.postfach === true, erpnext: KARTENZIEL !== 'wiki' || s.erpnext === true }; WISSEN_EINZELN = s.wissen_einzeln === true; WISSEN_OHNE_PFLICHT = s.wissen_ohne_pflicht === true; PROFILE = s.profile || ['default']; PROFILE_GELADEN = Array.isArray(s.profile) && s.profile.length > 0; HERMES_CLI = s.hermes_cli !== false; }

// Backend-Neuladen: EIN Zustand fuer alle Teile. Knoepfe bleiben immer sichtbar, sind waehrenddessen gesperrt und nennen den Grund.
let BACKEND_LAEDT = false
const backendHoerer = new Set()
function backendMelden(laedt) { BACKEND_LAEDT = laedt; for (const f of [...backendHoerer]) { try { f(laedt) } catch (e) { /* Hoerer weg */ } } }
function useBackendLaedt() {
  const [b, setB] = useState(BACKEND_LAEDT)
  useEffect(() => { const f = (x) => setB(x); backendHoerer.add(f); setB(BACKEND_LAEDT); return () => { backendHoerer.delete(f) } }, [])
  return b
}
async function backendNeuLaden() {
  const d = typeof window !== 'undefined' ? window.hermesDesktop : null
  if (!d || typeof d.recycleBackend !== 'function') return 'Diese Hermes-Version kann das Backend nicht aus dem Plugin neu laden.'
  if (BACKEND_LAEDT) return 'Backend wird schon neu geladen …'
  backendMelden(true)
  try {
    try { await d.recycleBackend() } catch (e) { return 'Fehler: ' + String(e && e.message || e) }
    for (let i = 0; i < 45; i++) {
      await new Promise((ok) => setTimeout(ok, 2000))
      try { const st = await rest('/status'); if (st.wissen_einzeln === true && st.wissen_ohne_pflicht === true) { statusUebernehmen(st); return null } } catch (e) { /* Backend startet noch */ }
    }
    return 'Backend antwortet nach 90 s noch nicht mit der neuen Fassung.'
  } finally { backendMelden(false) }
}
const fmtB = (n) => n == null ? '–' : n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : n < 1073741824 ? (n / 1048576).toFixed(1) + ' MB' : (n / 1073741824).toFixed(2) + ' GB'
const fmtT = (t) => t ? new Date(t * 1000).toLocaleString('de-DE') : ''
const rahmen = 'rounded border border-(--ui-stroke-secondary)'
const leise = 'text-(--ui-text-tertiary)'
// Leisten nur mit Inline-Stilen: der Host liefert nur die Tailwind-Klassen, die er selbst benutzt (z. B. fehlt die Klasse fuer 11-px-Schrift)
const LEISTE = { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8, minWidth: 0 }
const LEISE_STIL = { fontSize: 12, color: 'var(--ui-text-tertiary)' }
const GRUPPE = { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 4, padding: '2px 6px', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 4, minWidth: 0 }
// Benannte Knopfgruppe (nur Werkzeugzeile oben); ohne sichtbare Knoepfe faellt die ganze Gruppe weg (Kundenmodus blendet aus statt grau)
// Knoepfe, die nur gemeinsam umbrechen
const PAAR = { display: 'flex', gap: 4, whiteSpace: 'nowrap' }
function gruppe(name, knoepfe) {
  const da = knoepfe.filter(Boolean)
  return da.length ? jsxs('div', { role: 'group', 'aria-label': name, 'data-gruppe': name, style: GRUPPE, children: [jsx('span', { style: { ...LEISE_STIL, fontSize: 11, marginRight: 2 }, children: name }), ...da] }, name) : null
}
// Kleiner Leistenknopf; an = aktive Ansicht, disabled mit title = Grund
function knopf(text, onClick, o = {}) {
  return jsx(Button, { size: 'sm', variant: o.an ? 'secondary' : 'ghost', title: o.title, disabled: o.disabled, onClick, children: text })
}

function Zeile(label, wert) {
  return jsxs('div', { className: 'flex justify-between gap-3 text-xs', children: [jsx('span', { className: leise, children: label }), jsx('span', { className: 'font-mono truncate', title: String(wert ?? ''), children: String(wert ?? '–') })] })
}

const CLOUD = {
  webdav: ['Nextcloud / WebDAV', [['url', 'Adresse, z. B. https://cloud.example.com/remote.php/dav/files/NAME'], ['vendor', 'nextcloud, owncloud oder other'], ['user', 'Benutzer'], ['pass', 'Passwort']]],
  sftp: ['SFTP', [['host', 'Server'], ['port', 'Port (leer = 22)'], ['user', 'Benutzer'], ['pass', 'Passwort']]],
  mega: ['Mega', [['user', 'E-Mail'], ['pass', 'Passwort']]],
  drive: ['Google Drive', []],
  icloud: ['iCloud Drive', [['apple_id', 'Apple-ID'], ['password', 'Passwort']]]
}

function CloudAnbinden({ onFertig }) {
  const [typ, setTyp] = useState('webdav'); const [name, setName] = useState(''); const [felder, setFelder] = useState({})
  const [fehler, setFehler] = useState(null); const [laeuft, setLaeuft] = useState(false)
  const anbinden = async () => {
    setLaeuft(true); setFehler(null)
    try { await rest('/cloud', { method: 'POST', body: { typ, name, felder }, timeoutMs: 700000 }); onFertig(true) } catch (e) { setFehler(String(e && e.message || e)) } finally { setLaeuft(false) }
  }
  return jsxs('div', { className: 'flex flex-col gap-2', children: [
    jsx('select', { className: 'rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1', value: typ, onChange: (e) => { setTyp(e.target.value); setFelder({}) }, children: Object.keys(CLOUD).map((k) => jsx('option', { value: k, children: CLOUD[k][0] }, k)) }),
    jsx(Input, { placeholder: 'Name für diesen Speicher (Buchstaben/Ziffern, z. B. meinecloud)', value: name, onChange: (e) => setName(e.target.value) }),
    ...CLOUD[typ][1].map((f) => jsx(Input, { type: f[0].indexOf('pass') >= 0 ? 'password' : 'text', placeholder: f[1], value: felder[f[0]] || '', onChange: (e) => setFelder({ ...felder, [f[0]]: e.target.value }) }, f[0])),
    typ === 'drive' ? jsx('div', { className: leise, children: 'Beim Anbinden öffnet sich auf diesem Rechner der Browser zur Google-Anmeldung.' }) : null,
    fehler ? jsx('div', { className: 'text-destructive', children: fehler }) : null,
    jsx('div', { children: jsx(Button, { size: 'sm', disabled: laeuft || !name, onClick: anbinden, children: laeuft ? 'binde an …' : 'Anbinden und prüfen' }) })
  ] })
}

const ICLOUD_HINWEIS = 'iCloud: in den Systemeinstellungen iCloud Drive einschalten, dann den Ordner iCloud Drive hier wählen. Dateien nur in der Cloud vorher im Finder ‚Jetzt laden‘.'

// Kundenmodus: statt rclone (fehlt dort, und „Ins Wissen“ geht nur aus lokalem Ordner) der lokale iCloud-Drive-Ordner des Macs
function ICloudWaehlen({ onGewaehlt }) {
  const [meldung, setMeldung] = useState(null); const [laeuft, setLaeuft] = useState(false)
  const waehlen = async () => {
    setLaeuft(true); setMeldung(null)
    try {
      const s = await rest('/status')
      if (s.icloud_drive) onGewaehlt(s.icloud_drive)
      else setMeldung('iCloud Drive ist auf diesem Rechner nicht eingeschaltet (Ordner fehlt). Systemeinstellungen → Apple-ID → iCloud → iCloud Drive einschalten, dann erneut wählen.')
    } catch (e) { setMeldung(String(e && e.message || e)) } finally { setLaeuft(false) }
  }
  return jsxs('div', { className: 'flex flex-col gap-2', children: [
    jsx('div', { children: ICLOUD_HINWEIS }),
    jsx('div', { children: jsx(Button, { size: 'sm', disabled: laeuft, onClick: waehlen, children: laeuft ? 'suche …' : 'iCloud Drive wählen' }) }),
    meldung ? jsx('div', { className: 'text-destructive', children: meldung }) : null
  ] })
}

function QuelleAnlegen({ remotes, onFertig }) {
  const kunde = KARTENZIEL === 'wiki'
  const [art, setArt] = useState('ordner'); const [name, setName] = useState(''); const [pfad, setPfad] = useState(''); const [remote, setRemote] = useState(remotes[0] || '')
  const [fehler, setFehler] = useState(null); const [laeuft, setLaeuft] = useState(false); const [beschreibbar, setBeschreibbar] = useState(false)
  const anlegen = async () => {
    setLaeuft(true); setFehler(null)
    try { await rest('/quellen', { method: 'POST', body: { mandant: MANDANT, art, name, pfad, remote, nur_lesen: !(beschreibbar && art === 'ordner' && KARTENZIEL !== 'wiki') } }); onFertig(true) } catch (e) { setFehler(String(e && e.message || e)) } finally { setLaeuft(false) }
  }
  return jsxs('div', { className: rahmen + ' p-2 flex flex-col gap-2 text-xs', children: [
    jsxs('div', { className: 'flex gap-2 items-center', children: [
      jsx('span', { className: leise, children: 'Neue Quelle:' }),
      jsx(Button, { size: 'sm', variant: art === 'ordner' ? 'default' : 'ghost', onClick: () => setArt('ordner'), children: 'Ordner' }),
      kunde ? null : jsx(Button, { size: 'sm', variant: art === 'rclone' ? 'default' : 'ghost', onClick: () => setArt('rclone'), children: 'Online-Drive (rclone)' }),
      jsx(Button, { size: 'sm', variant: art === 'cloud' ? 'default' : 'ghost', onClick: () => setArt('cloud'), children: kunde ? 'iCloud Drive' : 'Cloud-Speicher anbinden' })
    ] }),
    art === 'cloud' && kunde ? jsx(ICloudWaehlen, { onGewaehlt: (p) => { setArt('ordner'); setPfad(p); if (!name) setName('iCloud Drive') } }) : null,
    art === 'cloud' && !kunde ? jsx(CloudAnbinden, { onFertig: (ok) => { if (ok) { setArt('rclone'); onFertig(true) } } }) : null,
    art === 'cloud' ? null : jsx(Input, { placeholder: 'Anzeigename (optional)', value: name, onChange: (e) => setName(e.target.value) }),
    art === 'cloud' ? null : art === 'ordner'
      ? jsx(Input, { placeholder: 'Ordnerpfad, z. B. ~/Documents/Ablage', value: pfad, onChange: (e) => setPfad(e.target.value) })
      : jsxs('div', { className: 'flex gap-2', children: [
          jsx('select', { className: 'rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1', value: remote, onChange: (e) => setRemote(e.target.value), children: remotes.map((r) => jsx('option', { value: r, children: r + ':' }, r)) }),
          jsx(Input, { placeholder: 'Unterordner im Drive (optional)', value: pfad, onChange: (e) => setPfad(e.target.value) })
        ] }),
    art === 'ordner' && KARTENZIEL !== 'wiki' ? jsxs('label', { className: 'flex gap-1 items-center ' + leise, children: [jsx('input', { type: 'checkbox', checked: beschreibbar, onChange: (e) => setBeschreibbar(e.target.checked) }), 'beschreibbar (Ziel für Kopieren/Verschieben; sonst nur lesen)'] }) : null,
    remotes.length === 0 && art === 'rclone' ? jsx('div', { className: leise, children: 'Kein Online-Speicher angebunden: oben „Cloud-Speicher anbinden“ wählen (Nextcloud, SFTP, Mega, Google Drive, iCloud).' }) : null,
    fehler ? jsx('div', { className: 'text-destructive', children: fehler }) : null,
    art === 'cloud' ? null : jsxs('div', { className: 'flex gap-2', children: [jsx(Button, { size: 'sm', disabled: laeuft, onClick: anlegen, children: laeuft ? 'prüfe …' : 'Einbinden' }), jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => onFertig(false), children: 'Abbrechen' })] })
  ] })
}

function Vorschau({ quelle, eintrag }) {
  const [v, setV] = useState(null); const [seite, setSeite] = useState(0)
  useEffect(() => { setSeite(0) }, [quelle, eintrag && eintrag.rel])
  useEffect(() => {
    setV(null)
    if (!quelle || !eintrag || eintrag.ordner) return
    let aktiv = true
    rest('/vorschau?quelle=' + encodeURIComponent(quelle) + '&pfad=' + encodeURIComponent(eintrag.rel) + '&seite=' + seite).then((r) => { if (aktiv) setV(r) }).catch((e) => { if (aktiv) setV({ art: 'fehler', hinweis: String(e && e.message || e) }) })
    return () => { aktiv = false }
  }, [quelle, eintrag && eintrag.rel, seite])
  if (!eintrag) return jsx('div', { className: leise + ' text-xs p-2', children: 'Datei anklicken für Vorschau.' })
  if (eintrag.ordner) return jsx('div', { className: leise + ' text-xs p-2', children: 'Ordner: ' + eintrag.name })
  if (!v) return jsx('div', { className: leise + ' text-xs p-2', children: 'lade Vorschau …' })
  if (v.art === 'bild') return jsx('img', { src: v.data, alt: eintrag.name, className: 'max-h-full max-w-full object-contain' })
  if (v.art === 'pdf_seite') return jsxs('div', { className: 'flex h-full flex-col gap-1 min-h-0', children: [
    jsxs('div', { className: 'flex items-center gap-1 text-xs p-1', children: [
      jsx(Button, { size: 'sm', variant: 'ghost', disabled: v.seite <= 0, onClick: () => setSeite(v.seite - 1), children: '‹' }),
      jsx('span', { children: 'Seite ' + (v.seite + 1) + ' von ' + v.seiten }),
      jsx(Button, { size: 'sm', variant: 'ghost', disabled: v.seite + 1 >= v.seiten, onClick: () => setSeite(v.seite + 1), children: '›' })] }),
    jsx('div', { className: 'flex-1 min-h-0 overflow-auto', children: jsx('img', { src: v.data, alt: eintrag.name + ' Seite ' + (v.seite + 1), className: 'w-full' }) })] })
  if (v.art === 'karte') return jsxs('div', { className: 'p-2 overflow-auto h-full', style: { fontSize: 11 }, children: [
    jsx('div', { className: 'mb-2 rounded border border-(--ui-stroke-secondary) p-1', children: v.kopf.map((z) => jsxs('div', { children: [jsx('span', { className: leise, children: z[0] + ': ' }), z[1]] }, z[0])) }),
    jsx('pre', { className: 'whitespace-pre-wrap break-words font-sans', children: v.inhalt + (v.abgeschnitten ? '\n… (gekürzt)' : '') })] })
  if (v.art === 'pdf') return jsx(SandboxedFrame, { src: v.data, title: eintrag.name, className: 'w-full h-full' })
  if (v.art === 'text') return jsx('pre', { className: 'whitespace-pre-wrap break-all p-2 font-mono', style: { fontSize: 11 }, children: v.inhalt + (v.abgeschnitten ? '\n… (gekürzt)' : '') })
  return jsx('div', { className: leise + ' text-xs p-2', children: 'Keine Vorschau: ' + (v.hinweis || v.mime || '') + ' · ' + fmtB(v.groesse) })
}

function ReraiseErgebnis({ erg, laeuft, onJa, onNein }) {
  if (erg.laeuft) {
    return jsx('div', { children: erg.probe ? 'Reraise: prüfe Zwillinge, baue Karten (nichts wird gesendet) …' : 'Reraise: sende …' })
  }
  if (erg.ausfall) return jsx('div', { className: 'text-destructive', children: erg.ausfall })
  const kopf = (erg.probe ? 'Reraise-Probe (nichts gesendet): ' : 'Reraise: ') + erg.dateien + ' Dateien: ' + erg.gesendet + ' gesendet, '
    + erg.schon_da + ' schon da, ' + (erg.dubletten ? erg.dubletten + ' Dubletten, ' : '') + erg.verweigert_privat + ' privat verweigert, ' + erg.ohne_zwilling + ' ohne Zwilling, '
    + (erg.probe ? erg.gebaut + ' gebaut, ' : '') + (erg.unklar ? erg.unklar + ' unklar, ' : '')
    + (erg.nicht_bestaetigt ? erg.nicht_bestaetigt + ' nicht bestätigt, ' : '') + erg.fehler + ' Fehler'
    + (erg.protokoll_fehler ? ', ' + erg.protokoll_fehler + ' Protokollfehler' : '')
  const zeile = (e, i) => jsx('div', {
    className: 'truncate', title: (e.karte_probe && e.karte_probe.body) || e.url || e.grund || '',
    children: (e.datei || '').split(/[\\/]/).pop() + ' → ' + e.zustand + (e.karte ? ' · Karte ' + e.karte : '')
      + (e.titel ? ' · „' + e.titel + '“' : '') + (e.grund ? ' · ' + e.grund : '')
  }, i)
  const frage = 'Öffentliche Karten sind endgültig und für alle sichtbar – ' + (erg.bestaetigt || []).length + ' Karten wirklich senden? '
    + 'Reraise kennt kein Löschen.'
  return jsxs('div', { children: [
    jsx('div', { className: 'font-medium', children: kopf }),
    erg.abbruch ? jsx('div', { className: 'text-destructive', children: 'Abbruch: ' + erg.abbruch }) : null,
    erg.frage ? jsxs('div', { className: 'flex gap-2 items-center flex-wrap', children: [
      jsx('span', { className: 'font-medium', children: frage }),
      jsx(Button, { size: 'sm', disabled: laeuft, onClick: onJa, children: 'Ja, senden' }),
      jsx(Button, { size: 'sm', variant: 'ghost', onClick: onNein, children: 'Abbrechen' })
    ] }) : null,
    ...(erg.eintraege || []).slice(0, 40).map(zeile)
  ] })
}

// sperre: EIN useRef des Elternteils fuer alle Laeufe dort (Ins Wissen und Reraise), damit nie zwei gleichzeitig starten
function useReraise(quelle, pfad, sperre) {
  const [erg, setErg] = useState(null); const [probe, setProbe] = useState(true)
  const laeuft = !!(erg && erg.laeuft)
  const rufen = async (sammlung, nurProbe, bestaetigt) => {
    if (sperre.current) return
    sperre.current = true
    setErg({ laeuft: true, probe: nurProbe })
    try {
      const body = { mandant: MANDANT, quelle, pfad, sammlung, probe: nurProbe }
      if (!nurProbe) body.bestaetigt = bestaetigt
      const r = await rest('/reraise', { method: 'POST', body, timeoutMs: 3600000 })
      const gebaut = (r.eintraege || []).filter((e) => e.zustand === 'gebaut' && e.sha256 && e.karte_hash).map((e) => e.sha256 + ':' + e.karte_hash)
      setErg(nurProbe && !probe && gebaut.length ? { ...r, frage: true, sammlung, bestaetigt: gebaut } : r)
    } catch (e) { setErg({ ausfall: String(e && e.message || e) }) } finally { sperre.current = false }
  }
  const los = (sammlung) => {
    if (!sammlung) { host.notify({ kind: 'warning', message: 'Trove: Sammlung angeben (die des Markdown-Zwillings).' }); return }
    rufen(sammlung, true)
  }
  const knopf = (sammlung, gesperrt) => !ZEIGE.reraise ? [jsx('a', { href: 'https://reraise.ai', target: '_blank', rel: 'noopener', className: 'text-xs', style: { color: 'var(--ui-text-tertiary)', textDecoration: 'underline' }, title: 'Veroeffentlichen nur mit hinterlegtem Reraise-Schluessel — zum Anmelden: reraise.ai', children: 'zu Reraise – anmelden' }, 'reraise-anmelden')] : [
    jsxs('label', {
      className: 'text-xs flex items-center gap-1', title: 'Karten bauen und anzeigen (Maus über die Zeile), nichts senden',
      children: [jsx('input', {
        type: 'checkbox', checked: probe, disabled: laeuft, onChange: (e) => { setProbe(e.target.checked); setErg(null) }
      }), 'nur Probe']
    }),
    jsx(Button, {
      size: 'sm', variant: 'ghost', disabled: laeuft || !!gesperrt, onClick: () => los(sammlung),
      title: 'Öffentliche Markdown-Zwillinge als Beleg-Karten zu reraise.ai. Ohne „nur Probe“: erst Probe, dann Rückfrage.',
      children: laeuft ? 'läuft …' : 'zu Reraise hochladen'
    })
  ]
  const ergebnis = erg ? jsx(ReraiseErgebnis, {
    erg, laeuft, onJa: () => rufen(erg.sammlung, false, erg.bestaetigt), onNein: () => setErg({ ...erg, frage: false })
  }) : null
  return { knopf, laeuft, ergebnis }
}

function ZuReraise({ r }) {
  const sammlungen = r.sammlungen || []
  const [sammlung, setSammlung] = useState(sammlungen[0] ? sammlungen[0].sammlung : ''); const sperre = useRef(false)
  const rr = useReraise(r.quelle, r.pfad, sperre)
  return jsxs('div', { className: 'flex flex-col gap-1', children: [
    jsx('div', {
      className: leise,
      children: 'Datei „' + r.datei + '“ als öffentliche Beleg-Karte zu reraise.ai — nur wenn ihr Markdown-Zwilling '
        + 'in der Sammlung Sichtbarkeit öffentlich trägt.'
    }),
    sammlungen.length ? jsxs('div', { className: 'flex gap-2 items-center flex-wrap', children: [
      jsxs('select', {
        className: 'rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-xs', value: sammlung,
        onChange: (e) => setSammlung(e.target.value),
        children: sammlungen.map((s) => jsx('option', {
          value: s.sammlung, children: s.sammlung + ' (' + s.markdown + ' Markdown)'
        }, s.sammlung))
      }),
      ...rr.knopf(sammlung)
    ] }) : jsx('div', { children: 'Noch keine Sammlung — erst den Ordner „Ins Wissen“ geben (Sichtbarkeit öffentlich).' }),
    rr.ergebnis
  ] })
}

function InsWissen({ r }) {
  // Kundenmodus: keine Pflichtfelder. Sammlung leer = SAMMLUNG_STANDARD (wiki/quellen), Zugehoerigkeit leer = ZUG_OFFEN
  // (die Belegbuchung fragt spaeter gezielt nach). Sammlung, Sichtbarkeit, Traeger nur unter "Mehr".
  const [sammlung, setSammlung] = useState(''); const [zug, setZug] = useState('')
  const [sicht, setSichtRoh] = useState(() => storage.get('sichtbarkeit_' + MANDANT, 'privat'))
  const setSicht = (v) => { storage.set('sichtbarkeit_' + MANDANT, v); setSichtRoh(v) }  // je Mandant dauerhaft, Standard privat
  const [traeger, setTraeger] = useState(''); const [karten, setKarten] = useState(KARTENZIEL === 'wiki'); const [erg, setErg] = useState(null)
  const [mehr, setMehr] = useState(false)
  const sperre = useRef(false); const sammlungEff = sammlung.trim().toLowerCase() || SAMMLUNG_STANDARD
  const rr = useReraise(r.quelle, r.pfad, sperre)
  const laeuft = !!(erg && erg.laeuft)
  const auswahlD = r.dateien || []; const ganz = auswahlD.length === 0
  const [einzeln, setEinzeln] = useState(WISSEN_EINZELN); const [ganzOk, setGanzOk] = useState(false); const [recycle, setRecycle] = useState(null)
  const [ohnePflicht, setOhnePflicht] = useState(WISSEN_OHNE_PFLICHT)
  const laedt = useBackendLaedt()
  useEffect(() => { if (!laedt) { setEinzeln(WISSEN_EINZELN); setOhnePflicht(WISSEN_OHNE_PFLICHT) } }, [laedt])
  const backendNeu = async () => {
    setRecycle('Backend wird neu geladen …')
    const m = await backendNeuLaden()
    setEinzeln(WISSEN_EINZELN); setOhnePflicht(WISSEN_OHNE_PFLICHT); setRecycle(m)
  }
  const grund = laeuft ? null : laedt ? 'Backend lädt …' : rr.laeuft ? 'Reraise läuft …' : (!ganz && !einzeln) ? 'erst Backend neu laden' : null
  const los = async () => {
    if (sperre.current || grund) return
    if (ganz && !ganzOk) { setGanzOk(true); return }
    sperre.current = true
    setErg({ laeuft: true })
    const body = { mandant: MANDANT, quelle: r.quelle, pfad: r.pfad, sammlung: sammlungEff, sichtbarkeit: sicht, zugehoerigkeit: zug.trim() || ZUG_OFFEN, karten }
    if (!ganz) body.dateien = auswahlD
    if (traeger) body.traeger = traeger
    try {
      const z = await rest('/wissen', { method: 'POST', body, timeoutMs: 7200000 })
      setErg(z)
      if (z.wolke_hinweis) host.notify({ kind: 'warning', message: 'Trove: ' + z.wolke_hinweis })
    }
    catch (e) { setErg({ ausfall: String(e && e.message || e) }) } finally { sperre.current = false; setGanzOk(false) }
  }
  const auswahl = 'rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-xs'
  const zeile = (e, i) => jsx('div', {
    className: 'truncate', title: e.md || e.fehler,
    children: (e.fehler ? 'FEHLER ' : '') + (e.datei || '').split(/[\\/]/).pop() + ' → ' + (e.zustand || '')
      + (e.karte ? ' · Karte ' + e.karte : e.grund ? ' · keine Karte: ' + e.grund : '')
      + (e.fehler ? ' ' + e.fehler : '')
  }, i)
  const erklaerung = (ganz ? 'GANZER Ordner „' + (r.datei || '/') + '“ (' + (r.anzahl || 0) + ' Dateien, auch Unterordner)' : auswahlD.length + ' ausgewählte Datei(en): ' + auswahlD.map((x) => x.split('/').pop()).join(', ')) + ' ins Wissen: je Datei ein Markdown-Zwilling (mit Prüfsumme ans Original '
    + 'gebunden), ' + (KARTENZIEL === 'wiki' ? 'dazu je Datei eine Wissenskarte im Wiki (status: zu-prüfen) mit Herkunft: Originalpfad und Ordner in deiner Ablage. Das Original bleibt unverändert.' : 'QMD-Sammlung trove-' + MANDANT + '-' + sammlungEff + ', optional Beleg-Karten auf dem Brett.')
  const zusammen = (z) => z.dateien + ' Dateien: ' + z.neu + ' neu, ' + z.schon_da + ' schon da, ' + (z.dubletten ? z.dubletten + ' Dubletten, ' : '') + z.karten + ' Karten, '
    + z.fehler + ' Fehler' + (z.nachgefuehrt ? ' · Ordner wird automatisch nachgeführt' : '') + (z.merker_offen ? ' · ' + z.merker_offen + ' Karte(n) unklar (Brett prüfen)' : '') + ' · QMD ' + (z.qmd && z.qmd.sammlung) + ' (' + (z.qmd && z.qmd.dateien_im_index) + ' im Index)'
  let ausgabe = null
  if (laeuft) ausgabe = jsx('div', { children: 'liest, schreibt Markdown, baut Sammlung … (OCR je Seite einige Sekunden)' })
  else if (erg && erg.ausfall) ausgabe = jsx('div', { className: 'text-destructive', children: erg.ausfall })
  else if (erg) {
    ausgabe = jsxs('div', { children: [
      jsx('div', { className: 'font-medium', children: zusammen(erg) }),
      erg.wolke_hinweis ? jsx('div', { className: 'font-medium text-destructive', children: '☁ ' + erg.wolke_hinweis }) : null, ...(erg.hinweise || []).map((h, i) => jsx('div', { className: 'text-destructive', children: h }, 'h' + i)), ...(erg.eintraege || []).slice(0, 40).map(zeile)] })
  }
  const knopfText = laeuft ? 'läuft …' : grund ? 'Ins Wissen – ' + grund : ganz ? (ganzOk ? 'Ja, ganzen Ordner (' + (r.anzahl || 0) + ' Dateien)' : 'Ins Wissen (ganzer Ordner)') : 'Ins Wissen (' + auswahlD.length + ' ausgewählt)'
  return jsxs('div', { className: 'flex flex-col gap-1', children: [
    jsx('div', { className: leise, children: erklaerung }),
    jsxs('div', { className: 'flex gap-2 items-center flex-wrap', children: [
      jsx(Input, {
        placeholder: 'Zugehörigkeit (optional, z. B. Meine Firma)', value: zug, onChange: (e) => setZug(e.target.value), className: 'text-xs w-56',
        title: 'Optional. Leer = „offen“; die Belegbuchung fragt später gezielt nach.'
      }),
      jsxs('label', { className: 'text-xs flex items-center gap-1', children: [
        jsx('input', { type: 'checkbox', checked: karten, onChange: (e) => setKarten(e.target.checked) }), KARTENZIEL === 'wiki' ? 'Wissenskarten erzeugen' : 'Beleg-Karten erzeugen'] }),
      jsx(Button, { size: 'sm', disabled: laeuft || !!grund, title: grund ? 'Gerade nicht möglich: ' + grund : 'Keine Pflichtfelder: ohne Angaben Sammlung „' + SAMMLUNG_STANDARD + '“, Zugehörigkeit „' + ZUG_OFFEN + '“.', onClick: los, children: knopfText }),
      ganzOk && !laeuft ? jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setGanzOk(false), children: 'Abbrechen' }) : null,
      jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setMehr(!mehr), children: mehr ? 'Weniger' : 'Mehr …' })
    ] }),
    mehr ? jsxs('div', { className: 'flex gap-2 items-center flex-wrap', children: [
      jsx(Input, {
        placeholder: 'Sammlung (leer = ' + SAMMLUNG_STANDARD + ')', value: sammlung, onChange: (e) => setSammlung(e.target.value), className: 'text-xs w-44'
      }),
      jsxs('select', {
        className: auswahl, value: sicht, onChange: (e) => setSicht(e.target.value),
        title: 'privat = eigene Nutzung · öffentlich = Reraise-Tausch',
        children: [jsx('option', { value: 'privat', children: 'privat (eigene Nutzung)' }),
          jsx('option', { value: 'oeffentlich', children: 'öffentlich (Reraise)' })]
      }),
      jsxs('select', {
        className: auswahl, value: traeger, onChange: (e) => setTraeger(e.target.value),
        title: 'automatisch: „offen“ ohne Zugehörigkeit, Person bei „privat“ in der Zugehörigkeit, sonst Firma',
        children: [jsx('option', { value: '', children: 'Träger automatisch' }, ''),
          ...['Firma', 'Person', 'Projekt', 'Kunde', 'Behoerde'].map((t) => jsx('option', { value: t, children: t }, t))]
      }),
      ...rr.knopf(sammlungEff, laeuft)
    ] }) : null,
    ganzOk && !laeuft ? jsx('div', { className: 'font-medium text-destructive', children: 'Nichts angehakt: es wird der GANZE Ordner verarbeitet (' + (r.anzahl || 0) + ' Dateien, mit Unterordnern, danach automatisch nachgeführt). Einzelne Belege: Häkchen setzen.' }) : null,
    (!ganz && !einzeln) || !ohnePflicht || recycle ? jsxs('div', { className: 'flex gap-2 items-center flex-wrap', children: [
      (!ganz && !einzeln) || !ohnePflicht ? jsx('span', { className: 'font-medium' + (!ganz && !einzeln ? ' text-destructive' : ''), children: !ganz && !einzeln ? 'Einzelauswahl: erst das Backend neu laden (10–30 s, laufende KI-Antworten abwarten).' : 'Neue Backend-Fassung bereit – bei Gelegenheit neu laden (10–30 s). „Ins Wissen“ geht auch ohne.' }) : null,
      (!ganz && !einzeln) || !ohnePflicht ? jsx(Button, { size: 'sm', disabled: laedt, onClick: backendNeu, children: laedt ? 'Backend lädt …' : 'Trove-Backend neu laden' }) : null,
      recycle ? jsx('span', { children: recycle }) : null
    ] }) : null,
    laeuft ? null : rr.ergebnis, // laufende Anzeige hat Vorrang vor dem alten Ergebnis der anderen Aktion
    rr.laeuft ? null : ausgabe
  ] })
}

function Unterschrift({ r }) {
  const [vorlage, setVorlage] = useState(r.vorlagen && r.vorlagen[0] ? String(r.vorlagen[0].id) : ''); const [an, setAn] = useState(''); const [name, setName] = useState(''); const [mail, setMail] = useState(false); const [erg, setErg] = useState(null)
  const senden = async () => {
    if (!an || !vorlage) { host.notify({ kind: 'warning', message: 'Trove: Vorlage und E-Mail angeben.' }); return }
    setErg({ laeuft: true })
    try { setErg(await rest('/docuseal/senden', { method: 'POST', body: { mandant: MANDANT, quelle: r.quelle, pfad: r.pfad, vorlage, an, name, mail }, timeoutMs: 120000 })) }
    catch (e) { setErg({ fehler: String(e && e.message || e) }) }
  }
  return jsxs('div', { className: 'flex flex-col gap-1', children: [
    jsx('div', { className: leise, children: 'Original: ' + r.datei + ' — Vorlage aus DocuSeal (Vorlagen entstehen in der DocuSeal-Oberfläche; PDF-Upload per API ist Pro-Edition).' }),
    jsxs('div', { className: 'flex gap-2 items-center flex-wrap', children: [
      jsxs('select', { className: 'rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-xs', value: vorlage, onChange: (e) => setVorlage(e.target.value), children: (r.vorlagen || []).map((v) => jsx('option', { value: String(v.id), children: v.id + ' · ' + v.name + ' (' + (v.rollen || []).join(', ') + ')' }, v.id)) }),
      jsx(Input, { placeholder: 'E-Mail des Unterzeichners', value: an, onChange: (e) => setAn(e.target.value), className: 'text-xs w-56' }),
      jsx(Input, { placeholder: 'Name (optional)', value: name, onChange: (e) => setName(e.target.value), className: 'text-xs w-40' }),
      jsxs('label', { className: 'text-xs flex items-center gap-1', children: [jsx('input', { type: 'checkbox', checked: mail, onChange: (e) => setMail(e.target.checked) }), 'DocuSeal mailt die Einladung'] }),
      jsx(Button, { size: 'sm', onClick: senden, children: 'Vorgang anlegen' })
    ] }),
    erg ? (erg.laeuft ? jsx('div', { children: 'lege Vorgang an …' }) : erg.fehler ? jsx('div', { className: 'text-destructive', children: erg.fehler }) : jsxs('div', { children: ['Vorgang ' + erg.vorgang + ' angelegt (' + erg.vorlage + ' an ' + erg.an + '). Link: ', jsx('span', { className: 'font-mono select-all', children: erg.link }), mail ? '' : ' — Link selbst übergeben (keine Mail).'] })) : null
  ] })
}

function BelegErgebnis({ r, zurueck }) {
  const z = (k, v) => jsxs('div', { children: [jsx('span', { className: leise, children: k + ': ' }), jsx('span', { children: String(v ?? '–') })] }, k)
  let inhalt
  if (r.laeuft) inhalt = jsx('div', { children: r.art + ' läuft … (OCR kann je Seite einige Sekunden brauchen)' })
  else if (r.fehler) inhalt = jsx('div', { className: 'text-destructive', children: r.fehler })
  else if (r.art === 'lesen') inhalt = jsxs('div', { children: [
    z('Methode', r.methode + ', ' + r.seiten + ' Seiten, ' + r.dauer_s + ' s'), z('sha256', r.sha256),
    ...Object.entries(r.felder || {}).map(([k, v]) => z(k, v)),
    jsx('div', { className: 'mt-1 font-medium', children: 'Verprobung: ' + (r.verprobung.sicher ? 'bestanden (sicher)' : r.verprobung.bestanden ? 'bestanden' : 'Prüfliste') }),
    ...(r.verprobung.gruende || []).map((g, i) => jsx('div', { children: '· ' + g }, i)),
    jsx('div', { className: leise + ' mt-1', children: 'Zwilling: ' + r.zwilling }),
    jsx('pre', { className: 'mt-1 whitespace-pre-wrap ' + leise, style: { fontSize: 10 }, children: r.text_anfang })
  ] })
  else if (r.art === 'stapel') inhalt = jsxs('div', { children: [
    z('Stapel', r.datei + ' → ' + (r.ziel || '') + ' · ' + (r.belege || []).length + ' Belege aus ' + r.seiten + ' Seiten, ' + r.dauer_s + ' s'),
    ...(r.belege || []).map((b) => jsx('div', { children: '#' + b.nr + ' Seiten ' + b.seiten.join('–') + ' ' + (b.nummer ? 'Nr ' + b.nummer + ' ' : '') + b.arten.join('/') + (b.buchungsfassung ? ' (Buchungsfassung ohne Anhang)' : '') + ' → ' + b.datei }, b.nr))
  ] })
  else if (r.art === 'unterschrift') inhalt = jsx(Unterschrift, { r })
  else if (r.art === 'wissen') inhalt = jsx(InsWissen, { r })
  else if (r.art === 'reraise') inhalt = jsx(ZuReraise, { r })
  else inhalt = jsxs('div', { children: [
    z('sha256', r.sha256),
    r.fehler ? z('ERPNext', r.fehler) : null,
    r.beleg ? jsxs('div', { className: 'mt-1', children: [jsx('div', { className: 'font-medium', children: 'Beleg (ERPNext-Spiegel)' }),
      ...['dateiname', 'letzter_ort', 'erste_sicht', 'letzte_bewegung', 'ereignisse', 'belegdatum', 'brutto', 'netto', 'steuer', 'waehrung', 'rechnungsnummer', 'aussteller_kennung', 'lesemethode', 'verprobung'].filter((k) => r.beleg[k] != null && r.beleg[k] !== '').map((k) => z(k, r.beleg[k]))] }) : jsx('div', { className: leise, children: 'kein Beleg-Satz im Spiegel (noch nicht gespiegelt oder nie gelesen)' }),
    r.zwilling ? jsxs('div', { className: 'mt-1', children: [jsx('div', { className: 'font-medium', children: 'Zwilling (lokal): ' + r.zwilling.methode }), ...Object.entries(r.zwilling.felder || {}).map(([k, v]) => z(k, v))] }) : null,
    jsx('div', { className: 'font-medium mt-1', children: 'Ereignisse: ' + (Array.isArray(r.ereignisse) ? r.ereignisse.length : '–') }),
    ...(Array.isArray(r.ereignisse) ? r.ereignisse.slice(0, 40).map((e) => jsx('div', { children: '#' + e.nr + ' ' + e.zeit + ' ' + e.ereignis + ' ' + (e.quelle || '') + (e.ziel ? ' → ' + e.ziel : '') + (e.meldung ? ' [' + e.meldung + ']' : '') }, e.nr)) : [])
  ] })
  return jsxs('div', { className: 'p-2 font-mono', style: { fontSize: 11 }, children: [jsx('div', { className: 'font-medium mb-1', children: r.art + ': ' + (r.datei || '') }), inhalt, jsx(Button, { size: 'sm', variant: 'ghost', onClick: zurueck, children: 'zurück zur Vorschau' })] })
}

function Seite({ seite, quellen, remotes, zustand, setZustand, auswahl, setAuswahl, neuLaden, onQuellenGeaendert }) {
  const [liste, setListe] = useState(null); const [fehler, setFehler] = useState(null); const [neu, setNeu] = useState(false); const [aktiv, setAktiv] = useState(null)
  const [filter, setFilter] = useState('alle'); const [spur, setSpur] = useState(null)
  const [tippen, setTippen] = useState(false); const [eingabe, setEingabe] = useState('')
  const pfadOeffnen = async (pfad) => {
    setFehler(null)
    try { const n = await rest('/quellen', { method: 'POST', body: { mandant: MANDANT, art: 'auto', pfad: (typeof pfad === 'string' ? pfad : eingabe).trim() } }); setZustand({ quelle: n.id, pfad: '' }); setTippen(false); setEingabe(''); onQuellenGeaendert() } catch (e) { setFehler(String(e && e.message || e)) }
  }
  // "…": nativer Finder-Ordnerdialog der Hermes-App (window.hermesDesktop.selectPaths); ohne Bruecke oder bei Fehler das Pfad-Textfeld
  const durchsuchen = async () => {
    const d = typeof window !== 'undefined' ? window.hermesDesktop : null
    if (d && typeof d.selectPaths === 'function') {
      try {
        const p = await d.selectPaths({ directories: true, multiple: false, title: 'Ordner für Trove wählen' })
        if (Array.isArray(p) && p[0]) await pfadOeffnen(p[0])
        return
      } catch (e) { setFehler('Ordnerdialog: ' + String(e && e.message || e) + ' — Pfad bitte eintippen.') }
    }
    setTippen(true)
  }
  const q = quellen.find((x) => x.id === zustand.quelle)
  const laedt = useBackendLaedt()
  const wissenGrund = laedt ? 'Backend lädt …' : !zustand.quelle ? 'erst Quelle wählen' : !q ? 'Quelle lädt …' : q.art !== 'ordner' ? 'nur lokale Ordner' : null
  const laden = useCallback(() => {
    if (!q) { setListe(null); return }
    setFehler(null)
    rest('/liste?quelle=' + encodeURIComponent(q.id) + '&pfad=' + encodeURIComponent(zustand.pfad || '')).then((r) => {
      r.eintraege.forEach((e) => { e.rel = (zustand.pfad ? zustand.pfad + '/' : '') + e.name }); setListe(r)
    }).catch((e) => setFehler(String(e && e.message || e)))
  }, [zustand.quelle, zustand.pfad, q && q.id])
  useEffect(() => { laden() }, [laden, neuLaden])
  const geh = (pfad) => { setZustand({ ...zustand, pfad }); setAuswahl([]); setAktiv(null); setSpur(null) }
  const teile = (zustand.pfad || '').split('/').filter(Boolean)
  const sichtbar = (liste ? liste.eintraege : []).filter((e) => filter === 'alle' || e.ordner || (filter === 'nur' ? e.dublette : !e.dublette))
  const gewaehlt = (liste ? liste.eintraege : []).filter((e) => !e.ordner && auswahl.includes(e.name)).map((e) => e.rel)
  const insWissen = () => wissenGrund ? null : setSpur({ art: 'wissen', datei: zustand.pfad || q.name, quelle: q.id, pfad: zustand.pfad || '', dateien: gewaehlt, anzahl: liste ? liste.eintraege.filter((e) => !e.ordner).length : 0 })
  const toggle = (name) => setAuswahl(auswahl.includes(name) ? auswahl.filter((n) => n !== name) : [...auswahl, name])
  const registrieren = async () => { setFehler(null); try { const r = await rest('/register', { method: 'POST', body: { mandant: MANDANT, quelle: q.id, pfad: zustand.pfad || '' } }); host.notify({ kind: 'info', message: 'Trove: Register – ' + r.neu + ' neue, ' + r.gesamt + ' gesamt' + (r.nur_in_wolke ? ', ' + r.nur_in_wolke + ' nur in der Wolke (iCloud-Platzhalter)' : '') }); laden() } catch (e) { setFehler(String(e && e.message || e)) } }
  const [f2, setF2] = useState(null)
  const aenderungen = async () => { setFehler(null); setF2({ laeuft: true }); try { const r = await rest('/aenderungen', { method: 'POST', body: { mandant: MANDANT, quelle: q.id, pfad: zustand.pfad || '' }, timeoutMs: 3600000 }); setF2(r); laden() } catch (e) { setF2(null); setFehler(String(e && e.message || e)) } }
  const spurZeigen = async (e) => { try { setSpur(await rest('/spur?datei=' + encodeURIComponent(e.pfad))) } catch (x) { setSpur({ fehler: String(x && x.message || x) }) } }
  const belegAktion = async (art) => {
    const e = liste && liste.eintraege.find((x) => x.name === aktiv)
    if (!e || e.ordner) { host.notify({ kind: 'warning', message: 'Trove: erst eine Datei anklicken.' }); return }
    setSpur({ art, laeuft: true, datei: e.name })
    try {
      if (art === 'lesen') setSpur({ art, datei: e.name, ...(await rest('/lesen', { method: 'POST', body: { mandant: MANDANT, quelle: q.id, pfad: e.rel }, timeoutMs: 1800000 })) })
      else if (art === 'stapel') setSpur({ art, datei: e.name, ...(await rest('/stapel', { method: 'POST', body: { mandant: MANDANT, quelle: q.id, pfad: e.rel }, timeoutMs: 1800000 })) })
      else if (art === 'unterschrift') setSpur({ art, datei: e.name, quelle: q.id, pfad: e.rel, ...(await rest('/docuseal/vorlagen')) })
      else if (art === 'reraise') setSpur({ art, datei: e.name, quelle: q.id, pfad: e.rel, ...(await rest('/wissen/sammlungen')) })
      else setSpur({ art, datei: e.name, ...(await rest('/historie?datei=' + encodeURIComponent(e.pfad))) })
      if (art !== 'historie') laden()
    } catch (x) { setSpur({ art, datei: e.name, fehler: String(x && x.message || x) }) }
  }
  // Datei-Knoepfe stehen immer da (nichts springt beim Anklicken); ohne angeklickte Datei gesperrt
  const dateiKnopf = (text, art, title) => knopf(text, () => belegAktion(art), aktiv ? { title } : { title: 'erst Datei anklicken', disabled: true })
  return jsxs('div', { className: 'flex flex-col gap-2 min-w-0 h-full', children: [
    jsxs('div', { 'data-zeile': 'quelle', style: LEISTE, children: [
      jsx('span', { className: 'text-xs font-medium', children: seite === 'links' ? 'Quelle (links)' : 'Ziel (rechts)' }),
      jsxs('select', { className: 'rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-xs', style: { flex: '1 1 120px', minWidth: 0, maxWidth: 240 }, value: zustand.quelle || '', onChange: (e) => { setZustand({ quelle: e.target.value, pfad: '' }); setAuswahl([]); setAktiv(null) }, children: [
        jsx('option', { value: '', children: '– Quelle wählen –' }),
        ...quellen.map((x) => jsx('option', { value: x.id, children: (x.art === 'rclone' ? '☁ ' : '📁 ') + x.name + (x.nur_lesen ? ' (nur lesen)' : '') }, x.id))
      ] }),
      q ? knopf('×', async () => { await rest('/quellen/' + q.id, { method: 'DELETE' }); setZustand({ quelle: '', pfad: '' }); onQuellenGeaendert() }, { title: 'Quelle entfernen' }) : null,
      jsxs('span', { 'data-paar': true, style: PAAR, children: [
        knopf('…', durchsuchen, { title: 'Ordner im Finder wählen' }),
        knopf('Pfad', () => setTippen(!tippen), { title: 'Pfad direkt eintippen (Ordner oder Online-Speicher als name:pfad)' }),
        knopf('+ Quelle', () => setNeu(!neu))] })
    ] }),
    tippen ? jsxs('div', { className: 'flex gap-2 items-center', children: [
      jsx(Input, { placeholder: 'Pfad, z. B. ~/Dokumente/Belege oder meinecloud:Belege', value: eingabe, onChange: (e) => setEingabe(e.target.value), onKeyDown: (e) => { if (e.key === 'Enter' && eingabe.trim()) pfadOeffnen() }, className: 'text-xs flex-1' }),
      jsx(Button, { size: 'sm', disabled: !eingabe.trim(), onClick: () => pfadOeffnen(), children: 'Prüfen und öffnen' })
    ] }) : null,
    neu ? jsx(QuelleAnlegen, { remotes, onFertig: (ok) => { setNeu(false); if (ok) onQuellenGeaendert() } }) : null,
    jsxs('div', { 'data-zeile': 'pfad', className: 'text-xs', style: LEISTE, children: [
      // Pfad kuerzt sich bis 140 px (feste Basis), damit beide Seiten gleich umbrechen und die Listen auf gleicher Hoehe beginnen
      q ? jsxs('span', { title: q.pfad, style: { flex: '1 1 140px', minWidth: 140, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: [
        jsx('button', { className: 'underline', onClick: () => geh(''), children: q.art === 'rclone' ? q.remote + ':' + (q.pfad || '') : q.pfad }),
        ...teile.map((t, i) => jsxs('span', { children: [' / ', jsx('button', { className: 'underline', onClick: () => geh(teile.slice(0, i + 1).join('/')), children: t })] }, i))] }) : jsx('span', { style: { flex: '1 1 140px' } }),
      q && q.art === 'ordner' ? knopf(liste && liste.indexiert ? 'Register ✓' : 'Indexieren', registrieren, { title: 'Prüfsummen rechnen (Dubletten erkennen)' }) : null,
      q && q.art === 'ordner' ? knopf('Änderungen prüfen', aenderungen, { title: 'F2: Stand aufnehmen und mit dem letzten Lauf vergleichen (neu / weg / verschoben / außen geändert)' }) : null,
      q ? jsxs('select', { className: 'rounded border border-(--ui-stroke-secondary) bg-transparent px-1 text-xs', value: filter, onChange: (e) => setFilter(e.target.value), children: [jsx('option', { value: 'alle', children: 'alle' }), jsx('option', { value: 'nur', children: 'nur Dubletten' }), jsx('option', { value: 'ohne', children: 'ohne Dubletten' })] }) : null,
      jsx(Button, { size: 'sm', disabled: !!wissenGrund, title: wissenGrund ? 'Gerade nicht möglich: ' + wissenGrund : 'F16: angehakte Dateien (sonst den ganzen Ordner) ins Wissen — Markdown-Zwilling je Datei, QMD-Sammlung, Wissenskarte. Das Original bleibt unverändert.', onClick: insWissen, children: wissenGrund ? 'Ins Wissen – ' + wissenGrund : gewaehlt.length ? 'Ins Wissen (' + gewaehlt.length + ' ausgewählt)' : 'Ins Wissen (ganzer Ordner)' })
    ] }),
    fehler ? jsx('div', { className: 'text-xs text-destructive', children: fehler }) : null,
    f2 ? jsxs('div', { className: rahmen + ' p-2 text-xs', children: [
      jsx('span', { className: 'font-medium', children: f2.laeuft ? 'Änderungen werden geprüft …' : 'F2 Lauf ' + f2.lauf + (f2.erster_lauf ? ' (Grundstand aufgenommen)' : '') + ': ' + Object.entries(f2.zusammen || {}).map(([k, v]) => k + ' ' + v).join(' · ') }),
      !f2.laeuft ? jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setF2(null), children: '×' }) : null,
      ...(!f2.laeuft ? ['neu', 'weg', 'verschoben', 'aussen_geaendert', 'durch_trove'].flatMap((k) => (f2.befunde[k] || []).slice(0, 15).map((b, i) => jsx('div', { className: 'font-mono truncate', children: k + ': ' + (b.pfad || (b.von + ' → ' + b.nach)) }, k + i))) : [])
    ] }) : null,
    jsxs('div', { 'data-liste': seite, className: rahmen + ' flex-1 min-h-0 overflow-auto text-xs', style: { minHeight: 160 }, children: [
      !q ? jsx('div', { className: leise + ' p-2', children: 'Keine Quelle gewählt.' }) : !liste ? jsx('div', { className: leise + ' p-2', children: 'lade …' }) : null,
      zustand.pfad ? jsx('div', { className: 'px-2 py-1 cursor-pointer hover:bg-(--ui-row-hover-background)', onDoubleClick: () => geh(teile.slice(0, -1).join('/')), children: '⬆ ..' }) : null,
      ...sichtbar.map((e) => jsxs('div', { className: 'flex items-center gap-2 px-2 py-0.5 cursor-pointer hover:bg-(--ui-row-hover-background) ' + (aktiv === e.name ? 'bg-(--ui-bg-secondary)' : ''),
        onClick: () => { setAktiv(e.name); setSpur(null) }, onDoubleClick: () => e.ordner ? geh(e.rel) : spurZeigen(e), children: [
        jsx('input', { type: 'checkbox', checked: auswahl.includes(e.name), onChange: () => toggle(e.name), onClick: (ev) => ev.stopPropagation() }),
        jsx('span', { className: 'flex-1 truncate', title: e.pfad, children: (e.ordner ? '📁 ' : '') + e.name }),
        e.dublette ? jsx(Badge, { children: e.fundorte + '× vorhanden' }) : null,
        jsx('span', { className: leise + ' w-16 text-right', children: e.ordner ? '' : fmtB(e.groesse) }),
        jsx('span', { className: leise + ' w-32 text-right', children: fmtT(e.mtime) })
      ] }, e.name))
    ] }),
    jsxs('div', { 'data-zeile': 'fuss', className: 'text-xs', style: LEISTE, children: [
      jsx('span', { className: leise, style: { whiteSpace: 'nowrap' }, children: liste ? liste.eintraege.length + ' Einträge, ' + auswahl.length + ' markiert' : '' }),
      dateiKnopf('Lesen', 'lesen', 'F3/F4: Lese-Treppe (E-Rechnung, Textlayer, sonst OCR lokal) und feste Verprobung; Zwilling + Belegprotokoll'),
      dateiKnopf('Zerlegen', 'stapel', 'F6: mehrseitiges PDF in einzelne Belege zerlegen (Stirling-PDF)'),
      dateiKnopf('Historie', 'historie', 'F11: Geschäftshistorie dieses Inhalts aus dem ERPNext-Spiegel'),
      ZEIGE.docuseal ? dateiKnopf('Unterschrift', 'unterschrift', 'F7: diese Datei als Original zu einem Unterschriften-Vorgang (DocuSeal-Vorlage) geben') : null,
      ZEIGE.reraise ? dateiKnopf('zu Reraise', 'reraise', 'F16: diese Datei als öffentliche Beleg-Karte zu reraise.ai (nur mit öffentlichem Markdown-Zwilling)') : jsx('a', { href: 'https://reraise.ai', target: '_blank', rel: 'noopener', className: 'text-xs', style: { color: 'var(--ui-text-tertiary)', textDecoration: 'underline' }, title: 'Veroeffentlichen nur mit hinterlegtem Reraise-Schluessel — zum Anmelden: reraise.ai', children: 'zu Reraise – anmelden' }, 'reraise-anmelden'),
      jsxs('span', { 'data-paar': true, style: { ...PAAR, marginLeft: 'auto' }, children: [
        knopf('alle', () => setAuswahl(liste.eintraege.map((e) => e.name)), { disabled: !liste }),
        knopf('Auswahl leeren', () => setAuswahl([]), { disabled: !auswahl.length })] })
    ] }),
    jsx('div', { className: rahmen + ' overflow-auto shrink-0', style: { height: 220 }, children: spur && spur.art
      ? jsx(BelegErgebnis, { r: spur, zurueck: () => setSpur(null) })
      : spur
      ? jsxs('div', { className: 'p-2 font-mono', style: { fontSize: 11 }, children: [
          jsx('div', { className: 'font-medium mb-1', children: 'Spur (Belegprotokoll): ' + (spur.datei || '') }),
          spur.fehler ? jsx('div', { children: spur.fehler }) : null,
          ...['direkt', 'rueckwaerts', 'vorwaerts', 'gleicher_inhalt'].map((k) => jsxs('div', { children: [jsx('div', { className: leise + ' mt-1', children: k + ': ' + ((spur[k] || []).length) }), ...(spur[k] || []).slice(0, 30).map((e) => jsx('div', { children: '#' + e.nr + ' ' + e.zeit + ' ' + e.ereignis + ' ' + (e.quelle || '') + (e.ziel ? ' → ' + e.ziel : '') + (e.meldung ? ' [' + e.meldung + ']' : '') }, e.nr))] }, k)),
          jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setSpur(null), children: 'zurück zur Vorschau' })
        ] })
      : jsx(Vorschau, { quelle: q && q.id, eintrag: liste && liste.eintraege.find((e) => e.name === aktiv) }) })
  ] })
}

function Suche() {
  const [q, setQ] = useState(''); const [r, setR] = useState(null); const [offen, setOffen] = useState(false)
  const suchen = async () => { if (q.trim().length < 2) return; setOffen(true); setR({ laeuft: true }); try { setR(await rest('/suche?q=' + encodeURIComponent(q.trim()))) } catch (e) { setR({ fehler: String(e && e.message || e) }) } }
  const zeile = (t, k) => jsx('div', { className: 'truncate', title: t, children: t }, k)
  return jsxs('div', { style: { position: 'relative' }, children: [
    jsxs('div', { className: 'flex gap-2 items-center', children: [
      jsx(Input, { placeholder: 'Suche über beide Welten: Dateiname, Rechnungsnummer, Absender, sha256 …', value: q, onChange: (e) => setQ(e.target.value), onKeyDown: (e) => { if (e.key === 'Enter') suchen() }, className: 'text-xs flex-1' }),
      jsx(Button, { size: 'sm', onClick: suchen, children: 'Suchen' }),
      r ? jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setOffen(!offen), children: offen ? 'zu' : 'auf' }) : null
    ] }),
    // ueberlagert die Leisten darunter, statt sie nach unten zu schieben
    offen && r ? jsx('div', { 'data-suchergebnis': true, className: rahmen + ' overflow-auto p-2 font-mono', style: { fontSize: 11, maxHeight: 260, position: 'absolute', top: '100%', left: 0, right: 0, marginTop: 4, zIndex: 20, background: 'var(--ui-bg-chrome)', boxShadow: '0 6px 16px rgba(0,0,0,.18)' }, children:
      r.laeuft ? 'suche …' : r.fehler ? jsx('span', { className: 'text-destructive', children: r.fehler }) : r.hinweis ? r.hinweis : jsxs('div', { children: [
        ...Object.entries(r.liegt || {}).map(([wo, pfade]) => jsxs('div', { className: 'mb-1', children: [jsx('div', { className: 'font-medium', children: 'liegt ' + wo + ' unter (' + pfade.length + '):' }), ...pfade.map((p, i) => zeile(p, i))] }, wo)),
        (r.erpnext && r.erpnext.belege && r.erpnext.belege.length) ? jsxs('div', { className: 'mb-1', children: [jsx('div', { className: 'font-medium', children: 'ERPNext Belege (' + r.erpnext.belege.length + '):' }), ...r.erpnext.belege.map((b) => zeile((b.belegdatum || '') + ' ' + (b.brutto != null ? b.brutto + ' ' + (b.waehrung || '') : '') + ' ' + (b.rechnungsnummer || '') + ' ' + (b.dateiname || '') + ' [' + (b.verprobung || '–') + ']', b.name))] }) : null,
        (r.erpnext && r.erpnext.kunden && r.erpnext.kunden.length) ? jsxs('div', { className: 'mb-1', children: [jsx('div', { className: 'font-medium', children: 'ERPNext Kundenakten (' + r.erpnext.kunden.length + '):' }), ...r.erpnext.kunden.map((k) => zeile(k.doctype + ' ' + k.name + ' ' + (k.customer_name || k.supplier_name || '') + ' ' + (k.grand_total != null ? k.grand_total + ' ' + (k.currency || '') : ''), k.doctype + k.name))] }) : null,
        (r.paperless && r.paperless.dokumente && r.paperless.dokumente.length) ? jsxs('div', { className: 'mb-1', children: [jsx('div', { className: 'font-medium', children: 'Paperless (Welt A, ' + r.paperless.dokumente.length + '):' }), ...r.paperless.dokumente.map((d) => zeile('#' + d.id + ' ' + (d.erstellt || '').slice(0, 10) + ' ' + (d.titel || '') + ' (' + (d.datei || '') + ')', d.id))] }) : null,
        (r.protokoll && r.protokoll.length) ? jsxs('div', { children: [jsx('div', { className: 'font-medium', children: 'Belegprotokoll (' + r.protokoll.length + '):' }), ...r.protokoll.slice(0, 20).map((e) => zeile('#' + e.nr + ' ' + e.zeit + ' ' + e.ereignis + ' ' + (e.quelle || '') + (e.ziel ? ' → ' + e.ziel : '') + (e.meldung ? ' [' + e.meldung + ']' : ''), e.nr))] }) : null,
        ...['erpnext', 'paperless'].filter((k) => r[k] && r[k].fehler).map((k) => jsx('div', { className: leise, children: k + ': ' + r[k].fehler }, k))
      ] }) }) : null
  ] })
}

function Auftraege() {
  const [liste, setListe] = useState([]); const [titel, setTitel] = useState(''); const [text, setText] = useState(''); const [assignee, setAssignee] = useState('schnapper')
  const [offen, setOffen] = useState(false); const [fehler, setFehler] = useState(null); const [laeuft, setLaeuft] = useState(false)
  const laden = useCallback(() => rest('/auftraege').then((r) => { setListe(r.auftraege || []); if (r.fehler) setFehler(r.fehler) }).catch((e) => setFehler(String(e && e.message || e))), [])
  useEffect(() => { laden(); const t = setInterval(laden, 20000); return () => clearInterval(t) }, [laden])
  const titelEff = titel.trim() || text.trim().split('\n')[0].slice(0, 80)
  const anlegen = async () => { if (!titelEff || laeuft) return; setLaeuft(true); setFehler(null); try { await rest('/auftrag', { method: 'POST', body: { mandant: MANDANT, titel: titelEff, text, assignee: (PROFILE_GELADEN && !PROFILE.includes(assignee)) ? PROFILE[0] : assignee, kontext: { links: storage.get('links', {}), rechts: storage.get('rechts', {}) } } }); setTitel(''); setText(''); setOffen(false); laden() } catch (e) { setFehler(String(e && e.message || e)) } finally { setLaeuft(false) } }
  const tu = async (id, was) => { setFehler(null); try { await rest('/auftrag/' + id + '/' + was, { method: 'POST', body: { mandant: MANDANT } }); laden() } catch (e) { setFehler(String(e && e.message || e)) } }
  const farbe = (s) => s === 'done' ? 'text-(--ui-text-success)' : s === 'blocked' ? 'text-(--ui-text-warning)' : s === 'running' ? 'text-(--ui-text-info)' : leise
  return jsxs('div', { className: rahmen + ' p-2 text-xs flex flex-col gap-1 shrink-0', children: [
    jsxs('div', { className: 'flex items-center gap-2', children: [
      jsx('span', { className: 'font-medium', children: 'Aufträge an die KI (Kanban)' }),
      jsx('span', { className: leise, children: liste.filter((a) => a.status !== 'archived').length + ' offen' }),
      jsx('span', { className: 'flex-1' }),
      jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setOffen(!offen), children: offen ? 'schließen' : '+ Auftrag' })
    ] }),
    offen ? jsxs('div', { className: 'flex flex-col gap-1', children: [
      jsx(Input, { placeholder: 'Titel (z. B. Belegstapel in Quelle links sichten und Dubletten listen)', value: titel, onChange: (e) => setTitel(e.target.value), onKeyDown: (e) => { if (e.key === 'Enter') anlegen() } }),
      jsx('textarea', { className: 'rounded border border-(--ui-stroke-secondary) bg-transparent p-1', style: { minHeight: 60 }, placeholder: 'Auftragstext — was genau getan werden soll. Die Karte bleibt gesperrt, bis du sie freigibst.', value: text, onChange: (e) => setText(e.target.value) }),
      jsxs('div', { className: 'flex gap-2 items-center', children: [
        jsx('span', { className: leise, children: 'Agent:' }),
        jsxs('select', { className: 'rounded border border-(--ui-stroke-secondary) bg-transparent px-1', value: assignee, onChange: (e) => setAssignee(e.target.value), children: (PROFILE_GELADEN ? PROFILE : ['schnapper', 'scotty', 'monk', 'default']).map((p) => jsx('option', { value: p, children: p }, p)) }),
        jsx(Button, { size: 'sm', disabled: laeuft || !titelEff, onClick: anlegen, children: laeuft ? 'lege an …' : !titelEff ? 'erst Titel oder Text eingeben' : 'Karte anlegen (gesperrt)' }),
        jsx('span', { className: leise, children: 'Die KI startet erst nach „Freigeben“ in der Liste.' })
      ] })
    ] }) : null,
    fehler ? jsx('div', { className: 'text-destructive', children: fehler }) : null,
    ...liste.filter((a) => a.status !== 'archived').slice(0, 12).map((a) => jsxs('div', { className: 'flex items-center gap-2', children: [
      jsx('span', { className: 'font-mono ' + farbe(a.status), children: a.status }),
      jsx('span', { className: 'font-mono', children: a.id }),
      jsx('span', { className: 'flex-1 truncate', title: a.ergebnis_kommentar || a.zusammenfassung || '', children: a.titel + (a.zusammenfassung ? ' — ' + a.zusammenfassung : a.ergebnis_kommentar ? ' — ' + a.ergebnis_kommentar : '') }),
      a.status === 'blocked' ? jsx(Button, { size: 'sm', onClick: () => tu(a.id, 'freigeben'), children: 'Freigeben' }) : null,
      ['done', 'blocked', 'review'].includes(a.status) ? jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => tu(a.id, 'archivieren'), children: 'Archivieren' }) : null
    ] }, a.id))
  ] })
}

function TrovePage({ nur }) {
  const [quellen, setQuellen] = useState([]); const [remotes, setRemotes] = useState([]); const [status, setStatus] = useState(null)
  const [links, setLinksRoh] = useState(() => storage.get('links', { quelle: '', pfad: '' })); const [rechts, setRechtsRoh] = useState(() => storage.get('rechts', { quelle: '', pfad: '' }))
  const [auswahlL, setAuswahlL] = useState([]); const [auswahlR, setAuswahlR] = useState([]); const [neuLaden, setNeuLaden] = useState(0); const [lauf, setLauf] = useState(null)
  const setLinks = (z) => { storage.set('links', z); setLinksRoh(z) }; const setRechts = (z) => { storage.set('rechts', z); setRechtsRoh(z) }
  // Quellen holen; antwortet das Backend gerade nicht (Neustart), bis 60 s wiederholen statt die Quelle stumm zu verlieren
  const quellenLaden = useCallback(async () => { for (let i = 0; i < 30; i++) { try { const r = await rest('/quellen'); setQuellen(r.quellen); setRemotes(r.rclone_remotes || []); return } catch (e) { await new Promise((ok) => setTimeout(ok, 2000)) } } }, [])
  const statusLaden = useCallback(() => rest('/status').then((s) => { statusUebernehmen(s); setStatus(s) }).catch(() => setStatus({ fehler: true })), [])
  useEffect(() => { quellenLaden(); statusLaden() }, [])
  // nach "Trove-Backend neu laden": Quellen, Status und Listen selbst neu holen (kein Seitenwechsel noetig)
  const laedt = useBackendLaedt(); const warLaedt = useRef(false)
  useEffect(() => { if (laedt) { warLaedt.current = true; return } if (warLaedt.current) { warLaedt.current = false; quellenLaden(); statusLaden(); setNeuLaden((n) => n + 1) } }, [laedt])
  const aktion = async (art, richtung) => {
    const von = richtung === 'lr' ? links : rechts, nach = richtung === 'lr' ? rechts : links, namen = richtung === 'lr' ? auswahlL : auswahlR
    if (!von.quelle || !nach.quelle) { host.notify({ kind: 'warning', message: 'Trove: beide Seiten brauchen eine Quelle.' }); return }
    if (art !== 'sync' && namen.length === 0) { host.notify({ kind: 'warning', message: 'Trove: erst Einträge markieren.' }); return }
    setLauf({ art, laeuft: true })
    try {
      const r = await rest('/aktion', { method: 'POST', body: { mandant: MANDANT, art, von: { quelle: von.quelle, pfad: von.pfad }, nach: { quelle: nach.quelle, pfad: nach.pfad }, namen: art === 'sync' ? [] : namen }, timeoutMs: 3600000 })
      setLauf({ art, ergebnis: r.ergebnis, lauf: r.lauf }); setNeuLaden((n) => n + 1); setAuswahlL([]); setAuswahlR([])
      host.notify({ kind: 'info', message: 'Trove: ' + art + ' fertig (' + r.lauf + ')' })
    } catch (e) { setLauf({ art, fehler: String(e && e.message || e) }) }
  }
  const seiteL = jsx(Seite, { seite: 'links', quellen, remotes, zustand: links, setZustand: setLinks, auswahl: auswahlL, setAuswahl: setAuswahlL, neuLaden, onQuellenGeaendert: quellenLaden })
  const seiteR = jsx(Seite, { seite: 'rechts', quellen, remotes, zustand: rechts, setZustand: setRechts, auswahl: auswahlR, setAuswahl: setAuswahlR, neuLaden, onQuellenGeaendert: quellenLaden })
  const mailAbholen = async () => {
    setLauf({ art: 'mail abholen', laeuft: true })
    try { const r = await rest('/mail/abholen', { method: 'POST', body: { mandant: MANDANT }, timeoutMs: 7200000 }); setLauf({ art: 'mail abholen', ergebnis: (r.ergebnis || []).map((z) => ({ name: z.postfach, mails: z.mails, neu: z.neu, anhaenge_neu: z.anhaenge_neu, fehler: z.fehler })) }) }
    catch (e) { setLauf({ art: 'mail abholen', fehler: String(e && e.message || e) }) }
  }
  const spiegeln = async () => {
    setLauf({ art: 'ERPNext-Spiegel', laeuft: true })
    try { const r = await rest('/spiegeln', { method: 'POST', body: { mandant: MANDANT }, timeoutMs: 3600000 }); setLauf({ art: 'ERPNext-Spiegel', ergebnis: [{ name: MANDANT, ereignisse: r.gespiegelt, bis_nr: r.letzte_nr, belege: r.belege_aktualisiert }] }) }
    catch (e) { setLauf({ art: 'ERPNext-Spiegel', fehler: String(e && e.message || e) }) }
  }
  const unterschriftenHolen = async () => {
    setLauf({ art: 'Unterschriften holen', laeuft: true })
    try { const r = await rest('/docuseal/abholen', { method: 'POST', body: { mandant: MANDANT }, timeoutMs: 600000 }); setLauf({ art: 'Unterschriften holen', ergebnis: (r.vorgaenge || []).map((v) => ({ name: 'Vorgang ' + v.vorgang, status: v.status, an: v.an, dateien: (v.dateien || []).length || (v.abgeholt ? 'abgeholt' : 0) })) }) }
    catch (e) { setLauf({ art: 'Unterschriften holen', fehler: String(e && e.message || e) }) }
  }
  const suche = jsx(Suche, {})
  // Zeile 1: Titel + Mandant links, Suche rechts
  const kopf = jsxs('div', { 'data-zeile': 'kopf', style: { ...LEISTE, alignItems: 'flex-start', columnGap: 16 }, children: [
    jsxs('div', { style: { display: 'flex', alignItems: 'baseline', gap: 8, paddingTop: 2 }, children: [
      jsx('span', { style: { fontSize: 18, fontWeight: 600 }, children: 'Trove' }),
      jsx('span', { style: LEISE_STIL, children: 'Mandant ' + ((status && status.mandant) || MANDANT || '…') })] }),
    jsx('div', { style: { flex: '1 1 320px', maxWidth: 720, marginLeft: 'auto', minWidth: 0 }, children: suche })
  ] })
  // Zeile 2: nur Knoepfe in benannten Gruppen; was im Kundenmodus nicht eingerichtet ist, fehlt ganz
  const werkzeuge = jsxs('div', { 'data-zeile': 'werkzeuge', style: LEISTE, children: [
    gruppe('Ansicht', [knopf('nur links', () => host.navigate('/trove/links'), { title: 'nur die linke Seite (z. B. im zweiten Fenster)', an: nur === 'links' }),
      knopf('nur rechts', () => host.navigate('/trove/rechts'), { title: 'nur die rechte Seite (z. B. im zweiten Fenster)', an: nur === 'rechts' }),
      knopf('beide', () => host.navigate('/trove'), { title: 'beide Seiten nebeneinander', an: !nur })]),
    gruppe('Holen', [ZEIGE.mail ? knopf('Mail abholen', mailAbholen, { title: 'F5: alle Postfaecher abholen (Mails als .eml, Anhaenge mit Pruefsumme ins Belegprotokoll)' }) : null,
      ZEIGE.docuseal ? knopf('Unterschriften holen', unterschriftenHolen, { title: 'F7: Stand aller Unterschriften-Vorgänge holen; fertige PDFs + Audit-Log werden abgelegt und protokolliert' }) : null]),
    gruppe('Abgleich', [ZEIGE.erpnext ? knopf('ERPNext-Spiegel', spiegeln, { title: 'F11: Belegprotokoll nach ERPNext spiegeln' }) : null])
  ] })
  // Mitte erst nach /status: vorher neutraler Platzhalter (weder Kopier-Knoepfe noch Kundenhinweis blitzen auf)
  const mitte = !status || status.fehler ? jsx('div', { 'data-mitte': 'wartet', className: 'flex flex-col justify-center items-center px-1 text-xs w-28 text-center ' + leise, children: status ? '–' : 'lädt …' }) : KARTENZIEL === 'wiki' ? jsx('div', { className: 'flex flex-col justify-center items-center px-1 text-xs w-28 text-center ' + leise, children: 'Deine Ablage wird nur gelesen. Kopieren und Verschieben sind hier abgeschaltet.' }) : jsxs('div', { className: 'flex flex-col gap-2 justify-center items-center px-1', children: [
    jsx(Button, { size: 'sm', title: 'markierte Einträge links verifiziert nach rechts kopieren', onClick: () => aktion('kopieren', 'lr'), children: 'Kopieren →' }),
    jsx(Button, { size: 'sm', title: 'markierte Einträge links nach rechts verschieben (Quelle erst nach Gegenprobe entfernt)', onClick: () => aktion('verschieben', 'lr'), children: 'Verschieben →' }),
    jsx(Button, { size: 'sm', title: 'ganzen linken Ordner nach rechts abgleichen (fehlende Dateien kopieren, nichts überschreiben)', onClick: () => aktion('sync', 'lr'), children: 'Sync →' }),
    jsx('div', { className: 'h-3' }),
    jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => aktion('kopieren', 'rl'), children: '← Kopieren' }),
    jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => aktion('verschieben', 'rl'), children: '← Verschieben' })
  ] })
  const ergebnis = lauf ? jsxs('div', { className: rahmen + ' p-2', children: [
    jsx('span', { className: 'font-medium', children: lauf.art + (lauf.laeuft ? ' läuft …' : lauf.fehler ? ' Fehler: ' : ' fertig' + (lauf.lauf ? ' · Lauf ' + lauf.lauf : '')) }),
    lauf.fehler ? jsx('span', { className: 'text-destructive', children: lauf.fehler }) : null,
    lauf.ergebnis ? jsx('span', { className: 'font-mono ml-2', children: lauf.ergebnis.map((r) => r.name + ': ' + Object.entries(r).filter(([k]) => k !== 'name').map(([k, v]) => k + '=' + v).join(' ')).join(' | ') }) : null
  ] }) : null
  // Zeile 3: Meldungen (Verbindung, Lauf-Ergebnis, Fehler) – nie zwischen den Knoepfen
  const verbindung = !status ? 'verbinde …' : status.fehler ? 'Backend nicht erreichbar' : 'Backend verbunden'
  const meldungen = jsxs('div', { 'data-zeile': 'meldungen', role: 'status', style: { display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12 }, children: [
    jsxs('div', { style: LEISTE, children: [
      jsx(Badge, { title: status && status.stufe ? 'Stufen: ' + status.stufe : undefined, children: verbindung }),
      status && status.protokolle ? jsx('span', { style: LEISE_STIL, children: 'Belegprotokoll ' + status.protokolle.map((p) => p.mandant + ': ' + p.zeilen + ' Zeilen').join(' · ') }) : null,
      status && status.fehler ? jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => { host.notify({ kind: 'info', message: 'Trove: Backend wird neu gestartet …' }); host.restartGateway() }, children: 'Backend neu starten' }) : null
    ] }),
    ergebnis
  ] })
  const auftraege = jsx(Auftraege, {})
  // Wurzel scrollt selbst (Host-Wrapper ist display:contents); Mittelteil schrumpft nie unter seinen Inhalt, sonst laeuft er ueber den Auftragsbereich
  const wurzel = 'flex h-full min-h-0 w-full min-w-0 flex-col gap-2 p-3 overflow-auto'
  const feld = (inhalt) => jsx('div', { className: 'flex-1 flex flex-col min-w-0', style: { minHeight: 560 }, children: inhalt })
  const spalte = (inhalt) => jsx('div', { style: { flex: '1 1 300px', minWidth: 0, display: 'flex', flexDirection: 'column' }, children: inhalt })
  if (nur === 'links') return jsxs('div', { className: wurzel, children: [kopf, werkzeuge, meldungen, feld(seiteL), auftraege] })
  if (nur === 'rechts') return jsxs('div', { className: wurzel, children: [kopf, werkzeuge, meldungen, feld(seiteR), auftraege] })
  return jsxs('div', { className: wurzel, children: [kopf, werkzeuge, meldungen,
    // Spalten brechen bei schmalem Fenster untereinander um, statt seitlich ueber den Rand zu laufen
    jsxs('div', { 'data-spalten': true, style: { flex: '1 0 auto', display: 'flex', flexWrap: 'wrap', gap: 8, minWidth: 0, minHeight: 560 }, children: [spalte(seiteL), mitte, spalte(seiteR)] }), auftraege] })
}

export default {
  id: ID,
  name: 'Trove',
  description: 'Datei-Zentrale: Quellen einbinden, zwei Seiten mit Vorschau, kopieren/verschieben/sync über das Belegprotokoll.',
  register(ctx) {
    rest = ctx.rest; storage = ctx.storage
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/trove' }, render: () => jsx(TrovePage, {}) },
      { id: 'page-links', area: ROUTES_AREA, data: { path: '/trove/links' }, render: () => jsx(TrovePage, { nur: 'links' }) },
      { id: 'page-rechts', area: ROUTES_AREA, data: { path: '/trove/rechts' }, render: () => jsx(TrovePage, { nur: 'rechts' }) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, order: 71, data: { codicon: 'archive', label: 'Trove', path: '/trove' } },
      { id: 'open', area: PALETTE_AREA, data: { id: 'trove.open', label: 'Trove: Datei-Zentrale öffnen', keywords: ['trove', 'datei', 'beleg'], run: () => host.navigate('/trove') } }
    ])
  }
}
