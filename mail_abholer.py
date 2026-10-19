"""Trove F5 — E-Mail-Archiv: Postfaecher abholen, Mail als .eml ablegen, Anhaenge einzeln mit Pruefsumme ins Belegprotokoll (Mail <-> Beleg).

Postfaecher je Mandant: <TROVE_DATEN>\\<mandant>\\postfaecher.json, Liste von
  {"id": "privat", "art": "gmail", "token_datei": "<Ordner>\\google_token.json"}
  {"id": "info",   "art": "imap",  "host": "imap.example.com", "user": "...", "passwort_datei": "C:\\...\\geheim.txt", "ordner": ["INBOX"]}
Nie Passwoerter/Token in dieser Datei oder im Repo — nur Pfade. Das Gmail-Token wird gelesen, nie zurueckgeschrieben (Hermes bleibt Eigentuemer).

Ablage: <TROVE_DATEN>\\<mandant>\\mail\\<postfach>\\<JJJJ>\\<kennung>\\mail.eml + anhaenge\\<name>; kennung = sha256(Message-ID)[:16].
Protokoll (F8): "gesehen" je Mail (quelle mail:<postfach>/<message-id>, ziel .eml, sha256 der .eml, meldung Betreff/Absender/Datum),
                 "kopiert" je Anhang (quelle mail:<postfach>/<message-id>/<name>, ziel Datei, sha256 nach Rueckleseprobe).
Stand je Postfach: <mandant>\\mail\\<postfach>\\stand.json (gmail: letzte internalDate; imap: letzte UID je Ordner). Idempotent.
"""
import argparse, base64, email, email.policy, hashlib, imaplib, json, os, re, sys, time, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import belegprotokoll as bp

BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sicherer_name(n, fallback="anhang"):
    n = (n or "").strip().replace("\r", "").replace("\n", "")
    n = BAD.sub("_", n)[:120].strip(". ") or fallback
    return n


def _sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


def _sha_datei(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _schreiben_und_pruefen(pfad, daten):
    """Nie ueberschreiben: gleicher Inhalt -> schon_da, anderer Inhalt -> Fehler. Rueckleseprobe gegen sha."""
    soll = _sha_bytes(daten)
    if os.path.exists(pfad):
        ist = _sha_datei(pfad)
        return ("schon_da" if ist == soll else "konflikt"), soll
    os.makedirs(os.path.dirname(pfad), exist_ok=True)
    tmp = pfad + ".teil"
    with open(tmp, "wb") as f:
        f.write(daten)
    if _sha_datei(tmp) != soll:
        os.remove(tmp)
        return "lesefehler", soll
    os.replace(tmp, pfad)
    return "neu", soll


# ---------- Gmail (REST, nur stdlib; Token des Hermes-Skills google-workspace, nur lesen) ----------
class Gmail:
    def __init__(self, token_datei):
        self.t = json.load(open(token_datei, encoding="utf-8"))
        self.access = None
        self.ablauf = 0

    def _token(self):
        if self.access and time.time() < self.ablauf - 60:
            return self.access
        body = urllib.parse.urlencode({"client_id": self.t["client_id"], "client_secret": self.t["client_secret"],
                                       "refresh_token": self.t["refresh_token"], "grant_type": "refresh_token"}).encode()
        with urllib.request.urlopen(urllib.request.Request(self.t.get("token_uri", "https://oauth2.googleapis.com/token"), data=body), timeout=60) as h:
            r = json.loads(h.read())
        self.access, self.ablauf = r["access_token"], time.time() + int(r.get("expires_in", 3600))
        return self.access

    def api(self, path, params=None):
        url = "https://gmail.googleapis.com/gmail/v1/users/me/" + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self._token()})
        for versuch in range(6):
            try:
                with urllib.request.urlopen(req, timeout=120) as h:
                    return json.loads(h.read())
            except urllib.error.HTTPError as e:
                text = e.read()[:300].decode("utf-8", "replace")
                if (e.code in (429, 500, 503) or (e.code == 403 and ("Quota" in text or "ate limit" in text))) and versuch < 5:
                    time.sleep(30 if e.code == 403 else 2 ** versuch); continue
                raise RuntimeError("Gmail %s: %s %s" % (path, e.code, text))
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if versuch < 5:
                    time.sleep(2 ** versuch); continue
                raise RuntimeError("Gmail %s: Netz: %s" % (path, e))

    def profil(self):
        return self.api("profile")

    def ids(self, abfrage, max_n=None):
        seite = None
        n = 0
        while True:
            p = {"q": abfrage, "maxResults": 500}
            if seite:
                p["pageToken"] = seite
            r = self.api("messages", p)
            for m in r.get("messages", []):
                yield m["id"]; n += 1
                if max_n and n >= max_n:
                    return
            seite = r.get("nextPageToken")
            if not seite:
                return

    def roh(self, mid):
        r = self.api("messages/" + mid, {"format": "raw"})
        return base64.urlsafe_b64decode(r["raw"] + "=" * (-len(r["raw"]) % 4)), int(r.get("internalDate", 0)), r.get("labelIds", [])


