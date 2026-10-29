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
LESEN = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
LESEN_MIT_SPUR = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)  # writes only into Trove's own data
ANTRAG = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)  # only queues a blocked request
HINWEIS = ("Waits for a human: the request is BLOCKED in the Trove tab and runs only after someone clicks Release there. "
           "Poll task_status with the task_id. You cannot release it yourself.")
ANLEITUNG = ("Trove is a file desk shared by a human and agents. Sources are local folders with an id; paths are relative to a source. "
             "Reading tools work immediately. Every change (copy, move, sync, into knowledge, agent task) is only a REQUEST: it is "
             "created blocked and runs after a human releases it in the Trove tab. Folders are read-only unless the human marked them "
             "writable. Nothing is ever overwritten.")


def _text(obj):
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)


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
            raise ToolError(str(ex.detail)) from None

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
        log(ctx, "request refused before queueing (%s): %s" % (was, grund))
        raise ToolError(grund)

    def antrag(ctx, art, body, titel, detail):
        try:
            e = mq.anlegen(mandant, wer(ctx), art, body, titel, detail)
        except bp.ProtokollGesperrt:
            raise ToolError("Trove is busy with a long run that holds the receipt log; nothing was queued, try again in a minute") from None
        return _text({"task_id": e["id"], "status": e["status"], "what": titel, "note": HINWEIS})

    def datei_aktion(ctx, art, from_source, from_path, to_source, to_path, names, reason):
        namen = [n for n in (names or []) if n]
        try:
            rufen(api._namen_pruefen, namen)
            qv, qn = quelle(from_source), quelle(to_source)
            von = rufen(api._sicher, qv["pfad"], from_path)
            rufen(api._sicher, qn["pfad"], to_path)
        except (ValueError, ToolError) as ex:
            return ablehnen(ctx, art, str(getattr(ex, "detail", ex)))
        if qn.get("nur_lesen", True):
            return ablehnen(ctx, art, "The target folder '%s' is read-only. Only the human can mark it writable in the Trove tab." % qn["name"])
        if art == "verschieben" and qv.get("nur_lesen", True):
            return ablehnen(ctx, art, "Move deletes the originals, so the source folder '%s' must be writable too. Copy works from a read-only source." % qv["name"])
        if art != "sync" and not namen:
            return ablehnen(ctx, art, "names: give the entry names to %s" % mq.AUSFUEHRUNG[art][1])
        fehlt = [n for n in namen if not os.path.lexists(os.path.join(von, n))]
        if fehlt:
            return ablehnen(ctx, art, "not found in %s: %s" % (ort(qv, from_path), ", ".join(fehlt[:10])))
        body = {"von": {"quelle": qv["id"], "pfad": from_path}, "nach": {"quelle": qn["id"], "pfad": to_path}, "namen": namen}
        was = "%d item(s)" % len(namen) if namen else "missing files"
        titel = "%s %s from %s to %s" % (mq.AUSFUEHRUNG[art][1], was, ort(qv, from_path), ort(qn, to_path))
        detail = ("items: " + ", ".join(namen[:20]) + (" ..." if len(namen) > 20 else "") if namen else "whole folder, missing files only") + \
                 ("; reason given by the agent: " + reason.strip() if (reason or "").strip() else "")
        return antrag(ctx, art, body, titel, detail)

    @server.tool(annotations=ANTRAG, description='Request a verified copy of entries (files or folders) from one source folder into another. Never overwrites. The target folder must be writable. ' + HINWEIS)
    def request_copy(from_source: str, to_source: str, names: list[str], from_path: str = "", to_path: str = "", reason: str = "",
                     ctx: Context = None) -> str:
        return datei_aktion(ctx, "kopieren", from_source, from_path, to_source, to_path, names, reason)

    @server.tool(annotations=ANTRAG, description='Request a move: copy, verify by reading back, then delete the original. Source and target must be writable. ' + HINWEIS)
    def request_move(from_source: str, to_source: str, names: list[str], from_path: str = "", to_path: str = "", reason: str = "",
                     ctx: Context = None) -> str:
        return datei_aktion(ctx, "verschieben", from_source, from_path, to_source, to_path, names, reason)

    @server.tool(annotations=ANTRAG, description='Request a one-way sync: copy files missing in the target folder from the source folder. Never overwrites. ' + HINWEIS)
    def request_sync(from_source: str, to_source: str, from_path: str = "", to_path: str = "", reason: str = "", ctx: Context = None) -> str:
        return datei_aktion(ctx, "sync", from_source, from_path, to_source, to_path, [], reason)

    @server.tool(annotations=ANTRAG, description='Request "Into knowledge" for a folder: one markdown twin per file, optional knowledge cards (cards=true), search index. visibility: privat (own use) or oeffentlich (card limited to the public allow-list). files: optional relative paths. ' + HINWEIS)
    def request_into_knowledge(source: str, path: str = "", collection: str = "", visibility: str = "privat", cards: bool = False,
                               files: list[str] | None = None, reason: str = "", ctx: Context = None) -> str:
        body = {"quelle": source, "pfad": path, "sammlung": collection, "sichtbarkeit": visibility, "karten": bool(cards), "dateien": list(files or [])}
        try:
            q = quelle(source)
            wurzel = rufen(api._sicher, q["pfad"], path)
            if not os.path.isdir(wurzel):
                raise ValueError("folder not found: " + ort(q, path))
            if visibility not in ("privat", "oeffentlich"):
                raise ValueError("visibility: privat | oeffentlich")
            body["sammlung"] = rufen(api._sammlung, mandant, body)
            for rel in body["dateien"]:
                if not os.path.isfile(rufen(api._sicher, q["pfad"], rel)):
                    raise ValueError("no such file: " + rel)
        except (ValueError, ToolError) as ex:
            return ablehnen(ctx, "wissen", str(getattr(ex, "detail", ex)))
        titel = "into knowledge: %s (collection %s, %s%s)" % (ort(q, path), body["sammlung"], visibility, ", cards" if cards else "")
        detail = ("files: " + ", ".join(body["dateien"][:20]) if body["dateien"] else "whole folder") + \
                 ("; reason given by the agent: " + reason.strip() if (reason or "").strip() else "")
        return antrag(ctx, "wissen", body, titel, detail)

    @server.tool(annotations=ANTRAG)
    def request_agent_task(title: str, text: str = "", ctx: Context = None) -> str:
        """Ask for work by a Hermes agent: creates a BLOCKED card on the Hermes kanban board (needs the hermes command line).
        A human releases it in the Trove tab; then a Hermes agent works on it. Poll task_status with the task_id."""
        e = rufen(api.auftrag_anlegen, {"mandant": mandant, "titel": title, "text": text + "\n\n(requested over MCP by %s)" % wer(ctx), "kontext": {"mcp_client": wer(ctx)}})
        log(ctx, "agent task created (blocked): %s" % title, lauf=e.get("id") or "")
        return _text({"task_id": e.get("id"), "status": e.get("status"), "note": HINWEIS})

    return server


def main(argv=None):
    ap = argparse.ArgumentParser(description="Trove MCP server (stdio by default)")
    ap.add_argument("--mandant", help="Trove client (default: trove.json / TROVE_MANDANT)")
    ap.add_argument("--client-name", help="name of the MCP client for the receipt log (default: what the client reports)")
    ap.add_argument("--http", type=int, metavar="PORT", help="serve streamable HTTP on 127.0.0.1:PORT instead of stdio")
    a = ap.parse_args(argv)
    server = bauen(a.mandant, a.client_name)
    if a.http:
        if MCP2:
            server.run("streamable-http", host="127.0.0.1", port=a.http)
        else:
            server.settings.host, server.settings.port = "127.0.0.1", a.http
            server.run("streamable-http")
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
