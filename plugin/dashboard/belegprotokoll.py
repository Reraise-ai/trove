"""Trove F8 — Belegprotokoll: jede Bewegung eines Belegs, nur anfuegend, rueckwaerts verfolgbar.

Eine JSONL-Datei je Mandant: <datenwurzel>\\<mandant>\\belegprotokoll.jsonl
Jede Zeile: ein Ereignis. Felder:
  zeit       ISO-Zeit mit Zone (Schreibzeitpunkt)
  ereignis   gesehen | kopiert | verifiziert | verschoben | umbenannt | fehler | notiz
  quelle     Pfad, von dem die Bewegung ausgeht (bei "gesehen": der Fundort)
  ziel       Pfad, an dem die Datei danach liegt (leer bei gesehen/fehler)
  sha256     Pruefsumme des Inhalts (leer, wenn nicht gerechnet)
  bytes      Groesse
  werkzeug   wer geschrieben hat (kopieren.py, register.py, ...)
  mandant    Mandantenname
  lauf       Kennung des Laufs (z. B. rettung-2026-01)
  meldung    Freitext (Fehler, Erklaerung)
  nr         laufende Nummer in dieser Datei (1..n)
  kette      sha256(kette der Vorzeile + diese Zeile ohne kette) — Luecken/Aenderungen in der Mitte sind nachweisbar
Nie ueberschreiben, nie loeschen. Nur anfuegen.

Grenzen (Review 18.10.2026): Die Kette allein erkennt kein Abschneiden am Ende und schuetzt nicht gegen
Neurechnen aller Folgehashes. Dafuer gibt es den ANKER: nach jedem Lauf wird (nr, kette) in eine zweite
Datei ausserhalb des Mandantenordners geschrieben (<datenwurzel>\\_anker\\<mandant>.jsonl);
`pruefen` vergleicht gegen den letzten Anker. Der Anker gehoert zusaetzlich auf ein zweites Medium.

Schreibschutz: ein Protokoll-Objekt haelt waehrend seiner Lebensdauer einen Exklusiv-Lock (<pfad>.lock, Klasse Sperre —
dieselbe Klasse dient anderen Werkzeugen als Laufsperre, art="lauf" -> LaufLaeuft); ein zweiter Schreiber scheitert sofort,
statt die Kette zu gabeln. Datenwurzel: Umgebungsvariable TROVE_DATEN (Standard <Benutzerverzeichnis>/Trove, oder trove.json), fuer alle Werkzeuge.
"""
import argparse, hashlib, json, os, sys, time, unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import konfig  # noqa: E402  (setzt TROVE_* aus trove.json vor)

WURZEL = konfig.datenwurzel()
EREIGNISSE = ("gesehen", "kopiert", "verifiziert", "verschoben", "umbenannt", "fehler", "notiz", "gelesen", "verprobt", "zerlegt", "versendet", "unterschrieben")
ENC = dict(encoding="utf-8", errors="surrogatepass")


class ProtokollDefekt(Exception):
    """Die Protokolldatei endet nicht mit einer vollstaendigen Zeile (Absturz mitten im Schreiben)."""


class ProtokollGesperrt(Exception):
    """Ein anderer Prozess schreibt gerade in dieses Protokoll."""


class LaufLaeuft(ProtokollGesperrt):
    """Laufsperre belegt: ein anderer Lauf desselben Werkzeugs laeuft (Unterklasse, damit Aufrufer weiter funktionieren)."""


def pfad_fuer(mandant):
    return os.path.join(WURZEL, mandant, "belegprotokoll.jsonl")


def anker_pfad_fuer(mandant):
    return os.path.join(WURZEL, "_anker", mandant + ".jsonl")


def jetzt():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


_jetzt = jetzt  # alter Name (docuseal.py u. a.)