# ---------- IMAP (stdlib) ----------
class Imap:
    def __init__(self, host, user, passwort_datei, port=993):
        self.c = imaplib.IMAP4_SSL(host, port)
        self.c.login(user, open(passwort_datei, encoding="utf-8").read().strip())

    def uids(self, ordner, ab_uid):
        typ, _ = self.c.select('"%s"' % ordner, readonly=True)
        if typ != "OK":
            raise RuntimeError("IMAP Ordner nicht waehlbar: " + ordner)
        uv = (self.c.response("UIDVALIDITY")[1] or [b""])[0]
        self.uidvalidity = uv.decode() if isinstance(uv, bytes) else str(uv or "")
        typ, d = self.c.uid("search", None, "UID %d:*" % (ab_uid + 1))
        return [int(u) for u in (d[0].split() if typ == "OK" and d and d[0] else []) if int(u) > ab_uid]

    def roh(self, uid):
        typ, d = self.c.uid("fetch", str(uid), "(RFC822)")
        if typ != "OK" or not d or d[0] is None:
            raise RuntimeError("IMAP fetch %s: %s" % (uid, typ))
        return d[0][1]


# ---------- Ablage + Protokoll ----------
def _kopf(msg):
    def k(n):
        v = msg.get(n, "")
        return str(v).replace("\r", " ").replace("\n", " ").strip()
    return {"message_id": k("Message-ID") or "", "betreff": k("Subject")[:300], "von": k("From")[:200], "an": k("To")[:200], "datum": k("Date")[:60]}


def _jahr(msg, fallback_ms=0):
    try:
        d = email.utils.parsedate_to_datetime(msg.get("Date"))
        return "%04d" % d.year
    except Exception:
        return time.strftime("%Y", time.localtime(fallback_ms / 1000)) if fallback_ms else "0000"


