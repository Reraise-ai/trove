// Trove — Hermes-Desktop-Tab (Disk-Plugin, kein Build). Quelle: plugin/desktop/plugin.js im Repo
// Zwei Seiten (links Quelle, rechts Ziel), Vorschau auf beiden Seiten, Aktionen laufen ueber das Belegprotokoll (F8).
// Je Datei: Lesen (F3 Treppe + F4 Verprobung), Zerlegen (F6 Stapel), Historie (F11 ERPNext-Spiegel), Unterschrift (F7 DocuSeal).
// Kopf: Suche ueber beide Welten (F15), Mail abholen (F5), ERPNext-Spiegel (F11), Unterschriften holen (F7).
// Multimonitor: Routen /trove/links und /trove/rechts zeigen je eine Seite; ein zweites Hermes-Fenster kann sie auf dem
// zweiten Monitor anzeigen (gleicher Zustand ueber ctx.storage).
import { host, Button, Badge, Input, SandboxedFrame, ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA } from '@hermes/plugin-sdk'
import { useState, useEffect, useCallback } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'trove'
let rest = null
let storage = null
const MANDANT = 'haus'

const fmtB = (n) => n == null ? '–' : n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : n < 1073741824 ? (n / 1048576).toFixed(1) + ' MB' : (n / 1073741824).toFixed(2) + ' GB'
const fmtT = (t) => t ? new Date(t * 1000).toLocaleString('de-DE') : ''
const rahmen = 'rounded border border-(--ui-stroke-secondary)'
const leise = 'text-(--ui-text-tertiary)'

function Zeile(label, wert) {
  return jsxs('div', { className: 'flex justify-between gap-3 text-xs', children: [jsx('span', { className: leise, children: label }), jsx('span', { className: 'font-mono truncate', title: String(wert ?? ''), children: String(wert ?? '–') })] })
}

function QuelleAnlegen({ remotes, onFertig }) {
  const [art, setArt] = useState('ordner'); const [name, setName] = useState(''); const [pfad, setPfad] = useState(''); const [remote, setRemote] = useState(remotes[0] || '')
  const [fehler, setFehler] = useState(null); const [laeuft, setLaeuft] = useState(false)
  const anlegen = async () => {
    setLaeuft(true); setFehler(null)
    try { await rest('/quellen', { method: 'POST', body: { mandant: MANDANT, art, name, pfad, remote } }); onFertig(true) } catch (e) { setFehler(String(e && e.message || e)) } finally { setLaeuft(false) }
  }
  return jsxs('div', { className: rahmen + ' p-2 flex flex-col gap-2 text-xs', children: [
    jsxs('div', { className: 'flex gap-2 items-center', children: [
      jsx('span', { className: leise, children: 'Neue Quelle:' }),
      jsx(Button, { size: 'sm', variant: art === 'ordner' ? 'default' : 'ghost', onClick: () => setArt('ordner'), children: 'Ordner' }),
      jsx(Button, { size: 'sm', variant: art === 'rclone' ? 'default' : 'ghost', onClick: () => setArt('rclone'), children: 'Online-Drive (rclone)' })
    ] }),
    jsx(Input, { placeholder: 'Anzeigename (optional)', value: name, onChange: (e) => setName(e.target.value) }),
    art === 'ordner'
      ? jsx(Input, { placeholder: 'Ordnerpfad, z. B. C:\\Daten\\Eingang', value: pfad, onChange: (e) => setPfad(e.target.value) })
      : jsxs('div', { className: 'flex gap-2', children: [
          jsx('select', { className: 'rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1', value: remote, onChange: (e) => setRemote(e.target.value), children: remotes.map((r) => jsx('option', { value: r, children: r + ':' }, r)) }),
          jsx(Input, { placeholder: 'Unterordner im Drive (optional)', value: pfad, onChange: (e) => setPfad(e.target.value) })
        ] }),
    remotes.length === 0 && art === 'rclone' ? jsx('div', { className: leise, children: 'Kein rclone-Remote eingerichtet. Auf dem Server: rclone config (Google Drive, Nextcloud, Mega, SFTP …).' }) : null,
    fehler ? jsx('div', { className: 'text-(--ui-text-danger)', children: fehler }) : null,
    jsxs('div', { className: 'flex gap-2', children: [jsx(Button, { size: 'sm', disabled: laeuft, onClick: anlegen, children: laeuft ? 'prüfe …' : 'Einbinden' }), jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => onFertig(false), children: 'Abbrechen' })] })
  ] })
}

