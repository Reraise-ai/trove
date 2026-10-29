"""Trove F16 — oeffentliche Belege aus dem Wissen als Karten zu reraise.ai hochladen (Tauschboerse, POST /api/x/cards).

Je Datei (Ordner oder einzelne Datei): Markdown-Zwilling aus <TROVE_DATEN>\\<mandant>\\wissen\\<sammlung>\\<sha16>_*.md
(pruefsumme im Kopf = sha256 des Originals) -> eine Karte (type evidence, layer evidenced) -> senden.
Harte Regel: nur Zwillinge mit sichtbarkeit "oeffentlich" — und kein Zwilling derselben Datei in irgendeiner Sammlung ist privat
(oeffentlich.privat_irgendwo, dieselbe Pruefung wie bei Brett-Karten in wissen.py).
Kein Zwilling -> "erst Ins Wissen", nichts raten. Karte nur aus der Positivliste oeffentlich.kartenfelder: nichts aus Belegtext
oder Dateiname; source = "Trove-Beleg sha256 <hex>"; jede Karte geht durch oeffentlich.pruefe_karte.
Gesendet wird nur, was nach der Probe bestaetigt wurde: --nur-sha <Datei> oder --bestaetigt sha,sha (ohne Liste und ohne --probe:
Fehler). Schluessel NUR ueber Hermes (Geheimnis TROVE_RERAISE_KEY, zugang.geheimnis); ohne Schluessel ist die Funktion aus: kein
Hochladen, keine Probe ueber die Plugin-Oberflaeche (dort nur ein Link auf reraise.ai). Trove legt nie selbst Schluessel an. Die Probe liest nur: kein Netz, kein Protokoll, kein Stand, keine Sperre.
Reraise kennt fuer Mitglieder kein Loeschen: ein Upload ist endgueltig. Darum nie wiederholen: vor JEDEM POST ein Abgleich per
GET ?q=<sha256> (gefunden -> "schon_da", gebucht; auch wenn die Stand-Datei verloren ist), dann steht die Pruefsumme in
reraise.json unter "unterwegs", nach der Buchung nicht mehr. Zurueckgesetzt wird nur bei klar abgewiesener Antwort
(400/401/403/413/422/429, Fehlerklasse Abgewiesen); alles andere (Netzfehler nach Start des POST, 3xx, 5xx, unlesbarer
Fehler-Body, Buchung gescheitert) -> "unklar", kein zweiter POST im selben Lauf: Dateien gleichen Inhalts (sha256) werden je Lauf
zusammengefasst (erste Fundstelle zaehlt, weitere -> "dublette", kein Netz), und eine sha256, die in diesem Lauf "unterwegs" war
(Lauf.offen), meldet _datei ohne Netz als "unklar". "unklar" geht direkt in die Stand-Datei, auch wenn das Protokoll gesperrt ist.
Alle Uebergaenge laufen ueber stand_setzen. Bestaetigung "sha256:karte_hash" (karte_hash aus der Probe) bindet an den Karteninhalt.
Stand je Mandant: <TROVE_DATEN>\\<mandant>\\reraise.json, ergaenzt aus F8 "versendet".
Laufsperre: gemeinsam.laufsperre(mandant) — dieselbe wie wissen.py; Privat-Pruefung und Senden liegen darin (-> LaufLaeuft).
Fehler und Rueckgabewert (main): Zusammenfassung immer als JSON; "abbruch" -> Rueckgabewert 2, sonst 0.
  Abbruch: Schluessel abgelehnt (401), Herkunft (403), Tageslimit; Start-Notiz oder Stand nicht lesbar (vor dem ersten Senden);
           Protokoll/Stand scheitern NACH erfolgreichem Senden (Karte existiert, Stand "unklar", naechster Lauf gleicht ab).
  Nur gezaehlt (fehler / protokoll_fehler, Lauf geht weiter): Datei-Fehler (4xx, kein Netz), Protokollfehler beim Eintrag
           "unklar" oder beim Fehler-Eintrag einer Datei, Protokollfehler bei der End-Notiz.
"""
import argparse
import contextlib
import dataclasses
import hashlib
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import belegprotokoll as bp
import gemeinsam as gm
import oeffentlich as oe
import zugang
from zugang import NichtEingerichtet

RERAISE = "https://reraise.ai"  # fixed; the self-tests point it at 127.0.0.1
VERSUCHE = 4
ABGEWIESEN = (400, 401, 403, 413, 422, 429)  # nur diese gelten als "nichts angelegt"; alles andere -> unklar
WERKZEUG = "reraise_hochladen.py"


class Abbruch(zugang.DienstFehler):
    """Gilt fuer den ganzen Lauf (Schluessel abgelehnt, Tageslimit, Protokoll/Stand nach Versand): weitermachen waere falsch."""


class Abgewiesen(zugang.DienstFehler):
    """Server hat klar abgelehnt (code in ABGEWIESEN): nichts angelegt, Stand darf zurueck."""

    def __init__(self, code, meldung):
        super().__init__(meldung)
        self.code = code


class AbgewiesenAbbruch(Abgewiesen, Abbruch):
    """Abgewiesen und fuer den ganzen Lauf (Schluessel, Herkunft, Tageslimit)."""


class Unklar(zugang.DienstFehler):
    """Die Anfrage ging raus, die Antwort fehlt: die Karte kann existieren."""


@dataclasses.dataclass
class Lauf:
    """Ein Lauf von hochladen(); prueft Mandant/Sammlung beim Anlegen. nur: bestaetigte sha256 -> karte_hash oder None
    (Menge wird zu dict; None in der Probe). index: oeffentlich.zwillings_index, einmal je Lauf."""
    mandant: str
    sammlung: str
    nur: Optional[dict] = None
    index: Optional[dict] = None
    offen: set = dataclasses.field(default_factory=set)  # sha256, die in diesem Lauf "unterwegs" gingen und nicht abgewiesen wurden
    name: str = dataclasses.field(init=False)

    def __post_init__(self):
        gm.pruefe_namen(self.mandant, self.sammlung)
        self.name = "reraise-" + time.strftime("%Y%m%d")
        if self.nur is not None and not isinstance(self.nur, dict):
            self.nur = dict.fromkeys(self.nur)