def ablegen(prot, mandant, postfach, roh, internal_ms=0, nur_anhaenge=False):
    msg = email.message_from_bytes(roh, policy=email.policy.default)
    kopf = _kopf(msg)
    kennung = hashlib.sha256((kopf["message_id"] or _sha_bytes(roh)).encode("utf-8", "replace")).hexdigest()[:16]
    anh = []
    for teil in msg.walk():
        if teil.get_content_disposition() in ("attachment", "inline") and teil.get_filename():
            daten = teil.get_payload(decode=True)
            if daten:
                anh.append((teil.get_filename(), daten, teil.get_content_type()))
    if nur_anhaenge and not anh:
        return {"kennung": kennung, "uebersprungen": True}
    ordner = os.path.join(bp.WURZEL, mandant, "mail", postfach, _jahr(msg, internal_ms), kennung)
    eml = os.path.join(ordner, "mail.eml")
    st, sha = _schreiben_und_pruefen(eml, roh)
    if st == "konflikt":  # gleiche Message-ID, anderer Inhalt (z. B. INBOX + Gesendet): eigener Ordner je Inhalt
        ordner += "-" + _sha_bytes(roh)[:8]; eml = os.path.join(ordner, "mail.eml"); st, sha = _schreiben_und_pruefen(eml, roh)
    quelle_mail = "mail:%s/%s" % (postfach, kopf["message_id"] or kennung)
    erg = {"kennung": kennung, "eml": eml, "eml_status": st, "anhaenge": []}
    if st == "neu":
        prot.schreiben("gesehen", quelle=quelle_mail, ziel=eml, sha256=sha, bytes_=len(roh), werkzeug="mail_abholer.py", mandant=mandant, lauf="mail-" + postfach,
                       meldung="Mail: %s | von %s | %s | %d Anhaenge" % (kopf["betreff"], kopf["von"], kopf["datum"], len(anh)), **kopf, anhaenge=len(anh))
    elif st in ("konflikt", "lesefehler"):
        prot.schreiben("fehler", quelle=quelle_mail, ziel=eml, werkzeug="mail_abholer.py", mandant=mandant, lauf="mail-" + postfach,
                       meldung="Mail nicht abgelegt: " + ("Datei vorhanden mit anderem Inhalt, nichts ueberschrieben" if st == "konflikt" else "Rueckleseprobe fehlgeschlagen"), **kopf)
        return {"kennung": kennung, "eml": eml, "eml_status": st, "anhaenge": []}
    belegt = set()
    for i, (name, daten, typ) in enumerate(anh, 1):
        n = _sicherer_name(name, "anhang_%d" % i)
        if getattr(os.path, "isreserved", None) and os.path.isreserved(n):
            n = "_" + n
        if n.casefold() in belegt:
            stamm, ext = os.path.splitext(n); n = "%s_%d%s" % (stamm, i, ext)
        belegt.add(n.casefold())
        p = os.path.join(ordner, "anhaenge", n)
        st2, sha2 = _schreiben_und_pruefen(p, daten)
        erg["anhaenge"].append({"name": n, "status": st2, "sha256": sha2, "bytes": len(daten)})
        if st2 == "neu":
            prot.schreiben("kopiert", quelle=quelle_mail + "/" + n, ziel=p, sha256=sha2, bytes_=len(daten), werkzeug="mail_abholer.py", mandant=mandant, lauf="mail-" + postfach,
                           meldung="Anhang aus Mail: %s | %s" % (kopf["betreff"][:120], typ), message_id=kopf["message_id"], betreff=kopf["betreff"], von=kopf["von"], datum=kopf["datum"], mime=typ)
        elif st2 in ("konflikt", "lesefehler"):
            prot.schreiben("fehler", quelle=quelle_mail + "/" + n, ziel=p, werkzeug="mail_abholer.py", mandant=mandant, lauf="mail-" + postfach, meldung="Anhang: " + st2)
    return erg


def _stand_pfad(mandant, postfach):
    return os.path.join(bp.WURZEL, mandant, "mail", postfach, "stand.json")


def _stand(mandant, postfach):
    p = _stand_pfad(mandant, postfach)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}


def _stand_speichern(mandant, postfach, s):
    p = _stand_pfad(mandant, postfach)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".neu", "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)
    os.replace(p + ".neu", p)


