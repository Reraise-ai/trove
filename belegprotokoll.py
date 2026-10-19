"""Trove F8 — Belegprotokoll: jede Bewegung eines Belegs, nur anfuegend, rueckwaerts verfolgbar.

Eine JSONL-Datei je Mandant: <TROVE_DATEN>\\<mandant>\\belegprotokoll.jsonl
Jede Zeile: ein Ereignis. Felder:
  zeit       ISO-Zeit mit Zone (Schreibzeitpunkt)
  ereignis   gesehen | kopiert | verifiziert | verschoben | umbenannt | fehler | notiz
  quelle     Pfad, von dem die Bewegung ausgeht (bei "gesehen": der Fundort)
  ziel       Pfad, an dem die Datei danach liegt (leer bei gesehen/fehler)
  sha256     Pruefsumme des Inhalts (leer, wenn nicht gerechnet)
  bytes      Groesse
  werkzeug   wer geschrieben hat (kopieren.py, register.py, ...)
  mandant    Mandantenname
  lauf       Kennung des Laufs (z. B. rettung-2026-10-17)
  meldung    Freitext (Fehler, Erklaerung)
  nr         laufende Nummer in dieser Datei (1..n)
  kette      sha256(kette der Vorzeile + diese Zeile ohne kette) — Luecken/Aenderungen in der Mitte sind nachweisbar
Nie ueberschreiben, nie loeschen. Nur anfuegen.

Grenzen (Review 18.10.2026): Die Kette allein erkennt kein Abschneiden am Ende und schuetzt nicht gegen
Neurechnen aller Folgehashes. Dafuer gibt es den ANKER: nach jedem Lauf wird (nr, kette) in eine zweite
Datei ausserhalb des Mandantenordners geschrieben (<TROVE_DATEN>\\_anker\\<mandant>.jsonl);
`pruefen` vergleicht gegen den letzten Anker. Der Anker gehoert zusaetzlich auf ein zweites Medium.

Schreibschutz: ein Protokoll-Objekt haelt waehrend seiner Lebensdauer einen Exklusiv-Lock (<pfad>.lock);
ein zweiter Schreiber scheitert sofort, statt die Kette zu gabeln.
"""
import argparse, ctypes, hashlib, json, os, sys, time, unicodedata

WURZEL = os.environ.get("TROVE_DATEN") or os.path.join(os.path.expanduser("~"), "trove-daten")  # Datenordner; per Umgebungsvariable TROVE_DATEN aenderbar
EREIGNISSE = ("gesehen", "kopiert", "verifiziert", "verschoben", "umbenannt", "fehler", "notiz", "gelesen", "verprobt", "zerlegt", "versendet", "unterschrieben")
ENC = dict(encoding="utf-8", errors="surrogatepass")


class ProtokollDefekt(Exception):
    """Die Protokolldatei endet nicht mit einer vollstaendigen Zeile (Absturz mitten im Schreiben)."""


class ProtokollGesperrt(Exception):
    """Ein anderer Prozess schreibt gerade in dieses Protokoll."""


def pfad_fuer(mandant):
    return os.path.join(WURZEL, mandant, "belegprotokoll.jsonl")


def anker_pfad_fuer(mandant):
    return os.path.join(WURZEL, "_anker", mandant + ".jsonl")


def _jetzt():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


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
        raise ProtokollDefekt("letzte Zeile von %s nicht lesbar (%s): %r" % (pfad, ex, letzte[:120]))


def _pid_lebt(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0); return True
        except OSError:
            return False
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k.GetExitCodeProcess(h, ctypes.byref(code))
    k.CloseHandle(h)
    return bool(ok) and code.value == 259  # STILL_ACTIVE


