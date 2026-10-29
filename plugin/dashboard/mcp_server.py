#!/usr/bin/env python3
"""Trove as an MCP server: other agent systems (Claude Desktop, Claude Code, Cursor, any MCP client) use the same desk.

    python mcp_server.py                 # stdio (what MCP clients start)
    python mcp_server.py --http 8765     # optional: streamable HTTP, bound to 127.0.0.1 only
    python mcp_server.py --mandant haus  # other Trove client than the configured one

Reading is free: sources, folder listings, previews, search, the receipt-log trail, receipt reading, change check.
Writing is never direct: copy / move / sync / into knowledge become a BLOCKED request in Trove's approval queue (mcp_queue.py).
It shows up in the Trove tab and runs only after a human clicks "Release" there. There is no MCP tool that releases or approves.
The client gets a task id and polls task_status. Every request, refusal, release and result goes to the receipt log with the
MCP client's name. The server wraps the plugin backend (plugin_api.py) -- the same functions and checks as the tab.
Needs: the Python packages `mcp` and `fastapi` (Hermes ships both when installed with its MCP support).
No credentials, no network listener except the optional HTTP transport on 127.0.0.1, no self-update.
"""
import argparse
import json
import os
import re
import sys

HIER = os.path.dirname(os.path.abspath(__file__))
if HIER not in sys.path:
    sys.path.insert(0, HIER)

try:
    import mcp  # noqa: F401
    import fastapi  # noqa: F401
except ImportError as _ex:
    sys.exit("Trove MCP server: the Python package '%s' is missing. Install it with: python3 -m pip install mcp fastapi" % (_ex.name or "mcp"))

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _Server, Context
    from mcp.server.mcpserver.exceptions import ToolError
    MCP2 = True
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server, Context
    from mcp.server.fastmcp.exceptions import ToolError
    MCP2 = False
from mcp.types import ToolAnnotations

NAME = "trove"
MAX_NAMEN = 200  # entry names / files per request
MAX_PFAD = 1024
MAX_GRUND = 500
MAX_TITEL = 200
MAX_TEXT = 4000
LESEN = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
LESEN_MIT_SPUR = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)  # writes only into Trove's own data
ANTRAG = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)  # only queues a blocked request
HINWEIS = ("Waits for a human: the request is BLOCKED in the Trove tab and runs only after someone clicks Release there. "
           "Poll task_status with the task_id. You cannot release it yourself.")
ANLEITUNG = ("Trove is a file desk shared by a human and agents. Sources are local folders with an id; paths are relative to a source. "
             "Reading tools work immediately. Every change (copy, move, sync, into knowledge, agent task) is only a REQUEST: it is "
             "created blocked and runs after a human releases it in the Trove tab. Folders are read-only unless the human marked them "
             "writable. Nothing is ever overwritten.")


def _sauber(detail, code, api):
    """Error text for the client: 4xx messages as they are; for 5xx and anything with a traceback only the last line,
    without file system paths."""
    text = api._aussen(detail) if hasattr(api, "_aussen") else str(detail)  # secrets masked, known paths stripped
    if code < 500 and "Traceback" not in text:
        return text
    zeilen = [z.strip() for z in text.splitlines() if z.strip()]
    letzte = zeilen[-1] if zeilen else "internal error"
    letzte = api._ohne_pfade(letzte)
    letzte = re.sub(r"(?:[A-Za-z]:)?(?:[\\/][^\s'\"\\/:<>|]+){2,}", "<path>", letzte)
    return "Trove could not do this: " + letzte[:300]


def _geschwaerzt(text):
    z = sys.modules.get("zugang")  # optional integrations loaded in this process: mask every secret value they read
    return z.schwaerzen(text) if z is not None else text