def _norm(p):
    r"""Pfad-Normalform fuer Vergleiche: \\?\-Praefix weg, Slashes vereinheitlicht und gefaltet, NFC,
    Gross/Klein nur bei Windows-Pfaden (Laufwerk oder UNC) ignoriert — POSIX-Pfade bleiben case-sensitiv."""
    p = unicodedata.normalize("NFC", p or "")
    if p.startswith("\\\\?\\UNC\\"):
        p = "\\\\" + p[8:]
    elif p.startswith("\\\\?\\"):
        p = p[4:]
    p = p.replace("\\", "/")
    while "//" in p[1:]:
        p = p[0] + p[1:].replace("//", "/")
    if len(p) > 1:
        p = p.rstrip("/") or p
    windows = (len(p) >= 2 and p[1] == ":") or p.startswith("//")
    return p.casefold() if windows else p


def _kette(vor_kette, eintrag_ohne_kette):
    h = hashlib.sha256()
    h.update((vor_kette or "").encode("utf-8", "surrogatepass"))
    h.update(json.dumps(eintrag_ohne_kette, ensure_ascii=False, sort_keys=True).encode("utf-8", "surrogatepass"))
    return h.hexdigest()


def _letzte_zeile(pfad):
    """Letzte vollstaendige Zeile. Liefert (eintrag, endet_mit_newline). Wirft ProtokollDefekt, wenn die
    letzte Zeile nicht lesbar ist (abgeschnitten, NUL-Fuellung) — dann bewusst entscheiden, nie still reparieren."""
    if not os.path.exists(pfad):
        return None, True
    with open(pfad, "rb") as f:
        f.seek(0, os.SEEK_END)
        ende = f.tell()
        if ende == 0:
            return None, True
        block = 65536
        while True:
            lese = min(ende, block)
            f.seek(ende - lese)
            tail = f.read(lese)
            # NUL-Fuellung (NTFS nach Stromausfall) am Ende ignorieren, sonst strikt
            tail_s = tail.rstrip(b"\x00")
            endet_nl = tail_s.endswith(b"\n")
            kern = tail_s.rstrip(b"\r\n")
            pos = kern.rfind(b"\n")
            if pos >= 0 or lese == ende:
                letzte = kern[pos + 1:] if pos >= 0 else kern
                break
            block *= 2
    try:
        return json.loads(letzte.decode("utf-8", "surrogatepass")), endet_nl
    except (ValueError, UnicodeDecodeError) as ex:
        raise ProtokollDefekt("last line of %s is not readable (%s): %r" % (pfad, ex, letzte[:120]))


def _pid_aus(pfad):
    try:
        with open(pfad) as fh:
            return int(fh.read().strip() or "0")
    except (OSError, ValueError):
        return 0


