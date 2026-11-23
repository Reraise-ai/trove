"""Request queue for requests that arrive over MCP (mcp_server.py).

An MCP client (Claude, Cursor, any agent system) does not change files directly; every write runs through this queue: it becomes an entry here, is
logged, and runs through the same backend functions as a click in the tab (plugin_api.aktion, plugin_api.wissen), with the
permission checks (folder rights) as they are at that moment.
Default (switch `freigabe_pflicht` OFF, konfig.freigabe_pflicht): the MCP server starts the entry right after queueing it
(status running -> done | failed). Switch ON: the entry waits as "blocked", shows up in the Trove tab's task list next to the
Hermes kanban cards, and only the tab's release button (POST /auftrag/<id>/freigeben in plugin_api.py) moves it on; the MCP
server then has no code path that releases. The run is a thread in the process that starts it: it ends with the MCP server
process (LAUFZEIT_S cleans up such entries).

Storage: <TROVE_DATEN>/<client>/mcp_auftraege.json, written under a lock file (belegprotokoll.WarteSperre) because two processes
use it (the MCP server creates entries and starts them when the switch is off; the Hermes dashboard releases them when it is on).
An agent task (agent_auftrag) starts a Hermes agent with Hermes' own permissions; Trove's folder rights do not bind it.
Status: blocked -> running -> done | failed; blocked -> rejected (archive button in the tab). Every step goes to the receipt log.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time

import belegprotokoll as bp

PREFIX = "mcp-"
RE_ID = re.compile(r"mcp-[0-9a-f]{10}")
DATEI = "mcp_auftraege.json"
WERKZEUG = "trove-mcp"
KARTE_MAX_RUNTIME_S, KARTE_MAX_RETRIES = 900, 1  # limits of a Hermes card (plugin_api._auftrag_anlegen): how long its agent can run
ANLEGEN_LOG_WARTEN_S = 5  # anlegen waits this long for the receipt log while it holds the queue lock (see anlegen)
SETZEN_FRIST_S = 600  # a finished run retries saving its result this long while the queue is busy


def _agent_auftrag(api, body):
    """Agent task (started directly, or released in the tab): create the Hermes kanban card and start it in one go."""
    herkunft = "mcp:%s" % ((body.get("kontext") or {}).get("mcp_client") or "?")
    karte = api._auftrag_anlegen(body, herkunft, starten=True)
    if karte.get("fehler"):
        raise RuntimeError(karte["fehler"])
    return {"karte": karte["id"]}


# kind -> (backend function in plugin_api, label, after-run seconds). After-run: how long a finished request still counts against
# the limits because work it started can still be going on (an agent task: its card's agent, every attempt at full runtime,
# 30 min; done or failed alike, and archiving does not free the place).
# Extension point: rename / new folder plug in here once the backend has them.
AUSFUEHRUNG = {
    "kopieren": ("aktion", "copy", 0),
    "verschieben": ("aktion", "move", 0),
    "sync": ("aktion", "sync", 0),
    "wissen": ("wissen", "into knowledge", 0),
    "umbenennen": ("umbenennen", "rename", 0),  # active once the backend has POST /umbenennen
    "ordner_neu": ("ordner_neu", "new folder", 0),  # active once the backend has POST /ordner-neu
    "agent_auftrag": (_agent_auftrag, "agent task", KARTE_MAX_RUNTIME_S * (KARTE_MAX_RETRIES + 1)),
}
MAX_OFFEN_JE_CLIENT = 20  # open requests per MCP client name: blocked, running, or an agent task within its after-run
MAX_OFFEN_GESAMT = 50  # open requests of all clients together (client names are self-reported)
OFFEN_TEXT = "waiting, running, or an agent task finished less than %d min ago; archiving does not free it" % (AUSFUEHRUNG["agent_auftrag"][2] // 60)
LAUFZEIT_S = int(os.environ.get("TROVE_MCP_LAUFZEIT_S") or 3 * 3600)  # "running" longer than this: the run is presumed dead, archivable




def ist_mcp(kid):
    return bool(RE_ID.fullmatch(kid or ""))


def _pfad(mandant):
    d = os.path.join(bp.WURZEL, mandant)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, DATEI)


def _lesen(p):
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return []


def _speichern(p, liste):
    with open(p + ".neu", "w", encoding="utf-8") as f:
        json.dump(liste, f, ensure_ascii=False, indent=1)
    os.replace(p + ".neu", p)


def log(mandant, meldung, ereignis="notiz", warten_s=30, werkzeug=WERKZEUG, **felder):
    """One receipt-log line. The log lock is held for a whole copy run by the backend, so wait for it (up to warten_s)
    instead of failing at once; still locked -> belegprotokoll.ProtokollGesperrt."""
    ende = time.time() + warten_s
    while True:
        try:
            return bp.schreiben(bp.pfad_fuer(mandant), ereignis=ereignis, werkzeug=werkzeug, mandant=mandant, meldung=meldung, **felder)
        except bp.ProtokollGesperrt:
            if time.time() > ende:
                raise
            time.sleep(0.2)


def _log(mandant, kid, meldung, ereignis="notiz", **extra):
    log(mandant, meldung, ereignis=ereignis, lauf=kid, mcp_auftrag=kid, **extra)


def _offen(e, jetzt):
    """Counts against the limits: waiting, running and not yet presumed dead (LAUFZEIT_S), or finished but within its after-run
    time (belegt_bis, see AUSFUEHRUNG)."""
    return (e["status"] == "blocked" or (e["status"] == "running" and jetzt - float(e.get("freigegeben_ts") or 0) <= LAUFZEIT_S)
            or jetzt < float(e.get("belegt_bis") or 0))


def anlegen(mandant, client, art, body, titel, details):
    """New request, status blocked (the MCP server starts it right away when the switch is off, see freigeben(direkt=True)).
    details: the complete text shown in the tab (all names, exact paths, full text).
    Returns the entry. Called by the MCP server. Raises ValueError when the client already has too many open requests."""
    if art not in AUSFUEHRUNG:
        raise ValueError("unknown kind: %s" % art)
    kid = PREFIX + secrets.token_hex(5)
    e = {"id": kid, "art": art, "body": body, "client": client, "titel": titel, "details": details, "zeit": bp.jetzt(), "status": "blocked"}
    p = _pfad(mandant)
    with bp.WarteSperre(p + ".lock"):  # waits up to 20 s, then bp.DateiBelegt
        alle = _lesen(p)
        jetzt = time.time()
        offen = [x for x in alle if _offen(x, jetzt)]
        n = sum(1 for x in offen if x.get("client") == client)
        if n >= MAX_OFFEN_JE_CLIENT:
            raise ValueError("%d requests from this client are still open (%s); try again later" % (n, OFFEN_TEXT))
        if len(offen) >= MAX_OFFEN_GESAMT:
            raise ValueError("%d requests are still open (%s); try again later" % (len(offen), OFFEN_TEXT))
        # log first: no queue entry without its log line. Lock holders never wait longer than lock waiters (WarteSperre, 20 s):
        # a long copy run holds the receipt log, and waiting 30 s here would make the end of a running request lose its result.
        _log(mandant, kid, "MCP request from %s queued: %s" % (client, titel), warten_s=ANLEGEN_LOG_WARTEN_S,
             mcp_client=client, herkunft="mcp:" + client, anfrage={"art": art, **body})
        _speichern(p, alle + [e])
    return e


def holen(mandant, kid):
    for e in _lesen(_pfad(mandant)):
        if e["id"] == kid:
            return e
    return None


def liste(mandant):
    return _lesen(_pfad(mandant))


def _setzen(mandant, kid, erwartet, **felder):
    """Atomic status transition. Returns the updated entry or None if the entry was not in an expected state."""
    p = _pfad(mandant)
    with bp.WarteSperre(p + ".lock"):  # waits up to 20 s, then bp.DateiBelegt
        alle = _lesen(p)
        for e in alle:
            if e["id"] == kid:
                if erwartet and e["status"] not in erwartet:
                    return None
                e.update(felder)
                _speichern(p, alle)
                return e
    return None


def _setzen_wartend(mandant, kid, erwartet, **felder):
    """_setzen, retried while the queue is busy (bp.DateiBelegt) for up to SETZEN_FRIST_S: a state change that must not be lost."""
    ende = time.time() + SETZEN_FRIST_S
    while True:
        try:
            return _setzen(mandant, kid, erwartet, **felder)
        except bp.DateiBelegt:
            if time.time() > ende:
                raise


def _details_text(e):
    zeilen = ["Requested by MCP client: %s (name reported by the client itself)" % e.get("client"), "At: %s" % e.get("zeit"), e.get("details") or e.get("detail") or ""]
    if e.get("ergebnis"):
        zeilen.append("Result: " + e["ergebnis"])
    return "\n".join(z for z in zeilen if z)


def fuer_tab(mandant):
    """Entries in the shape of the tab's task list (newest first, archived ones hidden by the tab)."""
    out = []
    for e in reversed(_lesen(_pfad(mandant))):
        st = {"rejected": "archived"}.get(e["status"], e["status"])
        out.append({"id": e["id"], "titel": "MCP %s: %s" % (e["client"], e["titel"]), "status": st, "zeit": e["zeit"], "quelle": "mcp",
                    "zusammenfassung": e.get("ergebnis") or "", "details": _details_text(e)})
    return out


