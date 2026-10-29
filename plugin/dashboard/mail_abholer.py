"""Trove F5 — mail archive: fetch mailboxes, store each mail as .eml and every attachment with checksum in the receipt log (optional).

Inactive by default. Mailboxes come only from Hermes:
  "imap":  settings imap_host, imap_user, imap_port (993), imap_folders (comma list, default INBOX) + secret TROVE_IMAP_PASSWORD.
           IMAP over TLS (IMAP4_SSL) only.
  "gmail": the OAuth client declared under oauth: in plugin.yaml (TROVE_GMAIL_CLIENT_ID / TROVE_GMAIL_CLIENT_SECRET, read-only
           scope). The user signs in once with `hermes auth add reraise-trove:gmail`; Hermes stores and refreshes the token and
           attaches it to each request (zugang.anfrage_mit_anmeldung). Trove never holds or refreshes a token.
Storage: <TROVE_DATEN>/<client>/mail/<mailbox>/<YYYY>/<id>/mail.eml + anhaenge/<name>; id = sha256(Message-ID)[:16].
Log (F8): "gesehen" per mail, "kopiert" per attachment (sha256 after read-back). State per mailbox: .../mail/<mailbox>/stand.json
(gmail: last internalDate + the last FERTIG_MAX message ids; imap: last UID per folder). Idempotent, never overwrites.
"""
import base64, email, email.policy, email.utils, hashlib, imaplib, json, os, re, sys, time, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import belegprotokoll as bp
import zugang
from zugang import NichtEingerichtet

WERKZEUG = "trove:mail"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me/"
FERTIG_MAX = 5000  # Gmail: remember only the last N fetched message ids (stand.json stays small; older mails lie before
                   # the time mark and a re-fetch is harmless anyway: same content -> "schon_da", nothing overwritten)


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


# ---------- Gmail (REST through Hermes: the sign-in stays in Hermes) ----------
class Gmail:
    def api(self, path, params=None):
        url = GMAIL_API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for versuch in range(6):
            try:
                st, body = zugang.anfrage_mit_anmeldung(zugang.GMAIL_OAUTH_ID, "GET", url, {"Accept": "application/json"}, 120)
            except (OSError, TimeoutError, zugang.DienstFehler) as e:
                if isinstance(e, zugang.DienstFehler) and str(e) != zugang.GMAIL_NETZ:
                    raise
                if versuch < 5:
                    time.sleep(2 ** versuch); continue
                raise zugang.DienstFehler(zugang.GMAIL_NETZ) from None
            if st == 200:
                return json.loads(body or b"{}")
            text = zugang.kurz(body or b"", 300)
            if (st in (429, 500, 503) or (st == 403 and ("Quota" in text or "ate limit" in text))) and versuch < 5:
                time.sleep(30 if st == 403 else 2 ** versuch); continue
            if st == 401:
                raise NichtEingerichtet(zugang.GMAIL_ANMELDEN)
            raise zugang.DienstFehler(zugang.GMAIL_ABGELEHNT % st)

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
        if not re.fullmatch(r"[0-9A-Za-z]{1,64}", str(mid)):
            raise zugang.DienstFehler("Gmail: unexpected message id")
        r = self.api("messages/" + mid, {"format": "raw"})
        return base64.urlsafe_b64decode(r["raw"] + "=" * (-len(r["raw"]) % 4)), int(r.get("internalDate", 0)), r.get("labelIds", [])


