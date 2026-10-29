"""Approval queue for requests that arrive over MCP (mcp_server.py).

An MCP client (Claude, Cursor, any agent system) never changes files directly. Every write request becomes an entry here with
status "blocked". It shows up in the Trove tab's task list next to the Hermes kanban cards. Only the tab's release button
(POST /auftrag/<id>/freigeben in plugin_api.py, i.e. a human click) moves it on; the MCP server has no code path that releases.
After release, the request runs through the same backend functions as a click in the tab (plugin_api.aktion, plugin_api.wissen),
with the permission checks as they are at that moment.

Storage: <TROVE_DATEN>/<client>/mcp_auftraege.json, written under a lock file because two processes use it
(the MCP server creates entries, the Hermes dashboard releases them).
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
def _agent_auftrag(api, body):
    """Released agent task: create the Hermes kanban card and release it in one go (the human has seen the full text in the tab)."""
    karte = api.auftrag_anlegen(body)
    api._hermes("kanban", "unblock", karte["id"], "--reason", "Released by a human in the Trove tab (MCP request)")
    try:
        api._hermes("kanban", "dispatch", "--max", "1", timeout=60)
    except Exception:
        pass
    return {"karte": karte["id"]}


# kind -> (backend function in plugin_api, label). Extension point: rename / new folder plug in here once the backend has them.
AUSFUEHRUNG = {
    "kopieren": ("aktion", "copy"),
    "verschieben": ("aktion", "move"),
    "sync": ("aktion", "sync"),
    "wissen": ("wissen", "into knowledge"),
    "umbenennen": ("umbenennen", "rename"),  # active once the backend has POST /umbenennen
    "ordner_neu": ("ordner_neu", "new folder"),  # active once the backend has POST /ordner-neu
    "agent_auftrag": (_agent_auftrag, "agent task"),
}
_LOCK_STALE_S = 60
MAX_OFFEN_JE_CLIENT = 20  # blocked requests per MCP client name
LAUFZEIT_S = int(os.environ.get("TROVE_MCP_LAUFZEIT_S") or 3 * 3600)  # "running" longer than this: the run is presumed dead, archivable




def ist_mcp(kid):
    return bool(RE_ID.fullmatch(kid or ""))


def _pfad(mandant):
    d = os.path.join(bp.WURZEL, mandant)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, DATEI)


class _Sperre:
    def __init__(self, pfad):
        self.pfad = pfad + ".lock"

    def __enter__(self):
        ende = time.time() + 20
        while True:
            try:
                fd = os.open(self.pfad, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.pfad) > _LOCK_STALE_S:
                        os.remove(self.pfad)
                        continue
                except OSError:
                    continue
                if time.time() > ende:
                    raise RuntimeError("MCP queue is locked by another process")
                time.sleep(0.05)

    def __exit__(self, *a):
        try:
            os.remove(self.pfad)
        except OSError:
            pass


def _lesen(p):
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return []


def _speichern(p, liste):
    with open(p + ".neu", "w", encoding="utf-8") as f:
        json.dump(liste, f, ensure_ascii=False, indent=1)
    os.replace(p + ".neu", p)


def log(mandant, meldung, ereignis="notiz", warten_s=30, **felder):
    """One receipt-log line. The log lock is held for a whole copy run by the backend, so wait for it (up to warten_s)
    instead of failing at once; still locked -> belegprotokoll.ProtokollGesperrt."""
    ende = time.time() + warten_s
    while True:
        try:
            return bp.schreiben(bp.pfad_fuer(mandant), ereignis=ereignis, werkzeug=WERKZEUG, mandant=mandant, meldung=meldung, **felder)
        except bp.ProtokollGesperrt:
            if time.time() > ende:
                raise
            time.sleep(0.2)


def _log(mandant, kid, meldung, ereignis="notiz", **extra):
    log(mandant, meldung, ereignis=ereignis, lauf=kid, mcp_auftrag=kid, **extra)


def anlegen(mandant, client, art, body, titel, details):
    """New request, status blocked. details: the complete text the human sees before releasing (all names, exact paths, full text).
    Returns the entry. Called by the MCP server. Raises ValueError when the client already has too many open requests."""
    if art not in AUSFUEHRUNG:
        raise ValueError("unknown kind: %s" % art)
    kid = PREFIX + secrets.token_hex(5)
    e = {"id": kid, "art": art, "body": body, "client": client, "titel": titel, "details": details, "zeit": bp.jetzt(), "status": "blocked"}
    p = _pfad(mandant)
    with _Sperre(p):
        alle = _lesen(p)
        offen = sum(1 for x in alle if x.get("client") == client and x["status"] == "blocked")
        if offen >= MAX_OFFEN_JE_CLIENT:
            raise ValueError("%d requests from this client are still waiting for release; wait until the human has handled them" % offen)
        _log(mandant, kid, "MCP request from %s created (blocked, waits for human release in the Trove tab): %s" % (client, titel),
             mcp_client=client, anfrage={"art": art, **body})  # log first: no queue entry without its log line
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
    with _Sperre(p):
        alle = _lesen(p)
        for e in alle:
            if e["id"] == kid:
                if erwartet and e["status"] not in erwartet:
                    return None
                e.update(felder)
                _speichern(p, alle)
                return e
    return None


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


def freigeben(mandant, kid, api, warten=False):
    """Human release (only called from the tab's release route). Runs the request in a background thread through the backend function."""
    e = _setzen(mandant, kid, ("blocked",), status="running", freigegeben=bp.jetzt(), freigegeben_ts=time.time())
    if e is None:
        cur = holen(mandant, kid)
        if cur is None:
            raise KeyError(kid)
        raise ValueError("request %s is %s, only a blocked request can be released" % (kid, cur["status"]))
    _log(mandant, kid, "MCP request from %s released by a human in the Trove tab: %s" % (e["client"], e["titel"]), mcp_client=e["client"])

    def lauf():
        fn = AUSFUEHRUNG[e["art"]][0]
        fn = getattr(api, fn) if isinstance(fn, str) else (lambda b, _f=fn: _f(api, b))
        body = dict(e["body"], mandant=mandant)
        if e["art"] in ("kopieren", "verschieben", "sync"):
            body["art"] = e["art"]
        try:
            r = fn(body)
            kurz = json.dumps({k: r.get(k) for k in ("lauf", "ergebnis", "verarbeitet", "neu", "fehler") if k in r}, ensure_ascii=False)[:600]
            fehler = (isinstance(r.get("fehler"), int) and r["fehler"] > 0) or any((x.get("fehler") or x.get("konflikt") or x.get("status") == "fehlt") for x in (r.get("ergebnis") or []) if isinstance(x, dict))
            status = "failed" if fehler else "done"
            ergebnis = ("done: " if status == "done" else "finished with conflicts/errors: ") + kurz
        except Exception as ex:  # HTTPException from the backend (e.g. folder switched to read-only meanwhile) and anything else
            status, ergebnis = "failed", "failed: " + str(getattr(ex, "detail", "") or ex or type(ex).__name__)[:600]
        try:
            _log(mandant, kid, "MCP request %s from %s %s" % (kid, e["client"], ergebnis), ereignis="notiz" if status == "done" else "fehler",
                 mcp_client=e["client"], warten_s=600)
        finally:
            _setzen(mandant, kid, None, status=status, ende=bp.jetzt(), ergebnis=ergebnis)

    if warten:
        lauf()
    else:
        threading.Thread(target=lauf, name="trove-mcp-" + kid, daemon=True).start()
    return {"ok": True, "id": kid, "status": "running"}


def ablehnen(mandant, kid):
    """Archive button in the tab, one atomic step: a blocked request is rejected, a finished one is hidden, a running one only
    when it has been running longer than LAUFZEIT_S (the run is presumed dead, e.g. the dashboard was closed)."""
    p = _pfad(mandant)
    with _Sperre(p):
        alle = _lesen(p)
        e = next((x for x in alle if x["id"] == kid), None)
        if e is None:
            raise KeyError(kid)
        vorher = e["status"]
        if vorher == "blocked":
            e.update(status="rejected", ende=bp.jetzt())
        elif vorher in ("done", "failed"):
            e["status"] = "archived"
        elif vorher == "running" and time.time() - float(e.get("freigegeben_ts") or 0) > LAUFZEIT_S:
            e.update(status="archived", ende=bp.jetzt(), ergebnis="abandoned: still running after %d s, presumed dead (no result recorded)" % LAUFZEIT_S)
        elif vorher == "running":
            raise ValueError("request %s is running right now; it can be archived when it has finished" % kid)
        else:
            raise ValueError("request %s is already %s" % (kid, vorher))
        _speichern(p, alle)
    if vorher == "blocked":
        _log(mandant, kid, "MCP request from %s rejected by a human in the Trove tab: %s" % (e["client"], e["titel"]), mcp_client=e["client"])
    elif vorher == "running":
        _log(mandant, kid, "MCP request from %s archived by a human after it hung in 'running': %s" % (e["client"], e["titel"]), ereignis="fehler", mcp_client=e["client"])
    return {"ok": True}