def freigeben(mandant, kid, api, warten=False, direkt=False):
    """Start a blocked request in a background thread through the backend function. Called from the tab's release route (a click),
    or with direkt=True by the MCP server right after queueing when the switch freigabe_pflicht is OFF (no click involved)."""
    e = _setzen(mandant, kid, ("blocked",), status="running", freigegeben=bp.jetzt(), freigegeben_ts=time.time())
    if e is None:
        cur = holen(mandant, kid)
        if cur is None:
            raise KeyError(kid)
        raise ValueError("request %s is %s, only a blocked request can be released" % (kid, cur["status"]))
    try:
        _log(mandant, kid, ("MCP request from %s started directly (freigabe_pflicht off): %s" if direkt else
                            "MCP request from %s released in the Trove tab: %s") % (e["client"], e["titel"]), mcp_client=e["client"], herkunft="mcp:" + e["client"])
    except BaseException:  # receipt log held by a long run: no run without its log line, and not stuck in "running" either
        _setzen_wartend(mandant, kid, ("running",), status="blocked")
        raise

    def lauf():
        fn = AUSFUEHRUNG[e["art"]][0]
        fn = getattr(api, fn) if isinstance(fn, str) else (lambda b, _f=fn: _f(api, b))
        body = dict(e["body"], mandant=mandant)
        if e["art"] in ("kopieren", "verschieben", "sync"):
            body["art"] = e["art"]
        if e["art"] == "agent_auftrag":
            body["client_id"] = kid  # a repeated run of the same request never creates a second kanban card
        try:
            r = fn(body)
            kurz = json.dumps({k: r.get(k) for k in ("lauf", "ergebnis", "verarbeitet", "neu", "fehler") if k in r}, ensure_ascii=False)[:600]
            fehler = (isinstance(r.get("fehler"), int) and r["fehler"] > 0) or any((x.get("fehler") or x.get("konflikt") or x.get("status") == "fehlt") for x in (r.get("ergebnis") or []) if isinstance(x, dict))
            status = "failed" if fehler else "done"
            ergebnis = ("done: " if status == "done" else "finished with conflicts/errors: ") + kurz
        except Exception as ex:  # HTTPException from the backend (e.g. folder switched to read-only meanwhile) and anything else
            text = str(getattr(ex, "detail", "") or ex or type(ex).__name__)
            status, ergebnis = "failed", "failed: " + getattr(api, "_aussen", str)(text)[:600]  # secrets masked, paths stripped, then cut
        try:
            _log(mandant, kid, "MCP request %s from %s %s" % (kid, e["client"], ergebnis), ereignis="notiz" if status == "done" else "fehler",
                 mcp_client=e["client"], warten_s=600)
        finally:
            try:
                _setzen_wartend(mandant, kid, None, status=status, ende=bp.jetzt(), ergebnis=ergebnis, belegt_bis=time.time() + AUSFUEHRUNG[e["art"]][2])
            except bp.DateiBelegt:  # gave up after SETZEN_FRIST_S: say so in the log instead of ending silently in this thread
                _log(mandant, kid, "MCP request %s from %s: result could not be saved, the request queue stayed busy for %d s (%s)"
                     % (kid, e["client"], SETZEN_FRIST_S, ergebnis), ereignis="fehler", mcp_client=e["client"], warten_s=600)

    if warten:
        lauf()
    else:
        threading.Thread(target=lauf, name="trove-mcp-" + kid, daemon=True).start()
    return {"ok": True, "id": kid, "status": "running"}