function Vorschau({ quelle, eintrag }) {
  const [v, setV] = useState(null)
  useEffect(() => {
    setV(null)
    if (!quelle || !eintrag || eintrag.ordner) return
    let aktiv = true
    rest('/vorschau?quelle=' + encodeURIComponent(quelle) + '&pfad=' + encodeURIComponent(eintrag.rel)).then((r) => { if (aktiv) setV(r) }).catch((e) => { if (aktiv) setV({ art: 'fehler', hinweis: String(e && e.message || e) }) })
    return () => { aktiv = false }
  }, [quelle, eintrag && eintrag.rel])
  if (!eintrag) return jsx('div', { className: leise + ' text-xs p-2', children: 'Datei anklicken für Vorschau.' })
  if (eintrag.ordner) return jsx('div', { className: leise + ' text-xs p-2', children: 'Ordner: ' + eintrag.name })
  if (!v) return jsx('div', { className: leise + ' text-xs p-2', children: 'lade Vorschau …' })
  if (v.art === 'bild') return jsx('img', { src: v.data, alt: eintrag.name, className: 'max-h-full max-w-full object-contain' })
  if (v.art === 'pdf') return jsx(SandboxedFrame, { src: v.data, title: eintrag.name, className: 'w-full h-full' })
  if (v.art === 'text') return jsx('pre', { className: 'text-[11px] whitespace-pre-wrap break-all p-2 font-mono', children: v.inhalt + (v.abgeschnitten ? '\n… (gekürzt)' : '') })
  return jsx('div', { className: leise + ' text-xs p-2', children: 'Keine Vorschau: ' + (v.hinweis || v.mime || '') + ' · ' + fmtB(v.groesse) })
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
    erg ? (erg.laeuft ? jsx('div', { children: 'lege Vorgang an …' }) : erg.fehler ? jsx('div', { className: 'text-(--ui-text-danger)', children: erg.fehler }) : jsxs('div', { children: ['Vorgang ' + erg.vorgang + ' angelegt (' + erg.vorlage + ' an ' + erg.an + '). Link: ', jsx('span', { className: 'font-mono select-all', children: erg.link }), mail ? '' : ' — Link selbst übergeben (keine Mail).'] })) : null
  ] })
}