def abholen(mandant, pf, max_n=None, nur_anhaenge=False, abfrage=None, prot=None, alles=False):
    """Ein Postfach abholen. Rueckgabe: Zaehlung. Idempotent ueber Stand + nie-ueberschreiben."""
    eigen = prot is None
    if eigen:
        prot = bp.Protokoll(bp.pfad_fuer(mandant))
    z = {"postfach": pf["id"], "mails": 0, "neu": 0, "schon_da": 0, "anhaenge_neu": 0, "fehler": 0}
    s = _stand(mandant, pf["id"])
    try:
        prot.schreiben("notiz", werkzeug="mail_abholer.py", mandant=mandant, lauf="mail-" + pf["id"], meldung="Abholung Start: %s (%s)" % (pf["id"], pf["art"]))
        if pf["art"] == "gmail":
            g = Gmail(pf["token_datei"])
            konto = g.profil().get("emailAddress", "?")
            ab = int(s.get("letzte_internal_ms", 0)) // 1000
            q = abfrage or ("" if alles else ("after:%d" % max(0, ab - 86400) if ab else ""))
            if nur_anhaenge:
                q = (q + " has:attachment").strip()
            hoechst = int(s.get("letzte_internal_ms", 0))
            fertig = set(s.get("fertig", []))
            voll = not (max_n or abfrage or nur_anhaenge)  # nur ein vollstaendiger Lauf darf den Zeitstand vorruecken
            for mid in g.ids(q, max_n):
                if mid in fertig:
                    z["schon_da"] += 1; continue
                try:
                    roh, ims, labels = g.roh(mid)
                    time.sleep(0.2)  # Gmail-Quote (Einheiten je Minute) schonen
                    e = ablegen(prot, mandant, pf["id"], roh, ims, nur_anhaenge)
                except Exception as ex:
                    z["fehler"] += 1
                    prot.schreiben("fehler", quelle="mail:%s/gmail-id-%s" % (pf["id"], mid), werkzeug="mail_abholer.py", mandant=mandant, lauf="mail-" + pf["id"], meldung="Ablage: " + str(ex)[:300])
                    continue
                z["mails"] += 1
                if e.get("eml_status") in ("konflikt", "lesefehler"):
                    z["fehler"] += 1; continue
                if not e.get("uebersprungen"):
                    fertig.add(mid)
                    z["neu" if e["eml_status"] == "neu" else "schon_da"] += 1
                    z["anhaenge_neu"] += sum(1 for a in e["anhaenge"] if a["status"] == "neu")
                hoechst = max(hoechst, ims)
                if z["mails"] % 50 == 0:
                    s["konto"] = konto; s["fertig"] = sorted(fertig); _stand_speichern(mandant, pf["id"], s)
            if voll and z["fehler"] == 0:
                s["letzte_internal_ms"] = hoechst
            s["konto"] = konto; s["fertig"] = sorted(fertig)
        elif pf["art"] == "imap":
            im = Imap(pf["host"], pf["user"], pf["passwort_datei"], int(pf.get("port", 993)))
            uids = s.setdefault("uid", {})
            if alles:
                uids.clear()
            uv_alt = s.setdefault("uidvalidity", {})
            for ordner in pf.get("ordner") or ["INBOX"]:
                liste = im.uids(ordner, int(uids.get(ordner, 0)))
                if uv_alt.get(ordner) and uv_alt[ordner] != im.uidvalidity:  # Server hat UIDs neu vergeben: von vorn (nie ueberschreiben macht das gefahrlos)
                    uids[ordner] = 0; liste = im.uids(ordner, 0)
                uv_alt[ordner] = im.uidvalidity
                for uid in liste[: (max_n or None)]:
                    try:
                        roh = im.roh(uid)
                        e = ablegen(prot, mandant, pf["id"], roh, 0, nur_anhaenge)
                    except Exception as ex:
                        z["fehler"] += 1
                        prot.schreiben("fehler", quelle="mail:%s/%s/uid-%d" % (pf["id"], ordner, uid), werkzeug="mail_abholer.py", mandant=mandant, lauf="mail-" + pf["id"], meldung="Ablage: " + str(ex)[:300])
                        continue
                    z["mails"] += 1
                    if e.get("eml_status") in ("konflikt", "lesefehler"):
                        z["fehler"] += 1
                    elif not e.get("uebersprungen"):
                        z["neu" if e["eml_status"] == "neu" else "schon_da"] += 1
                        z["anhaenge_neu"] += sum(1 for a in e["anhaenge"] if a["status"] == "neu")
                    uids[ordner] = max(int(uids.get(ordner, 0)), uid)
        else:
            raise ValueError("Postfach-Art unbekannt: " + str(pf.get("art")))
        _stand_speichern(mandant, pf["id"], s)
        prot.schreiben("notiz", werkzeug="mail_abholer.py", mandant=mandant, lauf="mail-" + pf["id"], meldung="Abholung Ende: " + json.dumps(z, ensure_ascii=False), **z)
        prot.anker_setzen(mandant, "mail-" + pf["id"])
    finally:
        if eigen:
            prot.close()
    return z


def postfaecher(mandant):
    p = os.path.join(bp.WURZEL, mandant, "postfaecher.json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else []


def main():
    ap = argparse.ArgumentParser(description="Trove F5 Mail-Abholer")
    ap.add_argument("--mandant", default="haus")
    ap.add_argument("--postfach", help="nur dieses Postfach (id)")
    ap.add_argument("--max", type=int, help="hoechstens so viele Mails je Postfach (Probe)")
    ap.add_argument("--nur-anhaenge", action="store_true")
    ap.add_argument("--abfrage", help="Gmail-Suchausdruck statt Stand (z. B. 'newer_than:30d has:attachment')")
    ap.add_argument("--alles", action="store_true", help="Stand ignorieren, ganzes Postfach durchgehen (Erstbefuellung; idempotent)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    out = []
    for pf in postfaecher(a.mandant):
        if a.postfach and pf["id"] != a.postfach:
            continue
        out.append(abholen(a.mandant, pf, a.max, a.nur_anhaenge, a.abfrage, alles=a.alles))
    print(json.dumps(out, ensure_ascii=False) if a.json else "\n".join("%(postfach)s: %(mails)d Mails, %(neu)d neu, %(schon_da)d schon da, %(anhaenge_neu)d Anhaenge neu, %(fehler)d Fehler" % z for z in out))


if __name__ == "__main__":
    main()