class Protokoll:
    """Offenes Protokoll mit Exklusiv-Lock; nr/kette werden erst nach erfolgreichem Schreiben uebernommen."""

    def __init__(self, pfad):
        self.pfad = pfad
        d = os.path.dirname(pfad)
        if d:
            os.makedirs(d, exist_ok=True)
        self._lock = pfad + ".lock"
        self._sperren()
        try:
            letzte, endet_nl = _letzte_zeile(pfad)
        except ProtokollDefekt:
            self._entsperren(); raise
        self.nr = letzte["nr"] if letzte else 0
        self.kette = letzte["kette"] if letzte else ""
        self._f = open(pfad, "a", buffering=1, newline="\n", **ENC)
        if not endet_nl:
            self._f.write("\n")  # eine vollstaendige Zeile ohne Zeilenende: sauber abschliessen

    def _sperren(self):
        """Exklusiv-Lock: die Lock-Datei bleibt waehrend der Lebensdauer OFFEN. Windows verweigert dann jedem anderen das Loeschen
        (kein FILE_SHARE_DELETE), ein Lock kann nicht gestohlen werden; stirbt der Halter, gibt das System die Datei frei und der
        naechste raeumt sie weg. Zusaetzlich steht die PID darin (Meldung). Unter Linux haelt flock die Sperre."""
        for _ in range(3):
            try:
                fd = os.open(self._lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    pid = int(open(self._lock).read().strip() or "0")
                except (OSError, ValueError):
                    pid = 0
                try:
                    os.remove(self._lock)  # gelingt nur, wenn kein lebender Halter die Datei offen haelt
                except PermissionError:
                    raise ProtokollGesperrt("%s wird von Prozess %s geschrieben" % (self.pfad, pid or "?"))
                except FileNotFoundError:
                    pass
                if pid and _pid_lebt(pid):
                    # Datei war loeschbar, Prozess lebt aber (anderes System ohne Loeschsperre): nicht anfassen
                    raise ProtokollGesperrt("%s wird von Prozess %d geschrieben" % (self.pfad, pid))
                continue
            if os.name != "nt":
                try:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    os.close(fd)
                    raise ProtokollGesperrt("%s ist gesperrt (flock)" % self.pfad)
            os.write(fd, str(os.getpid()).encode())
            self._lockfd = fd  # bleibt offen bis _entsperren
            return
        raise ProtokollGesperrt("Lock %s nicht erhaeltlich" % self._lock)

    def _entsperren(self):
        fd = getattr(self, "_lockfd", None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            self._lockfd = None
        try:
            os.remove(self._lock)
        except OSError:
            pass

    def schreiben(self, ereignis, quelle="", ziel="", sha256="", bytes_=None, werkzeug="", mandant="", lauf="", meldung="", zeit=None, **extra):
        if ereignis not in EREIGNISSE:
            raise ValueError("unbekanntes Ereignis: %r" % ereignis)
        nr = self.nr + 1
        e = {"nr": nr, "zeit": zeit or _jetzt(), "ereignis": ereignis, "quelle": quelle, "ziel": ziel,
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
        a = {"zeit": _jetzt(), "mandant": mandant, "lauf": lauf, "nr": self.nr, "kette": self.kette, "protokoll": self.pfad}
        with open(pfad, "a", newline="\n", **ENC) as f:
            f.write(json.dumps(a, ensure_ascii=False) + "\n"); f.flush(); os.fsync(f.fileno())
        return a

    def close(self):
        try:
            self._f.flush(); os.fsync(self._f.fileno()); self._f.close()
        finally:
            self._entsperren()

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
            return {"zeilen": n, "kette": "BRUCH", "bruch_bei": {"zeile": zeilennr, "fehler": fehler}, "anker": "nicht geprueft", "brueche": brueche}
        k = e.get("kette")
        ohne = {a: b for a, b in e.items() if a != "kette"}
        if e.get("nr") != vor_nr + 1 or _kette(vor, ohne) != k:
            brueche.append({"zeile": n, "nr": e.get("nr"), "zeit": e.get("zeit"), "ereignis": e.get("ereignis"), "werkzeug": e.get("werkzeug")})
        elif e.get("ereignis") == "notiz" and e.get("vermerk") == "kettenbruch":
            vermerkt.update(int(z) for z in (e.get("bruch_bei") or []))
        vor, vor_nr = k, e.get("nr") or vor_nr
    offen = [b for b in brueche if b["zeile"] not in vermerkt]
    if offen:
        return {"zeilen": n, "kette": "BRUCH", "bruch_bei": offen[0], "brueche": brueche, "offen": len(offen), "anker": "nicht geprueft"}
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


def _datei_sha(pfad):
    h = hashlib.sha256()
    with open(pfad, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def einlesen_kopierprotokoll(alt, pfad, mandant, lauf, werkzeug="kopieren.py"):
    """Altes kopieren.py-Protokoll (status ok/schon_da/geaendert/nicht_im_index/fehler/mismatch/
    quelle_verschwunden/symlink) als F8-Ereignisse uebernehmen. Original bleibt unberuehrt.
    Idempotent: dieselbe Altdatei (sha256) wird nicht zweimal uebernommen. Altzeiten sind zonenlos und
    werden in zeit_alt belassen (zeit_zone = 'angenommen lokal'); zeit = Einlesezeitpunkt."""
    abb = {"ok": "kopiert", "geaendert": "kopiert", "nicht_im_index": "kopiert", "schon_da": "gesehen",
           "mismatch": "fehler", "fehler": "fehler", "quelle_verschwunden": "notiz", "symlink": "notiz"}
    alt_sha = _datei_sha(alt)
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


def main():
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
            print("Datei:", r["datei"]); print("sha256:", ", ".join(r["sha256"]) or "-")
            for titel, liste in (("Direkt", r["direkt"]), ("Rueckwaerts (woher)", r["rueckwaerts"]), ("Vorwaerts (wohin)", r["vorwaerts"])):
                print("\n%s: %d Ereignis(se)" % (titel, len(liste)))
                for e in liste:
                    print("  #%s %s %-10s %s -> %s %s" % (e["nr"], e["zeit"], e["ereignis"], e.get("quelle", ""), e.get("ziel", ""), ("[" + e["meldung"] + "]") if e.get("meldung") else ""))
            print("\nGleicher Inhalt (sha256) an %d Stelle(n)" % len(r["gleicher_inhalt"]))
            for e in r["gleicher_inhalt"][:20]:
                print("  #%s %s %s" % (e["nr"], e["ereignis"], e.get("ziel") or e.get("quelle")))
            if r["abgeschnitten"]:
                print("\nHINWEIS: Suche nach %d Stufen abgeschnitten." % 50)


if __name__ == "__main__":
    main()