def ablehnen(mandant, kid):
    """Archive button in the tab, one atomic step: a blocked request is rejected, a finished one is hidden, a running one only
    when it has been running longer than LAUFZEIT_S (the run is presumed dead, e.g. the dashboard was closed)."""
    p = _pfad(mandant)
    with bp.WarteSperre(p + ".lock"):  # waits up to 20 s, then bp.DateiBelegt
        alle = _lesen(p)
        e = next((x for x in alle if x["id"] == kid), None)
        if e is None:
            raise KeyError(kid)
        vorher = e["status"]
        if vorher == "blocked":
            e.update(status="rejected", ende=bp.jetzt())
        elif vorher in ("done", "failed"):
            e["status"] = "archived"
        elif vorher == "running" and not _offen(e, time.time()):
            e.update(status="archived", ende=bp.jetzt(), ergebnis="abandoned: still running after %d s, presumed dead (no result recorded)" % LAUFZEIT_S)
        elif vorher == "running":
            raise ValueError("request %s is running right now; it can be archived when it has finished" % kid)
        else:
            raise ValueError("request %s is already %s" % (kid, vorher))
        _speichern(p, alle)
    if vorher == "blocked":
        _log(mandant, kid, "MCP request from %s rejected in the Trove tab: %s" % (e["client"], e["titel"]), mcp_client=e["client"])
    elif vorher == "running":
        _log(mandant, kid, "MCP request from %s archived in the Trove tab after it hung in 'running': %s" % (e["client"], e["titel"]), ereignis="fehler", mcp_client=e["client"])
    return {"ok": True}