function BelegErgebnis({ r, zurueck }) {
  const z = (k, v) => jsxs('div', { children: [jsx('span', { className: leise, children: k + ': ' }), jsx('span', { children: String(v ?? '–') })] }, k)
  let inhalt
  if (r.laeuft) inhalt = jsx('div', { children: r.art + ' läuft … (OCR kann je Seite einige Sekunden brauchen)' })
  else if (r.fehler) inhalt = jsx('div', { className: 'text-(--ui-text-danger)', children: r.fehler })
  else if (r.art === 'lesen') inhalt = jsxs('div', { children: [
    z('Methode', r.methode + ', ' + r.seiten + ' Seiten, ' + r.dauer_s + ' s'), z('sha256', r.sha256),
    ...Object.entries(r.felder || {}).map(([k, v]) => z(k, v)),
    jsx('div', { className: 'mt-1 font-medium', children: 'Verprobung: ' + (r.verprobung.sicher ? 'bestanden (sicher)' : r.verprobung.bestanden ? 'bestanden' : 'Prüfliste') }),
    ...(r.verprobung.gruende || []).map((g, i) => jsx('div', { children: '· ' + g }, i)),
    jsx('div', { className: leise + ' mt-1', children: 'Zwilling: ' + r.zwilling }),
    jsx('pre', { className: 'mt-1 whitespace-pre-wrap text-[10px] ' + leise, children: r.text_anfang })
  ] })
  else if (r.art === 'stapel') inhalt = jsxs('div', { children: [
    z('Stapel', r.datei + ' → ' + (r.ziel || '') + ' · ' + (r.belege || []).length + ' Belege aus ' + r.seiten + ' Seiten, ' + r.dauer_s + ' s'),
    ...(r.belege || []).map((b) => jsx('div', { children: '#' + b.nr + ' Seiten ' + b.seiten.join('–') + ' ' + (b.nummer ? 'Nr ' + b.nummer + ' ' : '') + b.arten.join('/') + (b.buchungsfassung ? ' (Buchungsfassung ohne Anhang)' : '') + ' → ' + b.datei }, b.nr))
  ] })
  else if (r.art === 'unterschrift') inhalt = jsx(Unterschrift, { r })
  else inhalt = jsxs('div', { children: [
    z('sha256', r.sha256),
    r.fehler ? z('ERPNext', r.fehler) : null,
    r.beleg ? jsxs('div', { className: 'mt-1', children: [jsx('div', { className: 'font-medium', children: 'Beleg (ERPNext-Spiegel)' }),
      ...['dateiname', 'letzter_ort', 'erste_sicht', 'letzte_bewegung', 'ereignisse', 'belegdatum', 'brutto', 'netto', 'steuer', 'waehrung', 'rechnungsnummer', 'aussteller_kennung', 'lesemethode', 'verprobung'].filter((k) => r.beleg[k] != null && r.beleg[k] !== '').map((k) => z(k, r.beleg[k]))] }) : jsx('div', { className: leise, children: 'kein Beleg-Satz im Spiegel (noch nicht gespiegelt oder nie gelesen)' }),
    r.zwilling ? jsxs('div', { className: 'mt-1', children: [jsx('div', { className: 'font-medium', children: 'Zwilling (lokal): ' + r.zwilling.methode }), ...Object.entries(r.zwilling.felder || {}).map(([k, v]) => z(k, v))] }) : null,
    jsx('div', { className: 'font-medium mt-1', children: 'Ereignisse: ' + (Array.isArray(r.ereignisse) ? r.ereignisse.length : '–') }),
    ...(Array.isArray(r.ereignisse) ? r.ereignisse.slice(0, 40).map((e) => jsx('div', { children: '#' + e.nr + ' ' + e.zeit + ' ' + e.ereignis + ' ' + (e.quelle || '') + (e.ziel ? ' → ' + e.ziel : '') + (e.meldung ? ' [' + e.meldung + ']' : '') }, e.nr)) : [])
  ] })
  return jsxs('div', { className: 'text-[11px] p-2 font-mono', children: [jsx('div', { className: 'font-medium mb-1', children: r.art + ': ' + (r.datei || '') }), inhalt, jsx(Button, { size: 'sm', variant: 'ghost', onClick: zurueck, children: 'zurück zur Vorschau' })] })
}

