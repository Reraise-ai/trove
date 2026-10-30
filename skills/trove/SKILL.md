---
name: trove
description: Work with the user's files through Trove's MCP server — browse sources, preview and search files, read receipts, and request copies, moves, syncs or "Into knowledge" runs that a human releases in the Trove tab. Use when the user asks about files, receipts, invoices or folders managed in Trove, or wants files copied, moved, synced or turned into knowledge cards.
---

# Trove

Trove is a local file desk ("cleanup desk") that runs on the user's own machine. The `trove` MCP server of this plugin gives you the same
desk the user and their Hermes agents see. Nothing leaves the machine; every action is written to Trove's tamper-evident receipt log
with your client name.

## Two kinds of tools

**Read, immediately** (no approval):

| Tool | What it returns |
| --- | --- |
| `status` | the active client ("Mandant"), data folder, open requests |
| `list_sources` | the registered source folders with id, name, path and whether writable |
| `list_folder(source, path="", limit=500)` | entries of a folder: name, folder flag, size, mtime, sha256 when known |
| `preview(source, path)` | text/preview of one file |
| `search(query, limit=30)` | search across register, receipt log and knowledge |
| `file_trail(file)` | the receipt-log trail of one file (by sha256 or path) |
| `read_receipt(source, path, force_ocr=False)` | receipt fields (date, amounts, currency, number) and the check result; writes only a markdown twin into Trove's own data folder |
| `check_changes(source, path="")` | what changed in a folder since the last snapshot |
| `task_status(task_id)` | state of one request |
| `list_tasks(limit=30)` | all requests, every client's |

**Change, only as a request** — these never touch a file themselves. Each one creates a **blocked request** in the Trove tab.
A human reads the exact folders, names and your reason there and clicks **Release** (runs it) or **Archive** (rejects it).
There is no MCP tool that releases a request.

| Tool | Request |
| --- | --- |
| `request_copy(from_source, to_source, names, from_path="", to_path="", reason="")` | verified copy (sha256 read-back) |
| `request_move(from_source, to_source, names, from_path="", to_path="", reason="")` | copy, verify, then delete the original |
| `request_sync(from_source, to_source, from_path="", to_path="", reason="")` | one-way sync of missing files |
| `request_into_knowledge(source, path="", collection="", visibility="privat", cards=False, files=None, reason="")` | one markdown twin per file, optional knowledge cards |
| `request_rename(source, name, new_name, path="", reason="")` | rename in place (only in writable folders) |
| `request_new_folder(source, name, path="", reason="")` | new empty folder (only in writable folders) |
| `request_agent_task(title, text="")` | work for a Hermes agent, waits on the Hermes Kanban board |

## How to work

1. Start with `list_sources`, then `list_folder` — use the source **id** from `list_sources`, not the name.
2. Paths are relative to the source folder. Absolute paths, `..` and anything through a link are refused.
3. Before requesting a change, tell the user what you are about to request and why; put that reason into `reason`.
   Keep names exact (the entry names from `list_folder`), at most 200 per request.
4. After a request, you get a task id. Tell the user the request is waiting in the Trove tab for release, then poll `task_status`
   (every ~10 s, give up after a few minutes and say so). Do not re-submit the same request while one is open.
5. Read-only sources stay read-only: only the user can mark a folder writable, in the tab. If a request is refused, say why and stop.

## Setup problems

If the server does not start, the plugin's **Python command** setting must point to a Python 3.10+ that has `mcp`, `fastapi` and
`pypdfium2` installed (`python3 -m pip install mcp fastapi pypdfium2`). Trove's data folder is `~/Trove` unless `TROVE_DATEN`
or `trove.json` says otherwise; see the README.