class _KeinUmleiten(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(_KeinUmleiten)


WISSEN_TEST = None  # self-test seam: callable(mandant) -> folder of the twins


def wissen_wurzel(mandant):
    """Folder whose sub-folders are the collections of markdown twins (as written by wissen.py: <wiki>/../raw/trove)."""
    if WISSEN_TEST is not None:
        return WISSEN_TEST(mandant)
    import wissenskarte as wk
    return os.path.dirname(wk.raw_ziel("x"))


def eingerichtet():
    """True only with a Reraise key in Hermes. No network."""
    return zugang.ist_gesetzt(zugang.RERAISE_KEY)


def _schluessel():
    s = zugang.geheimnis(zugang.RERAISE_KEY)
    if not s or len(s) > 100 or any(c.isspace() for c in s):
        raise NichtEingerichtet("no Reraise key in Hermes (%s): get one on reraise.ai and enter it in the Hermes settings of Trove"
                                % zugang.RERAISE_KEY)
    return s


def _prot(mandant):
    return gm.prot_mit_wiederholung(bp.pfad_fuer(mandant))


def _stand_pfad(mandant):
    return os.path.join(bp.WURZEL, mandant, "reraise.json")


def stand_lesen(mandant):
    p = _stand_pfad(mandant)
    s = {}
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            s = json.load(f)
    for k in ("karten", "unklar", "unterwegs"):
        s.setdefault(k, {})
    prot = bp.pfad_fuer(mandant)
    if os.path.exists(prot):
        for e in bp.eintraege(prot):
            ziel, sha = str(e.get("ziel") or ""), e.get("sha256")
            if e.get("ereignis") == "versendet" and e.get("werkzeug") == WERKZEUG and ziel.startswith("reraise:card/") and sha:
                stand_setzen(s, sha, "karten", s["karten"].get(sha) or {
                    "id": ziel.split("/", 1)[1], "no": e.get("nr_karte"), "url": e.get("url"), "zeit": e.get("zeit"),
                    "datei": os.path.basename(str(e.get("quelle") or "")), "sammlung": e.get("sammlung"), "aus": "protokoll"})
    return s


def stand_setzen(stand, sha, zustand, karte=None):
    """Einzige Stelle fuer Uebergaenge: sha steht danach in genau einem von karten/unklar/unterwegs (zustand None: in keinem).
    Speichert nicht (stand_lesen nutzt sie auch); Aufrufer: stand_speichern(mandant, stand_setzen(...))."""
    for k in ("karten", "unklar", "unterwegs"):
        stand[k].pop(sha, None)
    if zustand == "karten":
        stand["karten"][sha] = karte
    elif zustand:
        stand[zustand][sha] = bp.jetzt()
    return stand


def stand_speichern(mandant, s):
    gm.schreibe_atomar(_stand_pfad(mandant), json.dumps(s, ensure_ascii=False, indent=1))


def karte_bauen(sha, kopf):
    """Reraise-Karte nur aus der Positivliste des Zwilling-Kopfs (oeffentlich.kartenfelder, inkl. pruefe_karte)."""
    f = oe.kartenfelder(sha, kopf)
    karte = {"public": True, "type": "evidence", "layer": "evidenced", "language": "es" if kopf.get("waehrung") == "ARS" else "de",
             "title": f["titel"], "body": f["body"], "source": f["quelle"]}  # "Trove-Beleg " vorn: nie als URL-Schema gelesen
    if f["datum"]:
        karte["sourceDate"] = f["datum"]
    return karte


def karte_hash(karte):
    """sha256 ueber title und body der Karte (JSON-Liste: keine Mehrdeutigkeit an der Naht)."""
    return hashlib.sha256(json.dumps([karte["title"], karte["body"]], ensure_ascii=False).encode("utf-8")).hexdigest()


def _antwort(roh):
    try:
        d = json.loads(roh or b"{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def senden(karte, schluessel):
    """POST /api/x/cards. Wiederholt nur 429 ohne Tageslimit (Server hat geantwortet, nichts angelegt). Abgewiesen (ABGEWIESEN)
    -> Abgewiesen (bzw. AbgewiesenAbbruch); jeder Netzfehler nach Start des Requests (auch URLError: urllib verpackt ihn gleich, ob vor oder
    nach vollstaendigem Body), 3xx, 5xx, andere Codes und ein unlesbarer Fehler-Body -> Unklar."""
    if not eingerichtet() or not schluessel or schluessel != _schluessel():
        raise NichtEingerichtet("Reraise upload refused: no configured Reraise key in Hermes")  # defense in depth, before any network
    daten = json.dumps(karte, ensure_ascii=False).encode("utf-8")
    kopf = {"Content-Type": "application/json", "Accept": "application/json", "Origin": RERAISE, "User-Agent": "Trove/" + WERKZEUG,
            "x-reraise-key": schluessel}
    for versuch in range(1, VERSUCHE + 1):
        try:
            with OPENER.open(urllib.request.Request(RERAISE + "/api/x/cards", data=daten, method="POST", headers=kopf), timeout=30) as h:
                d = _antwort(h.read())
            if not (d.get("ok") and isinstance(d.get("card"), dict) and d["card"].get("id")):
                raise Unklar("Reraise: unexpected answer (details withheld) - the card may exist")
            return d["card"]
        except urllib.error.HTTPError as e:
            if e.code not in ABGEWIESEN:
                raise Unklar("Reraise %d nach dem Senden — Karte kann existieren" % e.code)
            try:
                d = _antwort(e.read(4000))
            except (OSError, http.client.HTTPException, ValueError):
                d = {}  # Code ist klar abgewiesen; ohne Body nur keine Einzelheiten
            if e.code == 401:
                raise AbgewiesenAbbruch(401, "Reraise does not know this key (401): enter a new key in Hermes (%s)" % zugang.RERAISE_KEY)
            if e.code == 403 and d.get("error") == "origin":
                raise AbgewiesenAbbruch(403, "Reraise rejected the origin (403 origin): %s" % RERAISE)
            if e.code == 429 and d.get("error") == "limit":
                raise AbgewiesenAbbruch(429, "Tageslimit von Reraise erreicht (30 Karten je Tag); gesendete stehen in "
                                        "reraise.json, morgen weiter")
            if e.code != 429 or versuch == VERSUCHE:
                raise Abgewiesen(e.code, str(zugang.http_fehler("Reraise", e.code, d, "error")))
        except (OSError, http.client.HTTPException) as e:  # URLError ist OSError: Body kann angekommen sein
            raise Unklar("Reraise: keine Antwort (%s) — Karte kann existieren" % type(e).__name__)
        time.sleep(2 ** versuch)


def abgleich(sha):
    """GET /api/x/cards?q=<sha256> (sucht auch in source). Karte mit dieser Pruefsumme in source -> Karte, sonst None."""
    url = RERAISE + "/api/x/cards?" + urllib.parse.urlencode({"q": sha})
    try:
        with OPENER.open(urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Trove/" + WERKZEUG}),
                         timeout=30) as h:
            d = _antwort(h.read())
    except (OSError, http.client.HTTPException) as e:
        raise zugang.DienstFehler("Abgleich mit Reraise gescheitert (%s) — nicht gesendet" % type(e).__name__)
    if not isinstance(d.get("cards"), list):
        raise zugang.DienstFehler("Abgleich mit Reraise: unerwartete Antwort — nicht gesendet")
    for c in d["cards"]:
        if isinstance(c, dict) and c.get("id") and sha in str(c.get("source") or ""):
            return {**c, "url": RERAISE + "/trade/#card-" + c["id"]}
    return None


def _notiz(l, ereignis, anker=False, **felder):
    with _prot(l.mandant) as prot:
        prot.schreiben(ereignis, werkzeug=WERKZEUG, mandant=l.mandant, lauf=l.name, **felder)
        if anker:
            prot.anker_setzen(l.mandant, l.name)


def _datei(l, p, sha, stand, probe, schluessel):
    """Eine Datei (sha256 vom Aufrufer): pruefen, Karte bauen; probe -> nur zeigen, sonst senden und buchen."""
    e = {"datei": p, "sha256": sha}
    if sha in stand["karten"]:
        k = stand["karten"][sha]
        return {**e, "zustand": "schon_da", "karte": k["id"], "url": k.get("url")}
    if sha in l.offen:
        return {**e, "zustand": "unklar", "grund": "in diesem Lauf schon gesendet, Antwort unklar — kein zweiter POST"}
    if l.nur is not None and sha not in l.nur:
        return {**e, "zustand": "nicht_bestaetigt", "grund": "nicht in der bestaetigten Liste der Probe"}
    wissen = wissen_wurzel(l.mandant)
    privat = oe.privat_irgendwo(wissen, sha, l.index)
    if privat:
        return {**e, "zustand": "verweigert_privat", "grund": "Zwilling in Sammlung %s ist nicht oeffentlich — privat bleibt privat"
                % ", ".join(privat)}
    treffer = [(md, kopf) for s, md, kopf, _ in oe.zwillinge(wissen, sha, l.index) if s == l.sammlung and kopf.get("pruefsumme") == sha]
    if not treffer:
        return {**e, "zustand": "ohne_zwilling", "grund": "erst Ins Wissen (kein Markdown-Zwilling in Sammlung %s)" % l.sammlung}
    md, kopf = treffer[0]
    karte = karte_bauen(sha, kopf)
    h = karte_hash(karte)
    if probe:
        return {**e, "zustand": "gebaut", "titel": karte["title"], "karte_probe": karte, "karte_hash": h}
    if l.nur.get(sha) not in (None, h):
        return {**e, "zustand": "nicht_bestaetigt", "grund": "Karte hat sich seit der Probe geaendert (karte_hash) — neu proben"}
    return {**e, **_senden_buchen(l, p, sha, md, karte, stand, schluessel)}


def _senden_buchen(l, p, sha, md, karte, stand, schluessel):
    """Erst abgleichen (GET), nur ohne Treffer senden, dann buchen. Gefunden -> "schon_da" (gebucht). Protokollfehler beim
    Eintrag "unklar" -> nur gezaehlt; Protokoll/Stand scheitern nach erfolgreichem Senden -> Abbruch (Stand "unklar")."""
    k, wie = abgleich(sha), "per Abgleich gefunden"
    if k is None:
        wie = "gesendet"
        l.offen.add(sha)
        stand_speichern(l.mandant, stand_setzen(stand, sha, "unterwegs"))
        try:
            k = senden(karte, schluessel)
        except Exception as ex:
            if isinstance(ex, Abgewiesen):
                l.offen.discard(sha)
                stand_speichern(l.mandant, stand_setzen(stand, sha, None))  # vom Server klar abgewiesen: nichts angelegt
                raise
            stand_speichern(l.mandant, stand_setzen(stand, sha, "unklar"))
            erg = {"zustand": "unklar", "grund": zugang.fehlertext(ex, 300)}
            try:
                _notiz(l, "fehler", quelle=p, sha256=sha, meldung="Reraise unklar: %s" % zugang.fehlertext(ex, 250), sammlung=l.sammlung)
            except Exception as fx:
                erg["protokoll_fehler"] = zugang.kurz(fx, 200)
            return erg
    try:
        _notiz(l, "versendet", quelle=p, ziel="reraise:card/%s" % k["id"], sha256=sha, bytes_=os.path.getsize(p),
               meldung="Oeffentliche Karte Nr %s auf reraise.ai (%s): %s" % (k.get("no"), wie, karte["title"]),
               karte=k["id"], nr_karte=k.get("no"), url=k.get("url"), sammlung=l.sammlung, md=md)
        stand_speichern(l.mandant, stand_setzen(stand, sha, "karten", {
            "id": k["id"], "no": k.get("no"), "url": k.get("url"), "zeit": bp.jetzt(), "datei": os.path.basename(p),
            "sammlung": l.sammlung}))
    except Exception as ex:
        try:
            stand_speichern(l.mandant, stand_setzen(stand, sha, "unklar"))
        except Exception:
            pass  # "unterwegs" steht seit vor dem POST im Stand: der naechste Lauf gleicht ab
        raise Abbruch("Karte %s ist auf Reraise, aber Protokoll/Stand scheiterten: %s" % (k["id"], zugang.fehlertext(ex, 200))) from ex
    return {"zustand": "gesendet" if wie == "gesendet" else "schon_da", "karte": k["id"], "nr": k.get("no"), "url": k.get("url"),
            "titel": karte["title"], "wie": wie}


def _dateien(pfad, max_dateien):
    if os.path.isfile(pfad):
        return [pfad]
    if not os.path.isdir(pfad):
        raise ValueError("not found: " + os.path.basename(pfad))
    dateien = [os.path.join(r, fn) for r, d, f in os.walk(pfad) for fn in sorted(f)
               if fn.lower().endswith(gm.ENDUNGEN) and not fn.startswith("~$")]
    return dateien[:max_dateien] if max_dateien else dateien


def hochladen(mandant, pfad, sammlung, max_dateien=None, probe=False, nur=None, schluessel=None):
    """probe: nur bauen und zeigen. Sonst Pflicht: nur (bestaetigte sha256) und schluessel. Abbruch steht in z["abbruch"],
    die Zusammenfassung kommt immer zurueck (Regeln im Modul-Docstring)."""
    l = Lauf(mandant=mandant, sammlung=sammlung, nur=nur)
    sperre = contextlib.nullcontext()
    if not probe:
        if nur is None:
            raise ValueError("nothing is sent without a confirmed list: run the preview first")
        if not RERAISE.startswith("https://"):
            raise ValueError("Reraise address must start with https://: %r" % RERAISE)
        if not schluessel:
            raise NichtEingerichtet("no Reraise key in Hermes")
        sperre = gm.laufsperre(mandant)
    dateien = _dateien(pfad, max_dateien)
    z = {"mandant": mandant, "sammlung": sammlung, "pfad": pfad, "probe": probe, "dateien": len(dateien), "gesendet": 0, "schon_da": 0,
         "verweigert_privat": 0, "ohne_zwilling": 0, "gebaut": 0, "unklar": 0, "nicht_bestaetigt": 0, "dubletten": 0,
         "fehler": 0, "protokoll_fehler": 0, "eintraege": []}
    erste = {}  # sha256 -> erste Fundstelle in diesem Lauf
    with sperre:
        try:
            l.index = oe.zwillings_index(wissen_wurzel(mandant))
            stand = stand_lesen(mandant)
            if not probe:
                _notiz(l, "notiz", meldung="Reraise-Hochladen Start: %s, Sammlung %s, %d Dateien" % (pfad, sammlung, len(dateien)))
            for p in dateien:
                try:
                    sha = bp.datei_sha(p)
                    if sha in erste:
                        e = {"datei": p, "sha256": sha, "zustand": "dublette", "grund": "gleicher Inhalt wie " + erste[sha]}
                    else:
                        erste[sha] = p
                        e = _datei(l, p, sha, stand, probe, schluessel)
                except Exception as ex:
                    e = {"datei": p, "zustand": "fehler", "grund": zugang.fehlertext(ex, 300)}
                    if isinstance(ex, Abbruch):
                        z["abbruch"] = zugang.fehlertext(ex, 300)
                    if not probe:
                        try:
                            _notiz(l, "fehler", quelle=p, meldung="Reraise: " + zugang.fehlertext(ex, 300))
                        except Exception as fx:
                            e["protokoll_fehler"] = zugang.kurz(fx, 200)
                z["dubletten" if e["zustand"] == "dublette" else e["zustand"]] += 1
                z["protokoll_fehler"] += "protokoll_fehler" in e
                z["eintraege"].append(e)
                if "abbruch" in z:
                    break
        except Exception as ex:
            z.setdefault("abbruch", zugang.fehlertext(ex, 300))
        if not probe:
            try:
                _notiz(l, "notiz", anker=True, meldung="Reraise-Hochladen Ende: "
                       + json.dumps({k: v for k, v in z.items() if k != "eintraege"}, ensure_ascii=False))
            except Exception:
                z["protokoll_fehler"] += 1
    return z


def sha_liste(text):
    """Bestaetigte Liste aus Datei-/Argumenttext (Komma, Leerraum): je Eintrag "sha256" oder "sha256:karte_hash" (je 64 Hex).
    Rueckgabe {sha256: karte_hash oder None}; dieselbe sha256 mit zwei verschiedenen Angaben -> ValueError."""
    out, falsch = {}, []
    for s in re.split(r"[\s,]+", (text or "").lower()):
        if not s:
            continue
        if not re.fullmatch(r"[0-9a-f]{64}(:[0-9a-f]{64})?", s):
            falsch.append(s)
            continue
        sha, _, h = s.partition(":")
        if sha in out and out[sha] != (h or None):
            falsch.append(s)
        out[sha] = h or None
    if falsch or not out:
        raise ValueError("confirmed list empty or invalid: %s" % ", ".join(f[:20] for f in falsch[:3]))
    return out


class _Netz:
    """Attrappe fuer OPENER: Antworten der Reihe nach (dict = 200 mit JSON, Exception = wird geworfen); merkt die Anfragen.
    Prueft bei jedem POST, dass die Pruefsumme schon als "unterwegs" im Stand steht. danach(i): Haken nach Anfrage i."""

    def __init__(self, *antworten, mandant="t", danach=None):
        self.antworten, self.anfragen, self.mandant, self.danach = list(antworten), [], mandant, danach

    def open(self, req, timeout=None):
        daten = json.loads(req.data) if req.data else None
        self.anfragen.append((req.get_method(), req.full_url, daten))
        if req.get_method() == "POST":
            sha = daten["source"].rsplit(" ", 1)[1]
            with open(_stand_pfad(self.mandant), encoding="utf-8") as fh:
                assert sha in json.load(fh)["unterwegs"], "POST ohne 'unterwegs' im Stand"
        if self.danach:
            self.danach(len(self.anfragen))
        a = self.antworten.pop(0)
        if isinstance(a, Exception):
            raise a
        import io
        return contextlib.closing(io.BytesIO(json.dumps(a).encode()))


def _http(code, d=None, body_reisst=False):
    import io

    class Reisst(io.BytesIO):
        def read(self, *a):
            raise ConnectionResetError(104, "reset beim Body-Lesen")
    return urllib.error.HTTPError(RERAISE, code, "test", {}, (Reisst if body_reisst else io.BytesIO)(json.dumps(d or {}).encode()))


def _selbsttest():
    """Karte nur aus Kopf-Feldern, Stand-Uebergaenge, sha-Liste; dann hochladen() ganz mit Netz-Attrappe (Temp-Ordner)."""
    global WISSEN_TEST
    WISSEN_TEST = lambda m: os.path.join(bp.WURZEL, m, "wissen")
    zugang.geheimnis_quelle = None
    try:
        senden({"source": "x"}, "k-123")
        raise AssertionError("senden() without configured key")
    except NichtEingerichtet:
        pass
    zugang.geheimnis_quelle = lambda n: {zugang.RERAISE_KEY: "k-123"}.get(n)  # Hermes mock for the rest of the self-test
    md = ('---\ntitel: "Juan Perez.pdf"\nquelle: "C:\\\\geheim\\\\r.pdf"\npruefsumme: "%s"\nbrutto: 1210.5\nwaehrung: "ARS"\n'
          'belegdatum: "2026-02-30"\niban: "DE89 3704 0044 0532 0130 00"\nverprobung: "bestanden"\ntraeger: "Person"\n'
          'aussteller_kennung: "20-12345678-9"\nrechnungsnummer: "12.345.678"\nsichtbarkeit: "oeffentlich"\nnotiz: "a\\u2028b"\n'
          '---\n\n# r.pdf\n\nOriginal: `C:\\geheim\\r.pdf`\n\n## Text\n\nDNI 12.345.678 muster@example.com Tel +54 11 4567-8901\n') % ("a" * 64)
    kopf = gm.kopf_lesen(md)
    assert kopf["brutto"] == 1210.5 and kopf["quelle"] == "C:\\geheim\\r.pdf" and kopf["notiz"] == "a\u2028b", kopf
    k = karte_bauen("a" * 64, kopf)
    alles = json.dumps(k, ensure_ascii=False)
    for s in ("geheim", "3704", "12345678", "12.345.678", "example", "4567", "## Text", "sourceDate", "Person", "Juan", "r.pdf",
              "1210"):
        assert s not in alles, (s, alles)
    assert k["language"] == "es" and not re.match(r"^([a-z][a-z0-9+.\-]*):(\S)", k["source"], re.I) and len(k["source"]) <= 500, k
    assert k["title"] == "Beleg " + "a" * 16 and k["source"] == "Trove-Beleg sha256 " + "a" * 64, k
    assert sha_liste("%s, %s\n" % ("a" * 64, "B" * 64)) == {"a" * 64: None, "b" * 64: None}
    assert sha_liste("%s:%s %s" % ("a" * 64, "c" * 64, "a" * 64 + ":" + "c" * 64)) == {"a" * 64: "c" * 64}
    for falsch in ("", "abc", "a" * 64 + ",x", "a" * 64 + ":" + "c" * 63, "%s:%s %s:%s" % ("a" * 64, "c" * 64, "a" * 64, "d" * 64)):
        try:
            sha_liste(falsch)
            raise AssertionError("Liste angenommen: %r" % falsch)
        except ValueError:
            pass
    s = {"karten": {}, "unklar": {}, "unterwegs": {}}
    for z in ("unterwegs", "unklar", "karten", None, "unklar"):
        stand_setzen(s, "x", z, {"id": "c"} if z == "karten" else None)
        assert sum("x" in s[k] for k in s) == (z is not None) and (z is None or "x" in s[z]), (z, s)
    _weiterleitung()
    _trockenlauf()


def _weiterleitung():
    """Echter OPENER gegen einen lokalen Server, der 302 liefert: Fehler (POST: Unklar), kein zweiter Request (der Schluessel
    ginge mit)."""
    import http.server
    import threading
    anfragen = []

    class H(http.server.BaseHTTPRequestHandler):
        def antwort(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            anfragen.append((self.command, self.path.split("?")[0], self.headers.get("x-reraise-key")))
            b = b'{"ok": true, "card": {"id": "x"}, "cards": [{"id": "x", "source": "%s"}]}' % (b"a" * 64)
            if self.path.startswith("/api/"):
                self.send_response(302)
                self.send_header("Location", "/woanders")
                b = b""
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        do_GET = do_POST = antwort

        def log_message(self, *a):
            pass
    s = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    g = globals()
    alt = g["RERAISE"]
    g["RERAISE"] = "http://127.0.0.1:%d" % s.server_address[1]
    try:
        for name, f in (("senden", lambda: senden({"source": "x"}, "k-123")), ("abgleich", lambda: abgleich("a" * 64))):
            try:
                f()
                raise AssertionError("%s ist der Weiterleitung gefolgt" % name)
            except RuntimeError as ex:
                assert isinstance(ex, Unklar) == (name == "senden"), ex  # 3xx nach POST: unklar (U1), nie gefolgt
        assert anfragen == [("POST", "/api/x/cards", "k-123"), ("GET", "/api/x/cards", None)], anfragen
    finally:
        g["RERAISE"] = alt
        s.shutdown()
        s.server_close()


def _trockenlauf():
    """hochladen() ganz: Probe schreibt nichts; Senden (gesendet, 500 -> unklar mit Protokollfehler, privat, ohne Zwilling);
    Protokollfehler nach Senden -> Abbruch; zweiter Lauf gleicht ab; 401 -> Abbruch, rc 2 ueber main; Zaehlung je Zustand."""
    import io
    import tempfile
    g = globals()
    alt = bp.WURZEL, OPENER, zugang.geheimnis_quelle, _prot
    with tempfile.TemporaryDirectory() as w:
        zugang.geheimnis_quelle = lambda n: {zugang.RERAISE_KEY: "k-123"}.get(n)  # Hermes mock
        bp.WURZEL = os.path.join(w, "trove")
        kaputt = []

        def prot(m):
            if kaputt:
                kaputt.pop()
                raise bp.ProtokollGesperrt("Test: Protokoll belegt")
            return alt[3](m)
        g["_prot"] = prot
        try:
            ein = os.path.join(w, "eingang")
            shas = {}
            for n in ("a_senden", "b_unklar", "c_privat", "d_ohne", "e_401", "f_buchung"):
                p = os.path.join(ein, n + ".txt")
                gm.schreibe_atomar(p, "Beleg " + n)
                shas[n] = bp.datei_sha(p)
                if n == "d_ohne":
                    continue
                for samml, sicht in (("oeff", "oeffentlich"),) + ((("priv", "privat"),) if n == "c_privat" else ()):
                    gm.schreibe_atomar(os.path.join(bp.WURZEL, "t", "wissen", samml, shas[n][:16] + "_" + n + ".md"), (
                        '---\ntitel: "%s.txt"\npruefsumme: "%s"\nsichtbarkeit: "%s"\nverprobung: "%s"\nbelegdatum: "2026-03-01"\n'
                        'netto: 100\nsteuer: 21\nbrutto: 121\nwaehrung: "ARS"\naussteller_kennung: "20-12345678-9"\n'
                        'rechnungsnummer: "0003-00012345"\n---\n\n## Text\n\nCliente Juan Perez DNI N.º 12.345.678 Alias juan.perez.mp\n')
                        % (n, shas[n], sicht, "sicher" if n == "a_senden" else "prueflist"))
            g["OPENER"] = _Netz({"cards": [{"id": "x", "source": "Trove-Beleg sha256 " + "b" * 64}, {"id": "y", "source": ""}]})
            assert abgleich("a" * 64) is None  # nur eine Karte mit dieser Pruefsumme in source zaehlt
            g["OPENER"] = netz = _Netz()
            z = hochladen("t", ein, "oeff", probe=True)
            assert (z["gebaut"], z["verweigert_privat"], z["ohne_zwilling"], z["fehler"]) == (4, 1, 1, 0), z
            assert not netz.anfragen and not os.path.exists(bp.pfad_fuer("t")) and not os.path.exists(_stand_pfad("t")), z
            gebaut = {e["sha256"]: e["karte_probe"] for e in z["eintraege"] if e["zustand"] == "gebaut"}
            assert gebaut[shas["a_senden"]]["title"] == "Beleg 2026-03-01 121.00 ARS", gebaut
            assert gebaut[shas["b_unklar"]]["title"] == "Beleg " + shas["b_unklar"][:16], gebaut  # prueflist: kein Datum (E-C3)
            assert "brutto" not in gebaut[shas["b_unklar"]]["body"] and "2026" not in gebaut[shas["b_unklar"]]["body"], gebaut
            assert all(e["karte_hash"] == karte_hash(e["karte_probe"]) for e in z["eintraege"] if e["zustand"] == "gebaut"), z
            for falsch in ({}, {"nur": {shas["a_senden"]}}):
                try:
                    hochladen("t", ein, "oeff", **falsch)
                    raise AssertionError("ohne bestaetigte Liste oder Schluessel gesendet")
                except (ValueError, NichtEingerichtet):
                    pass

            def kaputt_nach_500(i):
                if netz.antworten and isinstance(netz.antworten[0], urllib.error.HTTPError) and netz.antworten[0].code == 500:
                    kaputt.append(1)  # der Protokolleintrag zum Unklaren scheitert
            mit = {shas[n] for n in ("a_senden", "b_unklar", "c_privat", "d_ohne")}
            g["OPENER"] = netz = _Netz({"cards": []}, {"ok": True, "card": {"id": "c1", "no": 1, "url": "u1"}}, {"cards": []}, _http(500),
                                       danach=kaputt_nach_500)
            z = hochladen("t", ein, "oeff", nur=mit, schluessel="k-123")
            assert (z["gesendet"], z["unklar"], z["verweigert_privat"], z["ohne_zwilling"], z["fehler"], z["protokoll_fehler"]) \
                == (1, 1, 1, 1, 0, 1) and "abbruch" not in z, z
            assert [a[0] for a in netz.anfragen] == ["GET", "POST"] * 2 and shas["a_senden"] in netz.anfragen[0][1], netz.anfragen
            gesendet = json.dumps([a[2] for a in netz.anfragen], ensure_ascii=False)
            for x in ("Juan", "Perez", "12345678", "12.345.678", "juan.perez", "20-12345678-9", "0003-00012345", "a_senden",
                      "b_unklar", ".txt"):
                assert x not in gesendet, (x, gesendet)
            st = stand_lesen("t")
            assert st["karten"][shas["a_senden"]]["id"] == "c1" and shas["b_unklar"] in st["unklar"], st
            # Protokoll scheitert NACH erfolgreichem Senden -> Abbruch, Stand "unklar"
            g["OPENER"] = netz = _Netz({"cards": []}, {"ok": True, "card": {"id": "c3", "no": 3}},
                                       danach=lambda i: netz.anfragen[-1][0] == "POST" and kaputt.append(1))
            z = hochladen("t", ein, "oeff", nur={shas["f_buchung"]}, schluessel="k-123")
            assert "c3" in z.get("abbruch", "") and z["fehler"] == 1 and shas["f_buchung"] in stand_lesen("t")["unklar"], z
            g["OPENER"] = netz = _Netz({"cards": [{"id": "c2", "no": 2, "source": "Trove-Beleg sha256 " + shas["b_unklar"]}]},
                                       {"cards": [{"id": "c3", "no": 3, "source": "Trove-Beleg sha256 " + shas["f_buchung"]}]})
            z = hochladen("t", ein, "oeff", nur={shas["b_unklar"], shas["f_buchung"]}, schluessel="k-123")
            assert (z["gesendet"], z["schon_da"], z["nicht_bestaetigt"]) == (0, 3, 3) and [a[0] for a in netz.anfragen] == ["GET"] * 2, z
            assert [e.get("wie") for e in z["eintraege"] if e["zustand"] == "schon_da"] == [None, "per Abgleich gefunden",
                                                                                             "per Abgleich gefunden"], z
            g["OPENER"] = _Netz({"cards": []}, _http(401))
            rc, z = _main_json([ein, "--mandant", "t", "--sammlung", "oeff", "--bestaetigt", shas["e_401"], "--json"])
            assert rc == 2 and z["fehler"] == 1 and "401" in z["abbruch"], z
            st = stand_lesen("t")
            assert shas["e_401"] not in str(st), st
            r = bp.pruefen(bp.pfad_fuer("t"), bp.letzter_anker("t"))
            assert r["kette"] == "ok" and r["anker"] == "ok", r
            ev = [e["ereignis"] for e in bp.eintraege(bp.pfad_fuer("t"))]
            assert ev.count("versendet") == 3 and ev.count("fehler") == 2, ev  # 401 und c3; der unklare Eintrag scheiterte
            os.remove(_stand_pfad("t"))  # Stand verloren: die Karten kommen aus dem Protokoll ("versendet")
            st = stand_lesen("t")
            assert {sha: (k["id"], k["aus"]) for sha, k in st["karten"].items()} == {
                shas["a_senden"]: ("c1", "protokoll"), shas["b_unklar"]: ("c2", "protokoll"), shas["f_buchung"]: ("c3", "protokoll")}, st
            # URLError beim POST: genau 1 POST, kein Retry, Zustand unklar; naechster Lauf gleicht erst per GET ab
            g["OPENER"] = netz = _Netz({"cards": []}, urllib.error.URLError(ConnectionResetError(104, "reset")))
            z = hochladen("t", ein, "oeff", nur={shas["e_401"]}, schluessel="k-123")
            assert z["unklar"] == 1 and [a[0] for a in netz.anfragen] == ["GET", "POST"] and shas["e_401"] in stand_lesen("t")["unklar"], z
            g["OPENER"] = netz = _Netz({"cards": []}, {"ok": True, "card": {"id": "c5", "no": 5}})
            z = hochladen("t", ein, "oeff", nur={shas["e_401"]}, schluessel="k-123")
            assert z["gesendet"] == 1 and [a[0] for a in netz.anfragen] == ["GET", "POST"], (z, netz.anfragen)
            with gm.laufsperre("t"):  # dieselbe Sperre wie wissen.py
                try:
                    hochladen("t", ein, "oeff", nur={shas["a_senden"]}, schluessel="k-123")
                    raise AssertionError("zweiter Lauf nicht gesperrt")
                except bp.LaufLaeuft:
                    pass
                rc, z = _main_json([ein, "--mandant", "t", "--sammlung", "oeff", "--bestaetigt", shas["a_senden"], "--json"])
                assert rc == 2 and z["lauf_laeuft"] is True and "Lauf laeuft bereits" in z["abbruch"], (rc, z)  # E-C10
            _trockenlauf_u1(ein, shas)
        finally:
            bp.WURZEL = alt[0]
            g.update(OPENER=alt[1], _prot=alt[3]); zugang.geheimnis_quelle = alt[2]


def _main_json(args):
    import io
    aus, argv = io.StringIO(), sys.argv
    sys.argv = ["x", *args]
    try:
        with contextlib.redirect_stdout(aus):
            rc = main()
    finally:
        sys.argv = argv
    return rc, json.loads(aus.getvalue().strip().splitlines()[-1])


def _trockenlauf_u1(ein, shas):
    """U1/E-C9 (im Temp-Ordner von _trockenlauf): 502 mit reissendem Body -> unklar, genau ein POST; 3xx -> unklar;
    422 mit reissendem Body -> abgewiesen, Stand zurueck; Stand verloren + Karte auf Reraise -> schon_da ohne POST;
    karte_hash weicht ab -> nicht_bestaetigt ohne Netz; Dublette nach 502 (S1); unterwegs im Lauf -> unklar ohne Netz; nach Abbruch
    keine weitere Datei (M16); Abgewiesen-Klassen (H3); sha16-Kollision ohne Zwilling (M18)."""
    g = globals()
    neu = {}
    for n in ("g_502", "h_302", "i_422", "j_verloren", "k_hash"):
        p = os.path.join(ein + "-u1", n + ".txt")
        gm.schreibe_atomar(p, "Beleg " + n)
        neu[n] = sha = bp.datei_sha(p)
        gm.schreibe_atomar(os.path.join(bp.WURZEL, "t", "wissen", "oeff", sha[:16] + "_" + n + ".md"),
                           '---\npruefsumme: "%s"\nsichtbarkeit: "oeffentlich"\nverprobung: "prueflist"\n---\n' % sha)
    ein = ein + "-u1"

    def lauf(n, *antworten, **k):
        g["OPENER"] = netz = _Netz(*antworten)
        z = hochladen("t", os.path.join(ein, n + ".txt"), "oeff", nur=k.get("nur", {neu[n]}), schluessel="k-123")
        return z, [a[0] for a in netz.anfragen]
    z, wege = lauf("g_502", {"cards": []}, _http(502, body_reisst=True))
    assert z["unklar"] == 1 and wege == ["GET", "POST"] and neu["g_502"] in stand_lesen("t")["unklar"], (z, wege)
    z, wege = lauf("h_302", {"cards": []}, _http(302, body_reisst=True))
    assert z["unklar"] == 1 and wege == ["GET", "POST"] and neu["h_302"] in stand_lesen("t")["unklar"], (z, wege)
    z, wege = lauf("i_422", {"cards": []}, _http(422, body_reisst=True))
    assert z["fehler"] == 1 and "422" in z["eintraege"][0]["grund"] and wege == ["GET", "POST"], (z, wege)
    assert neu["i_422"] not in str(stand_lesen("t")), "422: Stand nicht zurueckgesetzt"
    os.remove(_stand_pfad("t"))  # Stand verloren, Karte liegt auf Reraise (Protokoll kennt sie nicht)
    z, wege = lauf("j_verloren", {"cards": [{"id": "c9", "no": 9, "source": "Trove-Beleg sha256 " + neu["j_verloren"]}]})
    assert z["schon_da"] == 1 and wege == ["GET"] and stand_lesen("t")["karten"][neu["j_verloren"]]["id"] == "c9", (z, wege)
    z, wege = lauf("k_hash", nur={neu["k_hash"]: "0" * 64})
    assert z["nicht_bestaetigt"] == 1 and "karte_hash" in z["eintraege"][0]["grund"] and wege == [], (z, wege)
    h = karte_hash(hochladen("t", ein, "oeff", probe=True)["eintraege"][-1]["karte_probe"])
    z, wege = lauf("k_hash", {"cards": []}, {"ok": True, "card": {"id": "c10", "no": 10}}, nur={neu["k_hash"]: h})
    assert z["gesendet"] == 1 and wege == ["GET", "POST"], (z, wege)
    # S1: zwei Dateien gleichen Inhalts, erster POST 502 -> genau ein POST, die zweite ist "dublette" ohne Netz
    dup = os.path.join(ein + "-dup")
    for n in ("d1", "d2"):
        gm.schreibe_atomar(os.path.join(dup, n + ".txt"), "Beleg dublette")
    sha = bp.datei_sha(os.path.join(dup, "d1.txt"))
    gm.schreibe_atomar(os.path.join(bp.WURZEL, "t", "wissen", "oeff", sha[:16] + "_d1.md"),
                       '---\npruefsumme: "%s"\nsichtbarkeit: "oeffentlich"\nverprobung: "prueflist"\n---\n' % sha)
    g["OPENER"] = netz = _Netz({"cards": []}, _http(502))
    z = hochladen("t", dup, "oeff", nur={sha}, schluessel="k-123")
    assert [a[0] for a in netz.anfragen] == ["GET", "POST"] and [e["zustand"] for e in z["eintraege"]] == ["unklar", "dublette"] \
        and z["dubletten"] == 1 and z["eintraege"][1]["grund"] == "gleicher Inhalt wie " + os.path.join(dup, "d1.txt"), (z, netz.anfragen)
    # Guertel: sha in diesem Lauf schon unterwegs gewesen -> "unklar" ohne Netz
    lo = Lauf(mandant="t", sammlung="oeff", nur={sha})
    lo.offen.add(sha)
    g["OPENER"] = netz = _Netz()
    e = _datei(lo, os.path.join(dup, "d2.txt"), sha, {"karten": {}, "unklar": {}, "unterwegs": {}}, False, "k-123")
    assert e["zustand"] == "unklar" and not netz.anfragen, e
    # M16: nach Abbruch (401) keine weitere Datei, keine weitere Anfrage
    ab = os.path.join(ein + "-abbruch")
    for n in ("a1", "a2"):
        gm.schreibe_atomar(os.path.join(ab, n + ".txt"), "Beleg abbruch " + n)
        sha_n = bp.datei_sha(os.path.join(ab, n + ".txt"))
        neu[n] = sha_n
        gm.schreibe_atomar(os.path.join(bp.WURZEL, "t", "wissen", "oeff", sha_n[:16] + "_" + n + ".md"),
                           '---\npruefsumme: "%s"\nsichtbarkeit: "oeffentlich"\nverprobung: "prueflist"\n---\n' % sha_n)
    g["OPENER"] = netz = _Netz({"cards": []}, _http(401), {"cards": []}, {"ok": True, "card": {"id": "nie"}})
    z = hochladen("t", ab, "oeff", nur={neu["a1"], neu["a2"]}, schluessel="k-123")
    assert "401" in z["abbruch"] and len(z["eintraege"]) == 1 and [a[0] for a in netz.anfragen] == ["GET", "POST"], (z, netz.anfragen)
    # H3: abgewiesen ist eine Klasse mit code; 401 ist zugleich Abbruch
    stand_speichern("t", stand_setzen(stand_lesen("t"), "9" * 64, "unterwegs"))  # die Attrappe verlangt "unterwegs" vor POST
    for code, klasse in ((422, Abgewiesen), (401, AbgewiesenAbbruch)):
        g["OPENER"] = _Netz(_http(code))
        try:
            senden({"source": "Trove-Beleg sha256 " + "9" * 64}, "k-123")
            raise AssertionError("%d angenommen" % code)
        except Abgewiesen as ex:
            assert type(ex) is klasse and ex.code == code and isinstance(ex, Abbruch) == (code == 401), ex
    stand_speichern("t", stand_setzen(stand_lesen("t"), "9" * 64, None))
    # M18: Zwilling mit gleichem sha16, aber anderer (gueltiger) pruefsumme -> kein Zwilling, keine Karte aus fremdem Kopf
    kol = os.path.join(ein + "-kollision", "k.txt")
    gm.schreibe_atomar(kol, "Beleg kollision")
    sha = bp.datei_sha(kol)
    gm.schreibe_atomar(os.path.join(bp.WURZEL, "t", "wissen", "oeff", sha[:16] + "_fremd.md"),
                       '---\npruefsumme: "%s"\nsichtbarkeit: "oeffentlich"\nverprobung: "bestanden"\nbelegdatum: "2026-03-01"\n---\n'
                       % ("0" * 64))
    z = hochladen("t", kol, "oeff", probe=True)
    assert z["ohne_zwilling"] == 1 and z["gebaut"] == 0, z


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description="Trove F16: oeffentliche Belege aus dem Wissen als Karten zu reraise.ai")
    ap.add_argument("pfad", nargs="?", help="Ordner oder Datei (Originale)")
    ap.add_argument("--mandant", default=bp.konfig.mandant())
    ap.add_argument("--sammlung", help="Sammlung des Markdown-Zwillings (wie bei Ins Wissen)")
    ap.add_argument("--max", type=int, help="hoechstens so viele Dateien")
    ap.add_argument("--probe", action="store_true", help="Karten bauen und zeigen, nichts senden")
    ap.add_argument("--nur-sha", help="Datei mit den bestaetigten sha256 (eine je Zeile oder kommagetrennt); nur diese senden")
    ap.add_argument("--bestaetigt", help="bestaetigte sha256, kommagetrennt; nur diese senden")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selbsttest", action="store_true")
    a = ap.parse_args()
    if a.selbsttest:
        _selbsttest()
        gm.selbsttest()
        oe.selbsttest()
        print("selbsttest ok")
        return 0
    if not a.pfad or not a.sammlung:
        ap.error("pfad und --sammlung angeben")
    if a.max is not None and a.max < 1:
        ap.error("--max: mindestens 1")
    try:
        nur = None
        if a.nur_sha:
            with open(a.nur_sha, encoding="utf-8-sig") as f:
                nur = sha_liste(f.read())
        if a.bestaetigt:
            nur = sha_liste(" ".join("%s:%s" % (k, v) if v else k for k, v in (nur or {}).items()) + " " + a.bestaetigt)
        z = hochladen(a.mandant, a.pfad, a.sammlung, a.max, a.probe, nur, None if a.probe else _schluessel())
    except bp.LaufLaeuft as ex:  # wie Abbruch: JSON, rc 2; plugin_api macht daraus 409
        z = {"abbruch": zugang.kurz(ex, 300), "lauf_laeuft": True}
        print(json.dumps(z, ensure_ascii=False) if a.json else "%s: %s" % (WERKZEUG, ex))
        return 2
    except (OSError, ValueError, RuntimeError, bp.ProtokollGesperrt, bp.ProtokollDefekt) as ex:
        sys.exit("%s: %s" % (WERKZEUG, ex))
    if a.json:
        print(json.dumps(z, ensure_ascii=False))
    else:
        print("%(dateien)d files: %(gesendet)d sent, %(schon_da)d already there, %(verweigert_privat)d refused (private), "
              "%(ohne_zwilling)d without twin, %(gebaut)d built (preview), %(unklar)d unclear, %(nicht_bestaetigt)d not confirmed, "
              "%(dubletten)d duplicates, %(fehler)d errors, %(protokoll_fehler)d log errors" % z + ("; ABORTED: " + z["abbruch"] if "abbruch" in z else ""))
    return 2 if "abbruch" in z else 0


if __name__ == "__main__":
    sys.exit(main())