function Seite({ seite, quellen, remotes, zustand, setZustand, auswahl, setAuswahl, neuLaden, onQuellenGeaendert }) {
  const [liste, setListe] = useState(null); const [fehler, setFehler] = useState(null); const [neu, setNeu] = useState(false); const [aktiv, setAktiv] = useState(null)
  const [filter, setFilter] = useState('alle'); const [spur, setSpur] = useState(null)
  const q = quellen.find((x) => x.id === zustand.quelle)
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
  const toggle = (name) => setAuswahl(auswahl.includes(name) ? auswahl.filter((n) => n !== name) : [...auswahl, name])
  const registrieren = async () => { setFehler(null); try { const r = await rest('/register', { method: 'POST', body: { mandant: MANDANT, quelle: q.id, pfad: zustand.pfad || '' } }); host.notify({ kind: 'info', message: 'Trove: Register – ' + r.neu + ' neue, ' + r.gesamt + ' gesamt' }); laden() } catch (e) { setFehler(String(e && e.message || e)) } }
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
      else setSpur({ art, datei: e.name, ...(await rest('/historie?datei=' + encodeURIComponent(e.pfad))) })
      if (art !== 'historie') laden()
    } catch (x) { setSpur({ art, datei: e.name, fehler: String(x && x.message || x) }) }
  }
  return jsxs('div', { className: 'flex flex-col gap-2 min-w-0 h-full', children: [
    jsxs('div', { className: 'flex gap-2 items-center flex-wrap', children: [
      jsx('span', { className: 'text-xs font-medium', children: seite === 'links' ? 'Quelle (links)' : 'Ziel (rechts)' }),
      jsxs('select', { className: 'rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-xs max-w-[60%]', value: zustand.quelle || '', onChange: (e) => { setZustand({ quelle: e.target.value, pfad: '' }); setAuswahl([]); setAktiv(null) }, children: [
        jsx('option', { value: '', children: '– Quelle wählen –' }),
        ...quellen.map((x) => jsx('option', { value: x.id, children: (x.art === 'rclone' ? '☁ ' : '📁 ') + x.name + (x.nur_lesen ? ' (nur lesen)' : '') }, x.id))
      ] }),
      jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setNeu(!neu), children: '+ Quelle' }),
      q ? jsx(Button, { size: 'sm', variant: 'ghost', title: 'Quelle aus der Liste nehmen (Dateien bleiben)', onClick: async () => { await rest('/quellen/' + q.id, { method: 'DELETE' }); setZustand({ quelle: '', pfad: '' }); onQuellenGeaendert() }, children: '×' }) : null
    ] }),
    neu ? jsx(QuelleAnlegen, { remotes, onFertig: (ok) => { setNeu(false); if (ok) onQuellenGeaendert() } }) : null,
    q ? jsxs('div', { className: 'flex gap-1 items-center text-xs flex-wrap', children: [
      jsx('button', { className: 'underline', onClick: () => geh(''), children: q.art === 'rclone' ? q.remote + ':' + (q.pfad || '') : q.pfad }),
      ...teile.map((t, i) => jsxs('span', { children: [' / ', jsx('button', { className: 'underline', onClick: () => geh(teile.slice(0, i + 1).join('/')), children: t })] }, i)),
      jsx('span', { className: 'flex-1' }),
      q.art === 'ordner' ? jsx(Button, { size: 'sm', variant: 'ghost', title: 'Prüfsummen rechnen (Dubletten erkennen)', onClick: registrieren, children: liste && liste.indexiert ? 'Register ✓' : 'Indexieren' }) : null,
      q.art === 'ordner' ? jsx(Button, { size: 'sm', variant: 'ghost', title: 'F2: Stand aufnehmen und mit dem letzten Lauf vergleichen (neu / weg / verschoben / außen geändert)', onClick: aenderungen, children: 'Änderungen prüfen' }) : null,
      jsxs('select', { className: 'rounded border border-(--ui-stroke-secondary) bg-transparent px-1 text-xs', value: filter, onChange: (e) => setFilter(e.target.value), children: [jsx('option', { value: 'alle', children: 'alle' }), jsx('option', { value: 'nur', children: 'nur Dubletten' }), jsx('option', { value: 'ohne', children: 'ohne Dubletten' })] })
    ] }) : null,
    fehler ? jsx('div', { className: 'text-xs text-(--ui-text-danger)', children: fehler }) : null,
    f2 ? jsxs('div', { className: rahmen + ' p-2 text-xs', children: [
      jsx('span', { className: 'font-medium', children: f2.laeuft ? 'Änderungen werden geprüft …' : 'F2 Lauf ' + f2.lauf + (f2.erster_lauf ? ' (Grundstand aufgenommen)' : '') + ': ' + Object.entries(f2.zusammen || {}).map(([k, v]) => k + ' ' + v).join(' · ') }),
      !f2.laeuft ? jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setF2(null), children: '×' }) : null,
      ...(!f2.laeuft ? ['neu', 'weg', 'verschoben', 'aussen_geaendert', 'durch_trove'].flatMap((k) => (f2.befunde[k] || []).slice(0, 15).map((b, i) => jsx('div', { className: 'font-mono truncate', children: k + ': ' + (b.pfad || (b.von + ' → ' + b.nach)) }, k + i))) : [])
    ] }) : null,
    jsxs('div', { className: rahmen + ' flex-1 min-h-[160px] overflow-auto text-xs', children: [
      !q ? jsx('div', { className: leise + ' p-2', children: 'Keine Quelle gewählt.' }) : !liste ? jsx('div', { className: leise + ' p-2', children: 'lade …' }) : null,
      zustand.pfad ? jsx('div', { className: 'px-2 py-1 cursor-pointer hover:bg-(--ui-bg-secondary)', onDoubleClick: () => geh(teile.slice(0, -1).join('/')), children: '⬆ ..' }) : null,
      ...sichtbar.map((e) => jsxs('div', { className: 'flex items-center gap-2 px-2 py-0.5 cursor-pointer hover:bg-(--ui-bg-secondary) ' + (aktiv === e.name ? 'bg-(--ui-bg-secondary)' : ''),
        onClick: () => { setAktiv(e.name); setSpur(null) }, onDoubleClick: () => e.ordner ? geh(e.rel) : spurZeigen(e), children: [
        jsx('input', { type: 'checkbox', checked: auswahl.includes(e.name), onChange: () => toggle(e.name), onClick: (ev) => ev.stopPropagation() }),
        jsx('span', { className: 'flex-1 truncate', title: e.pfad, children: (e.ordner ? '📁 ' : '') + e.name }),
        e.dublette ? jsx(Badge, { children: e.fundorte + '× vorhanden' }) : null,
        jsx('span', { className: leise + ' w-16 text-right', children: e.ordner ? '' : fmtB(e.groesse) }),
        jsx('span', { className: leise + ' w-32 text-right', children: fmtT(e.mtime) })
      ] }, e.name))
    ] }),
    jsxs('div', { className: 'flex gap-2 text-xs items-center', children: [
      jsx('span', { className: leise, children: liste ? liste.eintraege.length + ' Einträge, ' + auswahl.length + ' markiert' : '' }),
      jsx('span', { className: 'flex-1' }),
      auswahl.length ? jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setAuswahl([]), children: 'Auswahl leeren' }) : null,
      liste ? jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setAuswahl(liste.eintraege.map((e) => e.name)), children: 'alle' }) : null,
      aktiv ? jsx(Button, { size: 'sm', variant: 'ghost', title: 'F3/F4: Lese-Treppe (E-Rechnung, Textlayer, sonst OCR lokal) und feste Verprobung; Zwilling + Belegprotokoll', onClick: () => belegAktion('lesen'), children: 'Lesen' }) : null,
      aktiv ? jsx(Button, { size: 'sm', variant: 'ghost', title: 'F6: mehrseitiges PDF in einzelne Belege zerlegen (Stirling-PDF)', onClick: () => belegAktion('stapel'), children: 'Zerlegen' }) : null,
      aktiv ? jsx(Button, { size: 'sm', variant: 'ghost', title: 'F11: Geschäftshistorie dieses Inhalts aus dem ERPNext-Spiegel', onClick: () => belegAktion('historie'), children: 'Historie' }) : null
      aktiv ? jsx(Button, { size: 'sm', variant: 'ghost', title: 'F7: diese Datei als Original zu einem Unterschriften-Vorgang (DocuSeal-Vorlage) geben', onClick: () => belegAktion('unterschrift'), children: 'Unterschrift' }) : null
    ] }),
    jsx('div', { className: rahmen + ' h-[220px] overflow-auto', children: spur && spur.art
      ? jsx(BelegErgebnis, { r: spur, zurueck: () => setSpur(null) })
      : spur
      ? jsxs('div', { className: 'text-[11px] p-2 font-mono', children: [
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
  return jsxs('div', { className: 'flex flex-col gap-1', children: [
    jsxs('div', { className: 'flex gap-2 items-center', children: [
      jsx(Input, { placeholder: 'Suche über beide Welten: Dateiname, Rechnungsnummer, Absender, sha256 …', value: q, onChange: (e) => setQ(e.target.value), onKeyDown: (e) => { if (e.key === 'Enter') suchen() }, className: 'text-xs flex-1' }),
      jsx(Button, { size: 'sm', onClick: suchen, children: 'Suchen' }),
      r ? jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setOffen(!offen), children: offen ? 'zu' : 'auf' }) : null
    ] }),
    offen && r ? jsx('div', { className: rahmen + ' max-h-[260px] overflow-auto p-2 text-[11px] font-mono', children:
      r.laeuft ? 'suche …' : r.fehler ? jsx('span', { className: 'text-(--ui-text-danger)', children: r.fehler }) : r.hinweis ? r.hinweis : jsxs('div', { children: [
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
  const anlegen = async () => { setLaeuft(true); setFehler(null); try { await rest('/auftrag', { method: 'POST', body: { mandant: MANDANT, titel, text, assignee, kontext: { links: storage.get('links', {}), rechts: storage.get('rechts', {}) } } }); setTitel(''); setText(''); setOffen(false); laden() } catch (e) { setFehler(String(e && e.message || e)) } finally { setLaeuft(false) } }
  const tu = async (id, was) => { setFehler(null); try { await rest('/auftrag/' + id + '/' + was, { method: 'POST', body: { mandant: MANDANT } }); laden() } catch (e) { setFehler(String(e && e.message || e)) } }
  const farbe = (s) => s === 'done' ? 'text-(--ui-text-success)' : s === 'blocked' ? 'text-(--ui-text-warning)' : s === 'running' ? 'text-(--ui-text-info)' : leise
  return jsxs('div', { className: rahmen + ' p-2 text-xs flex flex-col gap-1', children: [
    jsxs('div', { className: 'flex items-center gap-2', children: [
      jsx('span', { className: 'font-medium', children: 'Aufträge an die KI (Kanban)' }),
      jsx('span', { className: leise, children: liste.filter((a) => a.status !== 'archived').length + ' offen' }),
      jsx('span', { className: 'flex-1' }),
      jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setOffen(!offen), children: offen ? 'schließen' : '+ Auftrag' })
    ] }),
    offen ? jsxs('div', { className: 'flex flex-col gap-1', children: [
      jsx(Input, { placeholder: 'Titel (z. B. Belegstapel in Quelle links sichten und Dubletten listen)', value: titel, onChange: (e) => setTitel(e.target.value) }),
      jsx('textarea', { className: 'rounded border border-(--ui-stroke-secondary) bg-transparent p-1 min-h-[60px]', placeholder: 'Auftragstext — was genau getan werden soll. Die Karte bleibt gesperrt, bis du sie freigibst.', value: text, onChange: (e) => setText(e.target.value) }),
      jsxs('div', { className: 'flex gap-2 items-center', children: [
        jsx('span', { className: leise, children: 'Agent:' }),
        jsxs('select', { className: 'rounded border border-(--ui-stroke-secondary) bg-transparent px-1', value: assignee, onChange: (e) => setAssignee(e.target.value), children: ['schnapper', 'scotty', 'monk', 'default'].map((p) => jsx('option', { value: p, children: p }, p)) }),
        jsx(Button, { size: 'sm', disabled: laeuft || !titel, onClick: anlegen, children: laeuft ? 'lege an …' : 'Karte anlegen (gesperrt)' })
      ] })
    ] }) : null,
    fehler ? jsx('div', { className: 'text-(--ui-text-danger)', children: fehler }) : null,
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
  const quellenLaden = useCallback(() => rest('/quellen').then((r) => { setQuellen(r.quellen); setRemotes(r.rclone_remotes || []) }).catch(() => {}), [])
  useEffect(() => { quellenLaden(); rest('/status').then(setStatus).catch(() => setStatus({ fehler: true })) }, [])
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
  const kopf = jsxs('div', { className: 'flex items-center gap-3 flex-wrap', children: [
    jsx('div', { className: 'text-lg font-medium', children: 'Trove — Datei-Zentrale' }),
    jsx(Badge, { children: !status ? 'verbinde …' : status.fehler ? 'Backend nicht erreichbar' : 'meldet sich · ' + status.stufe }),
    status && status.protokolle ? jsx('span', { className: leise + ' text-xs', children: status.protokolle.map((p) => p.mandant + ': ' + p.zeilen + ' Zeilen').join(' · ') }) : null,
    jsx('span', { className: 'flex-1' }),
    jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => host.navigate('/trove/links'), children: 'nur links' }),
    jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => host.navigate('/trove/rechts'), children: 'nur rechts' }),
    jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => host.navigate('/trove'), children: 'beide' }),
    jsx(Button, { size: 'sm', variant: 'ghost', title: 'Zweites Hermes-Fenster öffnen (für den zweiten Monitor), dort Trove wählen', onClick: () => {
      const sid = host.state.focusedSessionId && host.state.focusedSessionId.get ? host.state.focusedSessionId.get() : null
      if (sid && typeof host.openSession === 'function') host.openSession(sid, { intent: 'window' })
      else host.notify({ kind: 'info', message: 'Zweites Fenster: eine Sitzung in neuem Fenster öffnen (Rechtsklick auf Sitzung), dort Trove → „nur rechts“.' })
    }, children: 'Zweites Fenster' }),
    jsx(Button, { size: 'sm', variant: 'ghost', title: 'F5: alle Postfaecher abholen (Mails als .eml, Anhaenge mit Pruefsumme ins Belegprotokoll)', onClick: async () => {
      setLauf({ art: 'mail abholen', laeuft: true })
      try { const r = await rest('/mail/abholen', { method: 'POST', body: { mandant: MANDANT }, timeoutMs: 7200000 }); setLauf({ art: 'mail abholen', ergebnis: (r.ergebnis || []).map((z) => ({ name: z.postfach, mails: z.mails, neu: z.neu, anhaenge_neu: z.anhaenge_neu, fehler: z.fehler })) }) }
      catch (e) { setLauf({ art: 'mail abholen', fehler: String(e && e.message || e) }) }
    }, children: 'Mail abholen' }),
    jsx(Button, { size: 'sm', variant: 'ghost', title: 'F11: Belegprotokoll nach ERPNext spiegeln', onClick: async () => {
      setLauf({ art: 'ERPNext-Spiegel', laeuft: true })
      try { const r = await rest('/spiegeln', { method: 'POST', body: { mandant: MANDANT }, timeoutMs: 3600000 }); setLauf({ art: 'ERPNext-Spiegel', ergebnis: [{ name: 'haus', ereignisse: r.gespiegelt, bis_nr: r.letzte_nr, belege: r.belege_aktualisiert }] }) }
      catch (e) { setLauf({ art: 'ERPNext-Spiegel', fehler: String(e && e.message || e) }) }
    }, children: 'ERPNext-Spiegel' }),
    jsx(Button, { size: 'sm', variant: 'ghost', title: 'F7: Stand aller Unterschriften-Vorgänge holen; fertige PDFs + Audit-Log werden abgelegt und protokolliert', onClick: async () => {
      setLauf({ art: 'Unterschriften holen', laeuft: true })
      try { const r = await rest('/docuseal/abholen', { method: 'POST', body: { mandant: MANDANT }, timeoutMs: 600000 }); setLauf({ art: 'Unterschriften holen', ergebnis: (r.vorgaenge || []).map((v) => ({ name: 'Vorgang ' + v.vorgang, status: v.status, an: v.an, dateien: (v.dateien || []).length || (v.abgeholt ? 'abgeholt' : 0) })) }) }
      catch (e) { setLauf({ art: 'Unterschriften holen', fehler: String(e && e.message || e) }) }
    }, children: 'Unterschriften holen' }),
    status && status.fehler ? jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => { host.notify({ kind: 'info', message: 'Trove: Backend wird neu gestartet …' }); host.restartGateway() }, children: 'Backend neu starten' }) : null
  ] })
  const mitte = jsxs('div', { className: 'flex flex-col gap-2 justify-center items-center px-1', children: [
    jsx(Button, { size: 'sm', title: 'markierte Einträge links verifiziert nach rechts kopieren', onClick: () => aktion('kopieren', 'lr'), children: 'Kopieren →' }),
    jsx(Button, { size: 'sm', title: 'markierte Einträge links nach rechts verschieben (Quelle erst nach Gegenprobe entfernt)', onClick: () => aktion('verschieben', 'lr'), children: 'Verschieben →' }),
    jsx(Button, { size: 'sm', title: 'ganzen linken Ordner nach rechts abgleichen (fehlende Dateien kopieren, nichts überschreiben)', onClick: () => aktion('sync', 'lr'), children: 'Sync →' }),
    jsx('div', { className: 'h-3' }),
    jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => aktion('kopieren', 'rl'), children: '← Kopieren' }),
    jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => aktion('verschieben', 'rl'), children: '← Verschieben' })
  ] })
  const ergebnis = lauf ? jsxs('div', { className: rahmen + ' p-2 text-xs', children: [
    jsx('span', { className: 'font-medium', children: lauf.art + (lauf.laeuft ? ' läuft …' : lauf.fehler ? ' Fehler: ' : ' fertig' + (lauf.lauf ? ' · Lauf ' + lauf.lauf : '')) }),
    lauf.fehler ? jsx('span', { className: 'text-(--ui-text-danger)', children: lauf.fehler }) : null,
    lauf.ergebnis ? jsx('span', { className: 'font-mono ml-2', children: lauf.ergebnis.map((r) => r.name + ': ' + Object.entries(r).filter(([k]) => k !== 'name').map(([k, v]) => k + '=' + v).join(' ')).join(' | ') }) : null
  ] }) : null
  const auftraege = jsx(Auftraege, {}); const suche = jsx(Suche, {})
  if (nur === 'links') return jsxs('div', { className: 'flex h-full flex-col gap-2 p-3', children: [kopf, suche, ergebnis, seiteL, auftraege] })
  if (nur === 'rechts') return jsxs('div', { className: 'flex h-full flex-col gap-2 p-3', children: [kopf, suche, ergebnis, seiteR, auftraege] })
  return jsxs('div', { className: 'flex h-full flex-col gap-2 p-3', children: [kopf, suche, ergebnis,
    jsxs('div', { className: 'flex-1 grid gap-2 min-h-0', style: { gridTemplateColumns: '1fr auto 1fr' }, children: [seiteL, mitte, seiteR] }), auftraege] })
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