# ---------- IMAP (stdlib) ----------
class Imap:
    def __init__(self, host, user, passwort, port=993):
        self.c = imaplib.IMAP4_SSL(host, port, timeout=120)
        try:
            self.c.login(user, passwort)
        except imaplib.IMAP4.error:
            raise zugang.DienstFehler("IMAP login rejected for the configured user")

    def uids(self, ordner, ab_uid):
        typ, _ = self.c.select('"%s"' % ordner, readonly=True)
        if typ != "OK":
            raise zugang.DienstFehler("IMAP folder not selectable: " + ordner)
        uv = (self.c.response("UIDVALIDITY")[1] or [b""])[0]
        self.uidvalidity = uv.decode() if isinstance(uv, bytes) else str(uv or "")
        typ, d = self.c.uid("search", None, "UID %d:*" % (ab_uid + 1))
        return [int(u) for u in (d[0].split() if typ == "OK" and d and d[0] else []) if int(u) > ab_uid]

    def roh(self, uid):
        typ, d = self.c.uid("fetch", str(uid), "(RFC822)")
        if typ != "OK" or not d or d[0] is None:
            raise zugang.DienstFehler("IMAP fetch %s: %s" % (uid, typ))
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
        prot.schreiben("gesehen", quelle=quelle_mail, ziel=eml, sha256=sha, bytes_=len(roh), werkzeug=WERKZEUG, mandant=mandant, lauf="mail-" + postfach,
                       meldung="Mail: %s | von %s | %s | %d Anhaenge" % (kopf["betreff"], kopf["von"], kopf["datum"], len(anh)), **kopf, anhaenge=len(anh))
    elif st in ("konflikt", "lesefehler"):
        prot.schreiben("fehler", quelle=quelle_mail, ziel=eml, werkzeug=WERKZEUG, mandant=mandant, lauf="mail-" + postfach,
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
            prot.schreiben("kopiert", quelle=quelle_mail + "/" + n, ziel=p, sha256=sha2, bytes_=len(daten), werkzeug=WERKZEUG, mandant=mandant, lauf="mail-" + postfach,
                           meldung="Anhang aus Mail: %s | %s" % (kopf["betreff"][:120], typ), message_id=kopf["message_id"], betreff=kopf["betreff"], von=kopf["von"], datum=kopf["datum"], mime=typ)
        elif st2 in ("konflikt", "lesefehler"):
            prot.schreiben("fehler", quelle=quelle_mail + "/" + n, ziel=p, werkzeug=WERKZEUG, mandant=mandant, lauf="mail-" + postfach, meldung="Anhang: " + st2)
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
        prot.schreiben("notiz", werkzeug=WERKZEUG, mandant=mandant, lauf="mail-" + pf["id"], meldung="Abholung Start: %s (%s)" % (pf["id"], pf["art"]))
        if pf["art"] == "gmail":
            g = Gmail()
            konto = g.profil().get("emailAddress", "?")
            ab = int(s.get("letzte_internal_ms", 0)) // 1000
            q = abfrage or ("" if alles else ("after:%d" % max(0, ab - 86400) if ab else ""))
            if nur_anhaenge:
                q = (q + " has:attachment").strip()
            hoechst = int(s.get("letzte_internal_ms", 0))
            fertig_liste = list(s.get("fertig", []))[-FERTIG_MAX:]
            fertig = set(fertig_liste)
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
                    prot.schreiben("fehler", quelle="mail:%s/gmail-id-%s" % (pf["id"], mid), werkzeug=WERKZEUG, mandant=mandant, lauf="mail-" + pf["id"], meldung="Ablage: " + zugang.fehlertext(ex, 300))
                    continue
                z["mails"] += 1
                if e.get("eml_status") in ("konflikt", "lesefehler"):
                    z["fehler"] += 1; continue
                if not e.get("uebersprungen"):
                    fertig.add(mid); fertig_liste.append(mid)
                    z["neu" if e["eml_status"] == "neu" else "schon_da"] += 1
                    z["anhaenge_neu"] += sum(1 for a in e["anhaenge"] if a["status"] == "neu")
                hoechst = max(hoechst, ims)
                if z["mails"] % 50 == 0:
                    s["konto"] = konto; s["fertig"] = fertig_liste[-FERTIG_MAX:]; _stand_speichern(mandant, pf["id"], s)
            if voll and z["fehler"] == 0:
                s["letzte_internal_ms"] = hoechst
            s["konto"] = konto; s["fertig"] = fertig_liste[-FERTIG_MAX:]
        elif pf["art"] == "imap":
            passwort = zugang.geheimnis(zugang.IMAP_PASSWORT)
            if not passwort:
                raise NichtEingerichtet("IMAP is not configured in Hermes (%s)" % zugang.IMAP_PASSWORT)
            im = Imap(pf["host"], pf["user"], passwort, int(pf.get("port", 993)))
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
                        prot.schreiben("fehler", quelle="mail:%s/%s/uid-%d" % (pf["id"], ordner, uid), werkzeug=WERKZEUG, mandant=mandant, lauf="mail-" + pf["id"], meldung="Ablage: " + zugang.fehlertext(ex, 300))
                        continue
                    z["mails"] += 1
                    if e.get("eml_status") in ("konflikt", "lesefehler"):
                        z["fehler"] += 1
                    elif not e.get("uebersprungen"):
                        z["neu" if e["eml_status"] == "neu" else "schon_da"] += 1
                        z["anhaenge_neu"] += sum(1 for a in e["anhaenge"] if a["status"] == "neu")
                    uids[ordner] = max(int(uids.get(ordner, 0)), uid)
        else:
            raise ValueError("unknown mailbox kind: " + str(pf.get("art")))
        _stand_speichern(mandant, pf["id"], s)
        prot.schreiben("notiz", werkzeug=WERKZEUG, mandant=mandant, lauf="mail-" + pf["id"], meldung="Abholung Ende: " + json.dumps(z, ensure_ascii=False), **z)
        prot.anker_setzen(mandant, "mail-" + pf["id"])
    finally:
        if eigen:
            prot.close()
    return z


def postfaecher():
    """Mailboxes configured in Hermes, each with (active, missing settings). No network."""
    e = zugang.einstellungen()
    fehlt = [n for n in ("imap_host", "imap_user") if not str(e[n] or "").strip()]
    if not zugang.ist_gesetzt(zugang.IMAP_PASSWORT):
        fehlt.append(zugang.IMAP_PASSWORT)
    try:
        port = int(e["imap_port"] or 993)
    except (TypeError, ValueError):
        port = 993
    ordner = [o.strip() for o in (e["imap_folders"] if isinstance(e["imap_folders"], list) else str(e["imap_folders"] or "INBOX").split(",")) if str(o).strip()]
    imap = {"id": "imap", "art": "imap", "host": str(e["imap_host"] or "").strip(), "user": str(e["imap_user"] or "").strip(), "port": port,
            "ordner": ordner or ["INBOX"], "aktiv": not fehlt, "fehlt": fehlt}
    gfehlt = [] if zugang.ist_gesetzt(zugang.GMAIL_CLIENT_ID) else [zugang.GMAIL_CLIENT_ID]
    zugang.geheimnis(zugang.GMAIL_CLIENT_SECRET)  # never used by Trove (Hermes sends it), read only so it is masked in every text
    gmail = {"id": "gmail", "art": "gmail", "abfrage": str(e["gmail_query"] or "").strip(), "aktiv": not gfehlt, "fehlt": gfehlt,
             "anmeldung": "hermes auth add %s:%s" % (zugang.PLUGIN_ID, zugang.GMAIL_OAUTH_ID)}
    return [imap, gmail]


def postfach(pid):
    pf = next((p for p in postfaecher() if p["id"] == pid), None)
    if pf is None:
        raise ValueError("unknown mailbox: %s (imap | gmail)" % pid)
    if not pf["aktiv"]:
        raise NichtEingerichtet("mailbox %s is not configured in Hermes (missing: %s)" % (pid, ", ".join(pf["fehlt"])))
    return pf


def _selbsttest():
    """No configuration -> inactive, no request. Gmail through a mocked Hermes sign-in: mail + attachment stored, logged, idempotent.
    IMAP with a mocked Hermes password and a fake server class: the password goes only to login()."""
    import tempfile
    from email.message import EmailMessage
    alt = zugang.geheimnis_quelle, zugang.einstellungen_quelle, zugang.anfrage_quelle, bp.WURZEL, globals()["Imap"]
    anfragen = []
    try:
        with tempfile.TemporaryDirectory() as w:
            bp.WURZEL = w
            zugang.geheimnis_quelle = lambda n: None
            zugang.einstellungen_quelle = lambda: {}
            zugang.anfrage_quelle = lambda *a: anfragen.append(a) or (500, b"")
            assert [p["aktiv"] for p in postfaecher()] == [False, False]
            for pid in ("imap", "gmail"):
                try:
                    postfach(pid)
                    raise AssertionError("mailbox active without configuration")
                except NichtEingerichtet:
                    pass
            assert not anfragen
            m = EmailMessage()
            m["Message-ID"] = "<t1@example.invalid>"; m["Subject"] = "Invoice"; m["From"] = "a@example.invalid"; m["Date"] = "Tue, 20 Oct 2026 10:00:00 +0000"
            m.set_content("hello")
            m.add_attachment(b"%PDF-1.4 test", maintype="application", subtype="pdf", filename="r.pdf")
            raw = base64.urlsafe_b64encode(m.as_bytes()).decode()

            def gmail(provider, methode, url, headers, timeout):
                anfragen.append((provider, methode, url))
                assert provider == "gmail" and methode == "GET" and url.startswith(GMAIL_API) and "Authorization" not in headers
                if url.startswith(GMAIL_API + "profile"):
                    return 200, b'{"emailAddress": "me@example.invalid"}'
                if url.startswith(GMAIL_API + "messages?"):
                    return 200, b'{"messages": [{"id": "abc123"}]}'
                return 200, json.dumps({"raw": raw, "internalDate": "1792490400000"}).encode()
            zugang.anfrage_quelle = gmail
            zugang.geheimnis_quelle = lambda n: {zugang.GMAIL_CLIENT_ID: "client-id.apps.example"}.get(n)
            pf = postfach("gmail")
            z = abholen("t", pf)
            assert z["neu"] == 1 and z["anhaenge_neu"] == 1 and z["fehler"] == 0, z
            z = abholen("t", pf)
            assert z["neu"] == 0 and z["schon_da"] == 1, z
            ereignisse = [e["ereignis"] for e in bp.eintraege(bp.pfad_fuer("t"))]
            assert "gesehen" in ereignisse and "kopiert" in ereignisse, ereignisse
            gesehen_pw = []

            class FakeImap:
                def __init__(self, host, user, passwort, port=993):
                    gesehen_pw.append((host, user, passwort, port))
                    self.uidvalidity = "1"

                def uids(self, ordner, ab_uid):
                    return [1] if ab_uid < 1 else []

                def roh(self, uid):
                    return m.as_bytes()
            globals()["Imap"] = FakeImap
            zugang.geheimnis_quelle = lambda n: {zugang.IMAP_PASSWORT: "imap-pw-9876"}.get(n)
            zugang.einstellungen_quelle = lambda: {"imap_host": "127.0.0.1", "imap_user": "u", "imap_port": 9}
            z = abholen("t", postfach("imap"))
            assert gesehen_pw == [("127.0.0.1", "u", "imap-pw-9876", 9)] and z["mails"] == 1 and z["fehler"] == 0, (gesehen_pw, z)
            with open(bp.pfad_fuer("t"), encoding="utf-8") as f:
                assert "imap-pw-9876" not in f.read()
    finally:
        zugang.geheimnis_quelle, zugang.einstellungen_quelle, zugang.anfrage_quelle, bp.WURZEL, globals()["Imap"] = alt


if __name__ == "__main__":
    _selbsttest()
    print("mail selftest ok")