def _text(obj):
    z = sys.modules.get("zugang")
    if z is not None:
        obj = z.schwaerzen_tief(json.loads(json.dumps(obj, ensure_ascii=False, default=str)))
    return _geschwaerzt(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def bauen(mandant=None, client=None):
    """Build the server. mandant: Trove client (default from trove.json / TROVE_MANDANT). client: fixed MCP client name for the log."""
    if mandant:
        os.environ["TROVE_MANDANT"] = mandant
    import plugin_api as api  # noqa: E402 (after TROVE_MANDANT)
    import belegprotokoll as bp
    import mcp_queue as mq
    from fastapi import HTTPException

    mandant = api._mandant_pruefen(mandant or api.MANDANT)
    fest = client or os.environ.get("TROVE_MCP_CLIENT")
    server = _Server(NAME, instructions=ANLEITUNG)

    def wer(ctx):
        name = fest
        if not name:
            try:
                cp = ctx.session.client_params
                info = getattr(cp, "client_info", None) or getattr(cp, "clientInfo", None)
                name = info.name
            except Exception:
                name = None
        name = re.sub(r"[^A-Za-z0-9 ._-]", "", name or "")[:60].strip()
        return name or "mcp-client"

    def rufen(fn, *a, **k):
        try:
            return fn(*a, **k)
        except HTTPException as ex:
            raise ToolError(_sauber(ex.detail, ex.status_code, api)) from None
        except ToolError:
            raise
        except Exception as ex:  # no traceback, no system paths to the client; the full error goes to the server log (stderr)
            print("trove-mcp: %s: %r" % (getattr(fn, "__name__", "?"), ex), file=sys.stderr)
            raise ToolError("internal error in Trove (%s); details are in the MCP server log" % type(ex).__name__) from None

    def log(ctx, meldung, **extra):
        c = wer(ctx)
        try:
            mq.log(mandant, "MCP %s: %s" % (c, meldung), mcp_client=c, **extra)
        except bp.ProtokollGesperrt:
            raise ToolError("Trove is busy with a long run that holds the receipt log; try again in a minute") from None

    def quelle(qid):
        return rufen(api._quelle, mandant, qid)

    def ort(q, pfad):
        return q["name"] + ("/" + pfad.strip("/") if (pfad or "").strip("/") else "")

    # ------------------------------------------------------------------ read
    @server.tool(annotations=LESEN)
    def status(ctx: Context) -> str:
        """Trove version, the Trove client (mandant) this server works for, and how writes are approved."""
        s = api.status()
        return _text({"version": s["version"], "mandant": mandant, "mcp_client": wer(ctx), "data_root": s["datenwurzel"],
                      "writes": "every change is a blocked request; a human releases it in the Trove tab"})

    @server.tool(annotations=LESEN)
    def list_sources() -> str:
        """The folders on the desk: id, name, absolute path and whether the human marked it writable (default: read-only)."""
        return _text([{"id": q["id"], "name": q["name"], "path": q.get("pfad"), "writable": not q.get("nur_lesen", True)} for q in api._quellen(mandant)])

    @server.tool(annotations=LESEN)
    def list_folder(source: str, path: str = "", limit: int = 500) -> str:
        """Entries of a folder inside a source (path relative to the source root). sha256/duplicate come from the register if indexed."""
        r = rufen(api.liste, quelle=source, pfad=path, mandant=mandant)
        e = r["eintraege"]
        n = max(1, min(int(limit), 5000))
        return _text({"source": source, "path": path, "indexed": r["indexiert"], "total": len(e), "truncated": len(e) > n,
                      "entries": [{"name": x["name"], "folder": x["ordner"], "size": x["groesse"], "mtime": x["mtime"], "sha256": x.get("sha256"),
                                   "found_in_places": x.get("fundorte"), "duplicate": x.get("dublette")} for x in e[:n]]})

    @server.tool(annotations=LESEN)
    def preview(source: str, path: str) -> str:
        """Metadata of a file and, for text/markdown files, the first 32 KB of text. Images and PDFs: metadata only (use read_receipt for PDF text)."""
        r = rufen(api.vorschau, quelle=source, pfad=path, mandant=mandant)
        out = {k: v for k, v in r.items() if k != "data" and not (isinstance(v, str) and v.startswith("data:"))}
        out["kind"] = out.pop("art", None)
        return _text(out)

    @server.tool(annotations=LESEN)
    def search(query: str, limit: int = 30) -> str:
        """Search the file register and the receipt log (file names, paths, log messages, sha256 prefix)."""
        return _text(rufen(api.suche, q=query, mandant=mandant, n=limit))

    @server.tool(annotations=LESEN)
    def file_trail(file: str) -> str:
        """Everything the receipt log knows about one file (absolute path): where it came from, where copies went, same content elsewhere."""
        return _text(rufen(api.spur, datei=file, mandant=mandant))

    @server.tool(annotations=LESEN_MIT_SPUR)
    def read_receipt(source: str, path: str, force_ocr: bool = False, ctx: Context = None) -> str:
        """Read a receipt/invoice (e-invoice > PDF text > local OCR), extract fields and check them with fixed rules.
        Writes only into Trove's own data folder (a markdown twin and a log line); the original is not touched."""
        body = {"mandant": mandant, "quelle": source, "pfad": path, "erzwinge_ocr": force_ocr}
        r = rufen(api.lesen, body)
        log(ctx, "read_receipt %s" % ort(quelle(source), path), quelle=r.get("zwilling", ""), sha256=r.get("sha256", ""))
        return _text(r)

    @server.tool(annotations=LESEN_MIT_SPUR)
    def check_changes(source: str, path: str = "", ctx: Context = None) -> str:
        """Compare a folder with the last check: new, gone, moved, changed outside Trove. Stores the snapshot in Trove's data folder."""
        r = rufen(api.aenderungen_lauf, {"mandant": mandant, "quelle": source, "pfad": path})
        log(ctx, "check_changes %s" % ort(quelle(source), path), lauf=r.get("lauf", ""))
        return _text(r)

    @server.tool(annotations=LESEN)
    def task_status(task_id: str) -> str:
        """Status of a request created by this server (blocked / running / done / failed / rejected) or of a Hermes task card."""
        if mq.ist_mcp(task_id):
            e = mq.holen(mandant, task_id)
            if e is None:
                raise ToolError("unknown task id")
            return _text({k: e.get(k) for k in ("id", "art", "status", "titel", "client", "zeit", "freigegeben", "ende", "ergebnis")})
        for a in api._auftraege_lesen(mandant):
            if a.get("id") == task_id:
                return _text({k: a.get(k) for k in ("id", "titel", "status", "zeit", "zusammenfassung", "ergebnis_kommentar")})
        raise ToolError("unknown task id")

    @server.tool(annotations=LESEN)
    def list_tasks(limit: int = 30) -> str:
        """The latest requests in Trove's MCP approval queue (all MCP clients), newest first."""
        alle = list(reversed(mq.liste(mandant)))[:max(1, min(int(limit), 200))]
        return _text([{k: e.get(k) for k in ("id", "art", "status", "titel", "client", "zeit", "ergebnis")} for e in alle])

    # ------------------------------------------------------------------ write requests (never executed here)
    def ablehnen(ctx, was, grund):
        grund = api._aussen(grund) if hasattr(api, "_aussen") else grund
        log(ctx, "request refused before queueing (%s): %s" % (was, grund))
        raise ToolError(grund)

    def antrag(ctx, art, body, titel, details):
        try:
            e = mq.anlegen(mandant, wer(ctx), art, body, titel, details)
        except bp.ProtokollGesperrt:
            raise ToolError("Trove is busy with a long run that holds the receipt log; nothing was queued, try again in a minute") from None
        except ValueError as ex:
            return ablehnen(ctx, art, str(ex))
        return _text({"task_id": e["id"], "status": e["status"], "what": titel, "note": HINWEIS})

    def text_pruefen(text, feld, maximum):
        if text is not None and not isinstance(text, str):
            raise ValueError("%s: text" % feld)
        if len(text or "") > maximum:
            raise ValueError("%s: at most %d characters" % (feld, maximum))
        return (text or "").strip()

    def pfad_pruefen(pfad, feld):
        """Relative path inside a source, shown to the human exactly as it will be used: '..', absolute paths, NUL are refused."""
        pfad = text_pruefen(pfad, feld, MAX_PFAD)
        teile = pfad.replace("\\", "/").split("/")
        if ".." in teile or "\0" in pfad or os.path.isabs(pfad) or os.path.splitdrive(pfad)[0]:
            raise ValueError("%s: a path inside the source folder, without '..' and not absolute" % feld)
        return "/".join(t for t in teile if t not in ("", "."))

    def grund_zeile(reason):
        return "Reason given by the agent: " + reason if reason else "Reason given by the agent: (none)"

    def datei_aktion(ctx, art, from_source, from_path, to_source, to_path, names, reason):
        label = mq.AUSFUEHRUNG[art][1]
        try:
            if names is not None and (not isinstance(names, list) or len(names) > MAX_NAMEN):
                raise ValueError("names: a list of at most %d entry names per request" % MAX_NAMEN)
            namen = [n for n in (names or []) if n]
            rufen(api._namen_pruefen, namen)
            from_path, to_path = pfad_pruefen(from_path, "from_path"), pfad_pruefen(to_path, "to_path")
            reason = text_pruefen(reason, "reason", MAX_GRUND)
            qv, qn = quelle(from_source), quelle(to_source)
            von, nach = rufen(api._sicher, qv["pfad"], from_path), rufen(api._sicher, qn["pfad"], to_path)
            if not os.path.isdir(von):
                raise ValueError("folder not found: " + ort(qv, from_path))
            if qn.get("nur_lesen", True):
                raise ValueError("The target folder '%s' is read-only. Only the human can mark it writable in the Trove tab." % qn["name"])
            if art == "verschieben" and qv.get("nur_lesen", True):
                raise ValueError("Move deletes the originals, so the source folder '%s' must be writable too. Copy works from a read-only source." % qv["name"])
            if art != "sync" and not namen:
                raise ValueError("names: give the entry names to %s" % label)
            fehlt = [n for n in namen if not os.path.lexists(os.path.join(von, n))]
            if fehlt:
                raise ValueError("not found in %s: %s" % (ort(qv, from_path), ", ".join(fehlt[:10])))
            raus = [n for n in namen if not api._unter_real(os.path.join(von, n), von)]
            if raus:
                raise ValueError("these entries are links that leave the source folder: %s" % ", ".join(raus[:10]))
            links = [n for n in namen if api.ist_verknuepfung(os.path.join(von, n))]
            if links:
                raise ValueError("these entries are links; Trove does not copy, move or sync links: %s" % ", ".join(links[:10]))
            for wurzel, ordner, feld in ((qv["pfad"], von, "from_path"), (qn["pfad"], nach, "to_path")):
                if api._ueber_link(wurzel, ordner):
                    raise ValueError("%s goes through a link; Trove does not follow links" % feld)
            if art in ("verschieben", "sync") and not namen and api._unter_real(nach, von):
                raise ValueError("the target folder is inside the source folder")
            for n in (namen or [None]):
                grund = api._selbst_ziel(os.path.join(von, n) if n else von, os.path.join(nach, n) if n else nach, nach)
                if grund:
                    raise ValueError(grund)
        except (ValueError, ToolError) as ex:
            return ablehnen(ctx, art, str(ex))
        body = {"von": {"quelle": qv["id"], "pfad": from_path}, "nach": {"quelle": qn["id"], "pfad": to_path}, "namen": namen}
        was = "%d item(s)" % len(namen) if namen else "missing files"
        titel = "%s %s from %s to %s" % (label, was, ort(qv, from_path), ort(qn, to_path))
        details = "\n".join([
            "Action: %s%s" % (label, " (copies, verifies, then DELETES the originals)" if art == "verschieben" else " (never overwrites)"),
            "From folder: %s" % von,
            "Into folder (exact): %s" % nach,
            ("Items (%d): %s" % (len(namen), ", ".join(namen))) if namen else "Items: every file of the source folder that is missing in the target",
            grund_zeile(reason)])
        return antrag(ctx, art, body, titel, details)

    @server.tool(annotations=ANTRAG, description="Request a verified copy of entries (files or folders) from one source folder into another. "
                 "Never overwrites. The target folder must be writable. " + HINWEIS)
    def request_copy(from_source: str, to_source: str, names: list[str], from_path: str = "", to_path: str = "", reason: str = "",
                     ctx: Context = None) -> str:
        return datei_aktion(ctx, "kopieren", from_source, from_path, to_source, to_path, names, reason)

    @server.tool(annotations=ANTRAG, description="Request a move: copy, verify by reading back, then delete the original. "
                 "Source and target must be writable. " + HINWEIS)
    def request_move(from_source: str, to_source: str, names: list[str], from_path: str = "", to_path: str = "", reason: str = "",
                     ctx: Context = None) -> str:
        return datei_aktion(ctx, "verschieben", from_source, from_path, to_source, to_path, names, reason)

    @server.tool(annotations=ANTRAG, description="Request a one-way sync: copy files missing in the target folder from the source folder. "
                 "Never overwrites. " + HINWEIS)
    def request_sync(from_source: str, to_source: str, from_path: str = "", to_path: str = "", reason: str = "", ctx: Context = None) -> str:
        return datei_aktion(ctx, "sync", from_source, from_path, to_source, to_path, [], reason)

    @server.tool(annotations=ANTRAG, description='Request "Into knowledge" for a folder: one markdown twin per file, optional knowledge '
                 "cards (cards=true), search index. visibility: privat (own use) or oeffentlich (card limited to the public allow-list). "
                 "files: optional relative paths. " + HINWEIS)
    def request_into_knowledge(source: str, path: str = "", collection: str = "", visibility: str = "privat", cards: bool = False,
                               files: list[str] | None = None, reason: str = "", ctx: Context = None) -> str:
        try:
            if files is not None and (not isinstance(files, list) or len(files) > MAX_NAMEN):
                raise ValueError("files: a list of at most %d relative paths per request" % MAX_NAMEN)
            path = pfad_pruefen(path, "path")
            dateien = [pfad_pruefen(f, "files") for f in (files or [])]
            reason = text_pruefen(reason, "reason", MAX_GRUND)
            q = quelle(source)
            wurzel = rufen(api._sicher, q["pfad"], path)
            if not os.path.isdir(wurzel):
                raise ValueError("folder not found: " + ort(q, path))
            if visibility not in ("privat", "oeffentlich"):
                raise ValueError("visibility: privat | oeffentlich")
            body = {"quelle": q["id"], "pfad": path, "sammlung": text_pruefen(collection, "collection", 60), "sichtbarkeit": visibility,
                    "karten": bool(cards), "dateien": dateien}
            body["sammlung"] = rufen(api._sammlung, mandant, body)
            for rel in dateien:
                voll = rufen(api._sicher, q["pfad"], rel)
                if not os.path.isfile(voll) or not api._unter_real(voll, wurzel):
                    raise ValueError("no such file in the chosen folder: " + rel)
        except (ValueError, ToolError) as ex:
            return ablehnen(ctx, "wissen", str(ex))
        titel = "into knowledge: %s (collection %s, %s%s)" % (ort(q, path), body["sammlung"], visibility, ", cards" if cards else "")
        details = "\n".join([
            "Action: into knowledge (markdown twins in Trove's knowledge folder%s; originals are not changed)" % (", knowledge cards in the wiki" if cards else ""),
            "Folder: %s" % wurzel, "Collection: %s, visibility: %s" % (body["sammlung"], visibility),
            ("Files (%d): %s" % (len(dateien), ", ".join(dateien))) if dateien else "Files: every file in the folder",
            grund_zeile(reason)])
        return antrag(ctx, "wissen", body, titel, details)

    if hasattr(api, "umbenennen"):
        @server.tool(annotations=ANTRAG, description="Request renaming one file or folder in place (same folder). The source must be writable; "
                     "an existing entry is never replaced. " + HINWEIS)
        def request_rename(source: str, name: str, new_name: str, path: str = "", reason: str = "", ctx: Context = None) -> str:
            try:
                path = pfad_pruefen(path, "path")
                reason = text_pruefen(reason, "reason", MAX_GRUND)
                q = quelle(source)
                if q.get("nur_lesen", True):
                    raise ValueError("The folder '%s' is read-only. Only the human can mark it writable in the Trove tab." % q["name"])
                rufen(api._namen_pruefen, [name])
                if hasattr(api, "_eintragsname_pruefen"):
                    rufen(api._eintragsname_pruefen, new_name, "new_name")
                else:
                    rufen(api._namen_pruefen, [new_name])
                ordner = rufen(api._sicher, q["pfad"], path)
                if api._ueber_link(q["pfad"], ordner):
                    raise ValueError("path goes through a link; Trove does not follow links")
                if not name or not os.path.lexists(os.path.join(ordner, name)):
                    raise ValueError("not found in %s: %s" % (ort(q, path), name))
                if os.path.lexists(os.path.join(ordner, new_name)) and name.casefold() != (new_name or "").casefold():
                    raise ValueError("an entry named %r already exists in %s" % (new_name, ort(q, path)))
            except (ValueError, ToolError) as ex:
                return ablehnen(ctx, "umbenennen", str(ex))
            body = {"quelle": q["id"], "pfad": path, "name": name, "neu": new_name}
            details = "\n".join(["Action: rename in place", "Folder: %s" % ordner, "Old name: %s" % name, "New name: %s" % new_name, grund_zeile(reason)])
            return antrag(ctx, "umbenennen", body, "rename %s -> %s in %s" % (name, new_name, ort(q, path)), details)

    if hasattr(api, "ordner_neu"):
        @server.tool(annotations=ANTRAG, description="Request a new, empty folder inside a writable source. " + HINWEIS)
        def request_new_folder(source: str, name: str, path: str = "", reason: str = "", ctx: Context = None) -> str:
            try:
                path = pfad_pruefen(path, "path")
                reason = text_pruefen(reason, "reason", MAX_GRUND)
                q = quelle(source)
                if q.get("nur_lesen", True):
                    raise ValueError("The folder '%s' is read-only. Only the human can mark it writable in the Trove tab." % q["name"])
                if hasattr(api, "_eintragsname_pruefen"):
                    rufen(api._eintragsname_pruefen, name, "name")
                else:
                    rufen(api._namen_pruefen, [name])
                ordner = rufen(api._sicher, q["pfad"], path)
                if not os.path.isdir(ordner):
                    raise ValueError("folder not found: " + ort(q, path))
                if api._ueber_link(q["pfad"], ordner):
                    raise ValueError("path goes through a link; Trove does not follow links")
                if os.path.lexists(os.path.join(ordner, name)):
                    raise ValueError("an entry named %r already exists in %s" % (name, ort(q, path)))
            except (ValueError, ToolError) as ex:
                return ablehnen(ctx, "ordner_neu", str(ex))
            body = {"quelle": q["id"], "pfad": path, "name": name}
            details = "\n".join(["Action: new empty folder", "Inside: %s" % ordner, "Name: %s" % name, grund_zeile(reason)])
            return antrag(ctx, "ordner_neu", body, "new folder %s in %s" % (name, ort(q, path)), details)

    @server.tool(annotations=ANTRAG, description="Ask for work by a Hermes agent (needs the hermes command line). The full text waits as a "
                 "BLOCKED request in the Trove tab; when the human releases it, a Hermes kanban card is created and started. " + HINWEIS)
    def request_agent_task(title: str, text: str = "", ctx: Context = None) -> str:
        try:
            title = text_pruefen(title, "title", MAX_TITEL)
            text = text_pruefen(text, "text", MAX_TEXT)
            if not title:
                raise ValueError("title is missing")
            if api._hermes_exe() is None:
                raise ValueError("the hermes command line is not available on this machine")
        except (ValueError, ToolError) as ex:
            return ablehnen(ctx, "agent_auftrag", str(ex))
        c = wer(ctx)
        body = {"titel": title, "text": text + "\n\n(requested over MCP by %s, released by a human in the Trove tab)" % c, "kontext": {"mcp_client": c}}
        details = "\n".join(["Action: task for a Hermes agent. After release a Hermes agent works on it with its own permissions.",
                             "Title: %s" % title, "Full text:", text or "(empty)"])
        return antrag(ctx, "agent_auftrag", body, "agent task: " + title, details)

    return server


def main(argv=None):
    ap = argparse.ArgumentParser(description="Trove MCP server (stdio by default)")
    ap.add_argument("--mandant", help="Trove client (default: trove.json / TROVE_MANDANT)")
    ap.add_argument("--client-name", help="name of the MCP client for the receipt log (default: what the client reports)")
    ap.add_argument("--http", type=int, metavar="PORT", help="serve streamable HTTP on 127.0.0.1:PORT instead of stdio")
    a = ap.parse_args(argv)
    if a.http:
        schutz = _http_schutz(a.http)  # before anything else: refuse if this mcp version cannot enforce it
    server = bauen(a.mandant, a.client_name)
    if a.http:
        if MCP2:
            server.run("streamable-http", host="127.0.0.1", port=a.http, transport_security=schutz)
        else:
            server.settings.host, server.settings.port = "127.0.0.1", a.http
            server.settings.transport_security = schutz
            server.run("streamable-http")
    else:
        server.run("stdio")


def _http_schutz(port):
    """DNS-rebinding protection for the HTTP transport, set explicitly whatever the SDK default is: only Host 127.0.0.1:<port> /
    localhost:<port>, and no browser origin at all (MCP clients send none). Exits when the installed mcp cannot enforce it."""
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError:
        TransportSecuritySettings = None
    if TransportSecuritySettings is None or (not MCP2 and not _v1_hat_schutz()):
        sys.exit("Trove MCP server: this version of the 'mcp' package cannot enforce Host/Origin checks; --http is refused. "
                 "Use stdio (no --http) or upgrade: python3 -m pip install -U mcp")
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=["127.0.0.1:%d" % port, "localhost:%d" % port],
                                     allowed_origins=[])


def _v1_hat_schutz():
    try:
        from mcp.server.fastmcp.server import Settings
        return "transport_security" in Settings.model_fields
    except Exception:
        return False


if __name__ == "__main__":
    main()