class Sperre:
    """Exklusiv-Sperre auf einer Datei, Handle OFFEN bis entsperren (Kontextmanager); die PID steht darin (fuer die Meldung).
    Windows: O_CREAT|O_EXCL. Die Wahrheit ist das offene Handle, nicht die PID: ohne FILE_SHARE_DELETE verweigert das System
    jedem anderen das Loeschen, solange der Halter lebt (os.remove -> PermissionError = belegt). Gelingt das Loeschen, war die
    Sperre verwaist (auch wenn die PID inzwischen einem fremden Prozess gehoert) und wird neu angelegt. entsperren schliesst das
    Handle und loescht die Datei (bis 5 Versuche a 0,2 s, z. B. Virenscanner); scheitert das, bleibt sie verwaist liegen und
    der naechste Halter uebernimmt sie per Loeschversuch.
    POSIX: flock auf einer bleibenden Datei (nie geloescht: Loeschen liesse einem zweiten Oeffner ein verwaistes Inode, zwei
    Halter waeren moeglich); der Kern gibt flock beim Tod des Halters frei. Die Datei bleibt nach entsperren liegen.
    art "protokoll": belegt -> ProtokollGesperrt; art "lauf": belegt -> LaufLaeuft (Unterklasse)."""

    def __init__(self, pfad, art="protokoll"):
        if art not in ("protokoll", "lauf"):
            raise ValueError("art: protokoll | lauf")
        self.pfad = pfad
        self.art = art
        self._fd = None

    def __enter__(self):
        self.sperren()
        return self

    def __exit__(self, *a):
        self.entsperren()

    def _belegt(self, pid):
        if self.art == "lauf":
            return LaufLaeuft("Lauf laeuft bereits (Prozess %s): %s" % (pid or "?", self.pfad))
        return ProtokollGesperrt("%s is locked (process %s)" % (self.pfad, pid or "?"))

    def sperren(self):
        d = os.path.dirname(self.pfad)
        if d:
            os.makedirs(d, exist_ok=True)
        if os.name != "nt":
            import fcntl
            fd = os.open(self.pfad, os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise self._belegt(_pid_aus(self.pfad))
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
            self._fd = fd
            return
        for _ in range(3):
            try:
                fd = os.open(self.pfad, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except PermissionError:  # "delete pending" (Scanner mit FILE_SHARE_DELETE, Netzlaufwerk): kurz warten
                time.sleep(0.2)
                continue
            except FileExistsError:
                try:
                    os.remove(self.pfad)  # gelingt nur, wenn kein Halter die Datei offen haelt
                except PermissionError:
                    raise self._belegt(_pid_aus(self.pfad))
                except FileNotFoundError:
                    pass
                continue
            os.write(fd, str(os.getpid()).encode())
            self._fd = fd
            return
        raise self._belegt(None)

    def entsperren(self):
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        if os.name == "nt":
            for i in range(5):
                try:
                    os.remove(self.pfad)
                    return
                except FileNotFoundError:
                    return
                except OSError:
                    if i < 4:
                        time.sleep(0.2)


class Protokoll:
    """Offenes Protokoll mit Exklusiv-Sperre (<pfad>.lock, Klasse Sperre); nr/kette erst nach erfolgreichem Schreiben uebernommen."""

    def __init__(self, pfad):
        self.pfad = pfad
        d = os.path.dirname(pfad)
        if d:
            os.makedirs(d, exist_ok=True)
        self._sperre = Sperre(pfad + ".lock")
        self._sperre.sperren()
        self._f = None
        try:  # jeder Fehler hier (ProtokollDefekt, KeyError, OSError): Sperre frei, sonst haengt sie im Hermes-Prozess
            letzte, endet_nl = _letzte_zeile(pfad)
            self.nr = letzte["nr"] if letzte else 0
            self.kette = letzte["kette"] if letzte else ""
            self._f = open(pfad, "a", buffering=1, newline="\n", **ENC)
            if not endet_nl:
                self._f.write("\n")  # eine vollstaendige Zeile ohne Zeilenende: sauber abschliessen
        except BaseException:
            if self._f is not None:
                try:
                    self._f.close()
                except OSError:
                    pass
            self._sperre.entsperren()
            raise

    def schreiben(self, ereignis, quelle="", ziel="", sha256="", bytes_=None, werkzeug="", mandant="", lauf="", meldung="", zeit=None, **extra):
        if ereignis not in EREIGNISSE:
            raise ValueError("unbekanntes Ereignis: %r" % ereignis)
        nr = self.nr + 1
        e = {"nr": nr, "zeit": zeit or jetzt(), "ereignis": ereignis, "quelle": quelle, "ziel": ziel,
             "sha256": sha256 or "", "bytes": bytes_, "werkzeug": werkzeug, "mandant": mandant, "lauf": lauf,
             "meldung": meldung}
        e.update(extra)
        kette = _kette(self.kette, e)
        e["kette"] = kette
        zeile = json.dumps(e, ensure_ascii=False) + "\n"
        self._f.write(zeile)
        self._f.flush()
        self.nr, self.kette = nr, kette
        return e

    def anker_setzen(self, mandant, lauf=""):
        """(nr, kette) ausserhalb des Mandantenordners festhalten — Grundlage fuer pruefen gegen Abschneiden."""
        if self.nr == 0:
            return None
        pfad = anker_pfad_fuer(mandant)
        os.makedirs(os.path.dirname(pfad), exist_ok=True)
        a = {"zeit": jetzt(), "mandant": mandant, "lauf": lauf, "nr": self.nr, "kette": self.kette, "protokoll": self.pfad}
        with open(pfad, "a", newline="\n", **ENC) as f:
            f.write(json.dumps(a, ensure_ascii=False) + "\n"); f.flush(); os.fsync(f.fileno())
        return a

    def close(self):
        try:
            self._f.flush(); os.fsync(self._f.fileno()); self._f.close()
        finally:
            self._sperre.entsperren()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def schreiben(pfad, **felder):
    with Protokoll(pfad) as p:
        return p.schreiben(**felder)


def lesen(pfad):
    """Liefert (zeilennummer, eintrag_oder_None, fehlertext_oder_None)."""
    with open(pfad, newline="\n", **ENC) as f:
        n = 0
        for z in f:
            if not z.strip("\r\n\x00 "):
                continue
            n += 1
            try:
                yield n, json.loads(z), None
            except ValueError as ex:
                yield n, None, str(ex)


def eintraege(pfad):
    for _, e, fehler in lesen(pfad):
        if e is not None:
            yield e


def letzter_anker(mandant):
    pfad = anker_pfad_fuer(mandant)
    if not os.path.exists(pfad):
        return None
    letzte = None
    for _, a, _ in lesen(pfad):
        if a:
            letzte = a
    return letzte


def pruefen(pfad, anker=None):
    """Kette durchrechnen. Liefert dict mit zeilen, kette (ok / ok mit vermerkten Bruechen / BRUCH), bruch_bei, anker.
    Ein Bruch (nr springt oder kette passt nicht) gilt als vermerkt, wenn eine spaetere, selbst kettengueltige notiz-Zeile
    mit vermerk="kettenbruch" seine Zeilennummer in bruch_bei auffuehrt (Datei bleibt append-only; der Defekt steht drin)."""
    vor = ""
    vor_nr = 0
    n = 0
    brueche = []
    vermerkt = set()
    for zeilennr, e, fehler in lesen(pfad):
        n += 1
        if e is None:
            return {"zeilen": n, "kette": "BRUCH", "bruch_bei": {"zeile": zeilennr, "fehler": fehler}, "anker": "not checked", "brueche": brueche}
        k = e.get("kette")
        ohne = {a: b for a, b in e.items() if a != "kette"}
        if e.get("nr") != vor_nr + 1 or _kette(vor, ohne) != k:
            brueche.append({"zeile": n, "nr": e.get("nr"), "zeit": e.get("zeit"), "ereignis": e.get("ereignis"), "werkzeug": e.get("werkzeug")})
        elif e.get("ereignis") == "notiz" and e.get("vermerk") == "kettenbruch":
            vermerkt.update(int(z) for z in (e.get("bruch_bei") or []))
        vor, vor_nr = k, e.get("nr") or vor_nr
    offen = [b for b in brueche if b["zeile"] not in vermerkt]
    if offen:
        return {"zeilen": n, "kette": "BRUCH", "bruch_bei": offen[0], "brueche": brueche, "offen": len(offen), "anker": "not checked"}
    ergebnis = {"zeilen": n, "kette": "ok" if not brueche else "ok (%d Brueche vermerkt)" % len(brueche), "bruch_bei": None, "brueche": brueche, "anker": "kein"}
    if anker:
        # Anker = (nr, kette) einer Zeile: sie muss im Protokoll stehen (Position egal — nach einem vermerkten Bruch ist nr != Zeile moeglich)
        treffer = any(e.get("nr") == anker["nr"] and e.get("kette") == anker["kette"] for e in eintraege(pfad))
        ergebnis["anker"] = "ok" if treffer else "ABWEICHUNG: Zeile (nr %d, kette %s...) des Ankers nicht im Protokoll — Ende abgeschnitten oder veraendert?" % (anker["nr"], anker["kette"][:12])
        ergebnis["anker_nr"] = anker["nr"]
    return ergebnis


def spur(pfad, datei, max_tiefe=50):
    """Rueckwaerts: alle Ereignisse zu einer Datei, dann die Quelle davon, deren Quelle ... bis zum Ursprung.
    Vorwaerts: wohin die Datei danach noch gegangen ist. Treffer auch ueber sha256."""
    alle = list(eintraege(pfad))
    nach_ziel, nach_quelle, nach_sha = {}, {}, {}
    for e in alle:
        if e.get("ziel"):
            nach_ziel.setdefault(_norm(e["ziel"]), []).append(e)
        if e.get("quelle"):
            nach_quelle.setdefault(_norm(e["quelle"]), []).append(e)
        if e.get("sha256"):
            nach_sha.setdefault(e["sha256"], []).append(e)
    gesucht = _norm(datei)
    treffer = sorted({id(e): e for e in nach_ziel.get(gesucht, []) + nach_quelle.get(gesucht, [])}.values(), key=lambda e: e["nr"])
    shas = {e["sha256"] for e in treffer if e.get("sha256")}
    if not shas and os.path.exists(datei):
        h = hashlib.sha256()
        with open(datei, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        shas = {h.hexdigest()}
    gleicher_inhalt = sorted({id(e): e for s in shas for e in nach_sha.get(s, [])}.values(), key=lambda e: e["nr"])

    def lauf(start, index, feld):
        kette, offen, gesehen, abgeschnitten = [], [start], set(), False
        tiefe = 0
        while offen:
            if tiefe >= max_tiefe:
                abgeschnitten = True; break
            tiefe += 1
            naechste = []
            for knoten in offen:
                if knoten in gesehen:
                    continue
                gesehen.add(knoten)
                for e in index.get(knoten, []):
                    if e.get(feld):
                        kette.append(e); naechste.append(_norm(e[feld]))
            offen = naechste
        return sorted({id(e): e for e in kette}.values(), key=lambda e: e["nr"]), abgeschnitten

    rueck, r_ab = lauf(gesucht, nach_ziel, "quelle")
    vor, v_ab = lauf(gesucht, nach_quelle, "ziel")
    return {"datei": datei, "direkt": treffer, "rueckwaerts": rueck, "vorwaerts": vor, "gleicher_inhalt": gleicher_inhalt,
            "sha256": sorted(shas), "abgeschnitten": r_ab or v_ab}


def datei_sha(pfad):
    h = hashlib.sha256()
    with open(pfad, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


_datei_sha = datei_sha  # alter Name


def einlesen_kopierprotokoll(alt, pfad, mandant, lauf, werkzeug="kopieren.py"):
    """Altes kopieren.py-Protokoll (status ok/schon_da/geaendert/nicht_im_index/fehler/mismatch/
    quelle_verschwunden/symlink) als F8-Ereignisse uebernehmen. Original bleibt unberuehrt.
    Idempotent: dieselbe Altdatei (sha256) wird nicht zweimal uebernommen. Altzeiten sind zonenlos und
    werden in zeit_alt belassen (zeit_zone = 'angenommen lokal'); zeit = Einlesezeitpunkt."""
    abb = {"ok": "kopiert", "geaendert": "kopiert", "nicht_im_index": "kopiert", "schon_da": "gesehen",
           "mismatch": "fehler", "fehler": "fehler", "quelle_verschwunden": "notiz", "symlink": "notiz"}
    alt_sha = datei_sha(alt)
    if os.path.exists(pfad):
        for e in eintraege(pfad):
            if e.get("ereignis") == "notiz" and e.get("eingelesen_sha") == alt_sha:
                return 0
    n = 0
    with Protokoll(pfad) as p:
        p.schreiben("notiz", werkzeug="belegprotokoll.py", mandant=mandant, lauf=lauf,
                    meldung="einlesen beginnt: " + alt, eingelesen_sha=alt_sha, eingelesen_von=alt)
        for _, a, fehler in lesen(alt):
            if a is None:
                p.schreiben("fehler", werkzeug="belegprotokoll.py", mandant=mandant, lauf=lauf, meldung="Altzeile unlesbar: " + str(fehler))
                n += 1
                continue
            st = a.get("status", "")
            ev = abb.get(st, "notiz")
            meldung = a.get("meldung", "") or ""
            if st in ("geaendert", "nicht_im_index", "quelle_verschwunden", "symlink", "mismatch"):
                meldung = (st + ("; " + meldung if meldung else ""))
            p.schreiben(ev, quelle=a.get("quelle", ""), ziel=a.get("ziel", ""),
                        sha256=a.get("sha_kopie") or a.get("sha_index") or "", bytes_=a.get("bytes"),
                        werkzeug=werkzeug, mandant=mandant, lauf=lauf, meldung=meldung,
                        zeit_alt=a.get("zeit"), zeit_zone="angenommen lokal", status_alt=st, sha_index=a.get("sha_index"))
            n += 1
        p.schreiben("notiz", werkzeug="belegprotokoll.py", mandant=mandant, lauf=lauf,
                    meldung="einlesen beendet: %d Zeilen uebernommen" % n, eingelesen_sha=alt_sha)
        p.anker_setzen(mandant, lauf)
    return n


def _selbsttest():
    """Sperre mit zwei echten Prozessen: zweiter Schreiber gesperrt, toter Halter (kill -9 / verwaiste Datei) aufgeraeumt."""
    import signal, subprocess, tempfile
    with tempfile.TemporaryDirectory() as w:
        pfad = os.path.join(w, "m", "belegprotokoll.jsonl")
        halter = ("import sys; sys.path.insert(0, %r); import belegprotokoll as bp; p = bp.Protokoll(%r); "
                  "p.schreiben('notiz', meldung='halter'); print('da', flush=True); sys.stdin.read()"
                  % (os.path.dirname(os.path.abspath(__file__)), pfad))
        k = subprocess.Popen([sys.executable, "-c", halter], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert k.stdout.readline().strip() == "da"
            try:
                Protokoll(pfad)
                raise AssertionError("zweiter Schreiber nicht gesperrt")
            except ProtokollGesperrt as ex:
                assert str(k.pid) in str(ex), ex
            if os.name == "nt":
                k.kill()
            else:
                os.kill(k.pid, signal.SIGKILL)
            k.wait(10)
        finally:
            if k.poll() is None:
                k.kill()
        with Protokoll(pfad) as p:  # toter Halter: Sperre frei
            p.schreiben("notiz", meldung="nach dem Tod")
        with open(pfad + ".lock", "w") as fh:
            fh.write(str(k.pid))  # verwaiste Sperrdatei mit toter PID
        schreiben(pfad, ereignis="notiz", meldung="ueber verwaister Sperre")
        assert pruefen(pfad)["kette"] == "ok" and [e["nr"] for e in eintraege(pfad)] == [1, 2, 3]
        with Sperre(os.path.join(w, "lauf.lock"), art="lauf") as s:
            try:
                Sperre(s.pfad, art="lauf").sperren()
                raise AssertionError("Laufsperre doppelt vergeben")
            except LaufLaeuft as ex:
                assert isinstance(ex, ProtokollGesperrt) and "Lauf laeuft bereits" in str(ex), ex
        with Sperre(s.pfad):
            pass
        assert os.name == "nt" or os.path.exists(s.pfad)  # POSIX: Sperrdatei bleibt (siehe Sperre)
        # Fehler in __init__ nach sperren() (KeyError: letzte Zeile ohne nr) -> Sperre wieder frei, im selben Prozess
        kaputt = os.path.join(w, "k", "belegprotokoll.jsonl")
        os.makedirs(os.path.dirname(kaputt))
        with open(kaputt, "w", encoding="utf-8") as fh:
            fh.write('{"ohne": "nr"}\n')
        for _ in range(2):
            try:
                Protokoll(kaputt)
                raise AssertionError("Zeile ohne nr angenommen")
            except KeyError:
                pass  # zweiter Durchlauf: wieder KeyError, nicht ProtokollGesperrt
        with Sperre(kaputt + ".lock"):
            pass
        _selbsttest_windows(w)


def _selbsttest_windows(w):
    """Windows-Zweig mit Attrappen von os.name und os.remove (laeuft auch unter Linux): offenes Handle -> PermissionError."""
    alt = os.name, os.remove
    pfad = os.path.join(w, "win.lock")
    offen, blockiert, versuche = set(), [0], []

    def remove(p):
        versuche.append(p)
        if p in offen:
            raise PermissionError(32, "offenes Handle")  # WinError 32: Halter lebt (ohne FILE_SHARE_DELETE)
        if blockiert[0]:
            blockiert[0] -= 1
            raise PermissionError(32, "Virenscanner")
        alt[1](p)
    try:
        os.name = "nt"
        os.remove = remove
        with open(pfad, "w") as fh:
            fh.write(str(os.getppid()))  # PID lebt, gehoert aber einem fremden Prozess; kein Handle offen
        with Sperre(pfad) as s:  # PID wiederverwendet: Uebernahme
            assert _pid_aus(pfad) == os.getpid() and versuche == [pfad], versuche
            offen.add(pfad)  # Halter lebt: remove -> PermissionError -> belegt, Datei bleibt
            try:
                Sperre(pfad, art="lauf").sperren()
                raise AssertionError("lebender Halter uebergangen")
            except LaufLaeuft as ex:
                assert str(os.getpid()) in str(ex), ex
            assert _pid_aus(pfad) == os.getpid()
            offen.clear()
            blockiert[0] = 2  # entsperren mit kurz blockiertem remove: nach Wiederholung geloescht
            del versuche[:]
        assert not os.path.exists(pfad) and versuche == [pfad] * 3 and s._fd is None, versuche
        with Sperre(pfad):
            blockiert[0] = 9  # dauerhaft blockiert: aufgeben ohne Fehler, Datei bleibt verwaist
        assert os.path.exists(pfad) and blockiert[0] == 4
        blockiert[0] = 0
        with Sperre(pfad):  # der naechste Halter uebernimmt sie per Loeschversuch
            assert _pid_aus(pfad) == os.getpid()
        assert not os.path.exists(pfad)
        _selbsttest_ausstehend(pfad)
    finally:
        os.name, os.remove = alt


def _selbsttest_ausstehend(pfad):
    """O_EXCL wirft PermissionError (Datei "delete pending"): warten und erneut; nach 3 Versuchen belegt."""
    alt = os.open, time.sleep
    rest, oeffnungen, pausen = [0], [], []

    def oeffnen(p, flags, *a):
        if p == pfad:
            oeffnungen.append(p)
            if rest[0]:
                rest[0] -= 1
                raise PermissionError(13, "delete pending")
        return alt[0](p, flags, *a)
    try:
        os.open, time.sleep = oeffnen, pausen.append
        rest[0] = 2
        with Sperre(pfad):
            assert len(oeffnungen) == 3 and pausen == [0.2, 0.2], (oeffnungen, pausen)
        del oeffnungen[:], pausen[:]
        rest[0] = 9
        try:
            Sperre(pfad, art="lauf").sperren()
            raise AssertionError("pending delete nicht als belegt gemeldet")
        except LaufLaeuft:
            pass
        assert len(oeffnungen) == 3 and pausen == [0.2] * 3 and not os.path.exists(pfad), (oeffnungen, pausen)
    finally:
        os.open, time.sleep = alt


def main():
    if sys.argv[1:] == ["--selbsttest"]:
        _selbsttest()
        print("selbsttest ok")
        return
    ap = argparse.ArgumentParser(description="Trove F8 Belegprotokoll (append-only, rueckwaerts verfolgbar)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("schreiben"); s.add_argument("--mandant", required=True); s.add_argument("--ereignis", required=True, choices=EREIGNISSE)
    s.add_argument("--quelle", default=""); s.add_argument("--ziel", default=""); s.add_argument("--sha256", default="")
    s.add_argument("--bytes", type=int); s.add_argument("--werkzeug", default="cli"); s.add_argument("--lauf", default=""); s.add_argument("--meldung", default="")
    s = sub.add_parser("spur"); s.add_argument("--mandant", required=True); s.add_argument("--datei", required=True); s.add_argument("--json", action="store_true")
    s = sub.add_parser("pruefen"); s.add_argument("--mandant", required=True)
    s = sub.add_parser("anker"); s.add_argument("--mandant", required=True); s.add_argument("--lauf", default="")
    s = sub.add_parser("einlesen"); s.add_argument("--mandant", required=True); s.add_argument("--alt", required=True); s.add_argument("--lauf", required=True); s.add_argument("--werkzeug", default="kopieren.py")
    a = ap.parse_args()
    pfad = pfad_fuer(a.mandant)
    if a.cmd == "schreiben":
        e = schreiben(pfad, ereignis=a.ereignis, quelle=a.quelle, ziel=a.ziel, sha256=a.sha256, bytes_=a.bytes, werkzeug=a.werkzeug, mandant=a.mandant, lauf=a.lauf, meldung=a.meldung)
        print(json.dumps(e, ensure_ascii=False))
    elif a.cmd == "pruefen":
        r = pruefen(pfad, letzter_anker(a.mandant)); r["protokoll"] = pfad
        print(json.dumps(r, ensure_ascii=False))
        sys.exit(0 if r["kette"] == "ok" and not str(r["anker"]).startswith("ABWEICHUNG") else 2)
    elif a.cmd == "anker":
        with Protokoll(pfad) as p:
            print(json.dumps(p.anker_setzen(a.mandant, a.lauf), ensure_ascii=False))
    elif a.cmd == "einlesen":
        n = einlesen_kopierprotokoll(a.alt, pfad, a.mandant, a.lauf, a.werkzeug)
        print(json.dumps({"protokoll": pfad, "uebernommen": n}, ensure_ascii=False))
    elif a.cmd == "spur":
        r = spur(pfad, a.datei)
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=1))
        else:
            print("File:", r["datei"]); print("sha256:", ", ".join(r["sha256"]) or "-")
            for titel, liste in (("Direct", r["direkt"]), ("Backwards (from where)", r["rueckwaerts"]), ("Forwards (to where)", r["vorwaerts"])):
                print("\n%s: %d event(s)" % (titel, len(liste)))
                for e in liste:
                    print("  #%s %s %-10s %s -> %s %s" % (e["nr"], e["zeit"], e["ereignis"], e.get("quelle", ""), e.get("ziel", ""), ("[" + e["meldung"] + "]") if e.get("meldung") else ""))
            print("\nGleicher Inhalt (sha256) an %d Stelle(n)" % len(r["gleicher_inhalt"]))
            for e in r["gleicher_inhalt"][:20]:
                print("  #%s %s %s" % (e["nr"], e["ereignis"], e.get("ziel") or e.get("quelle")))
            if r["abgeschnitten"]:
                print("\nHINWEIS: Suche nach %d Stufen abgeschnitten." % 50)


if __name__ == "__main__":
    main()
