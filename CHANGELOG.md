# Changelog

All notable changes to Trove are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-11-23

### Changed
- **Agents steer Trove directly; the release step is a switch.** New setting `freigabe_pflicht` in `trove.json` (default **off**; shown with its source
  `freigabe_pflicht_quelle` in `GET /status`, `GET /auftraege` and the MCP tool `status`). `TROVE_FREIGABE_PFLICHT` can only switch it on, never off; unclear values
  and an unreadable `trove.json` count as on, with `freigabe_pflicht_warnung`. Off: a card created in the tab and a request over MCP start at once. On: both are created
  blocked and start only after **Release** in the Trove tab, as before. Folder rights (read-only unless marked writable) bind the file actions in both positions; an
  agent task runs a Hermes agent with Hermes' own permissions, and starts only if `TROVE_ARBEITSORDNER` (when set) lies in a writable folder. Every action is
  logged with its origin (`tab` or `mcp:<client>`) as a log field, not as an approval criterion. The environment can only make the switch stricter: an empty value in
  `trove.json` and a `TROVE_KONFIG` that points to a missing file count as on; `freigabe_pflicht_datei` shows which `trove.json` was read. The working folder is
  checked at every start, and Hermes gets the resolved path that was checked; Release in the tab checks the folder stored with the card (its `--workspace`), resolved again, not the current variable. A finished agent task counts against the per-client
  limit for as long as its agent can run (card runtime × attempts). A request started over MCP runs inside the MCP server
  process and ends with it. Texts in `mcp_server.py`, the skill file, both READMEs, `llms.txt`, `docs/PRIVACY.md` and `docs/TERMS.md` describe the new behaviour.
- **Web-page protection of the tab routes in both positions.** Create (`POST /auftrag`), release and archive accept only a JSON request from the local address (Host
  `127.0.0.1`, `localhost`, `[::1]`; more via `TROVE_DASHBOARD_HOSTS`, origins via `TROVE_DASHBOARD_ORIGIN`), for **every** card (before, plain Hermes cards were
  not checked). `Origin: null` is refused. The 403 names the host and the origin it saw. Hermes Desktop reaches the routes through its main process (Host `127.0.0.1:<port>`, no Origin header).
  Honest limit: this holds back MCP clients and foreign web pages, not programs on the same computer.
- Log texts say what happened ("created from the tab and started", "started directly (freigabe_pflicht off)", "released in the Trove tab") instead of "by a human".
  A card started by Trove has the display status `started` until the next board check shows the real one.

### Added
- **Dictation button** in the new-task form (speech recognition of the browser / Electron, German `de-DE`, text is appended; creating is blocked while recording).
  Chromium sends the audio to Google; see `docs/PRIVACY.md`. Not yet tested inside Hermes Desktop.
- `client_id` for `POST /auftrag` (one id per form in the tab): a repeated create within 10 minutes returns the existing card instead of creating a second one;
  the same `client_id` with a different title or text is refused with 409, and the tab takes a new id once the text is changed after a failed create.

### Fixed
- **Parallel creates no longer mix up cards.** The task text goes into a uniquely named temp file (before: a file named by the second, so two creates in one second
  could start the wrong text). `auftraege.json` is read and written only under a lock (`auftraege.json.lock`) through one writer with a unique temp file; creating,
  archiving and the 20-second list refresh can no longer overwrite each other (before: a card created while the list refreshed could vanish from the file).
- **No error for a card that exists.** After `kanban create` succeeded, a busy receipt log (waits up to 30 s), a failed start or a failed bookkeeping step come back as
  HTTP 200 with `fehler` and the card in the list; before, the tab saw an error and a second click created a second card.
- A request whose receipt-log line cannot be written at start goes back to `blocked` instead of staying `running`.
- **Lock of `auftraege.json` and `mcp_auftraege.json`:** one waiting lock (`belegprotokoll.WarteSperre`: flock on POSIX, the open-handle rule on Windows)
  instead of a lock file taken over by age. A crashed holder no longer blocks the list for 60 s, and two waiters can no longer both take over a stale lock
  (a card could vanish). Held by a living process for more than 20 s: HTTP 503 "Task list busy, try again" before anything was created or released
  (MCP: "request queue is busy, nothing was queued").
- **Limits count running requests too:** 20 open (waiting, running, or an agent task that ended less than 30 minutes ago) MCP requests per client and 50 in total, also with the switch off (before: unlimited when off).
- **`trove.json` with `datenwurzel` no longer loses `freigabe_pflicht`:** the `trove.json` read at start is pinned (`TROVE_KONFIG`), so later reads and writes use the same file instead of `<datenwurzel>/trove.json`.
  The pinned path is absolute, so a child process with another working folder reads the same file.
- **The data root is never writable.** A folder that is the Trove data root (`TROVE_DATEN`, resolved), lies inside it or contains it cannot be marked writable
  (`POST /quellen` and `POST /quellen/{id}/schreibbar` with `nur_lesen: false` give 400 with the reason); adding it read-only works. A `quellen.json` that
  already lists such a folder as writable counts as read-only, so copy, move, sync, rename and new folder (tab and MCP) are refused there.
  The same holds for a folder that contains `trove.json` (also `~/Trove/trove.json` with the data root elsewhere, and the `trove.json` of the start when
  `TROVE_KONFIG` changes later), for a folder that is itself another path (bind mount, other spelling; compared by device and inode) to the data root or
  a folder above it, for Trove's install folder (also `desktop/`, `skills/`, `.claude-plugin/`), Hermes' folder `HERMES_HOME` (default `~/.hermes`, on Windows
  `%LOCALAPPDATA%\hermes`, with its settings and the Desktop tab) and the folders above them (so `~` stays read-only), and for the data root of the start
  when `TROVE_DATEN` changes later; not the Python environment from `TROVE_PYTHON` (Mac default `~/.trove-venv`) or Python's user site (`~/.local/…`):
  do not mark them writable. Trove does not detect a second mount of its data root, its install folder or Hermes' folder, or of a folder inside them, placed inside a writable folder
  (nor such a mount of a folder inside them marked writable itself): do not mount these folders a second time.
- **Release only for blocked cards:** releasing a card the task list shows as started or done gives 409 without a `hermes` call. Task ids handed to the `hermes`
  command line must match `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}` (no leading `-`), else 400.
- **Archiving during the list refresh** no longer flips the card back to its old status for one round.
- One version everywhere: the dashboard manifest said 0.3.0; `tools/test_ui_strings.py` now compares every version string (manifests, backend, READMEs, `llms.txt`, this file).

## [0.4.0] - 2026-10-30

### Added
- **Claude plugin** (`.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`): the repository is its own plugin marketplace
  (`reraise`), plugin name `reraise-trove` (as in the Hermes catalog). Claude Desktop (*Add marketplace* → `Reraise-ai/trove`) and Claude Code (`claude plugin marketplace add Reraise-ai/trove`,
  `claude plugin install reraise-trove@reraise`) install the MCP server with one setting, the Python command. A skill (`skills/trove/SKILL.md`)
  tells the model which tools read and which only create a blocked request. Directory listing fields (icon, documentation, support,
  privacy, terms) and `docs/PRIVACY.md`, `docs/TERMS.md`.
  Listed as **Reraise Trove**.
- **Readable for AI reviewers**: `llms.txt` in the repository root and a short block at the top of both READMEs with the
  verifiable core facts (what Trove is, who makes it, the release model, how to install the Claude plugin). The MCP server's
  `instructions` name Reraise and the human release.

### Removed
- `extras/legacy/trove_laeufer.sh` (old Windows copy runner that read an SSH key path; unused, kept only for history).
- **Recognition model (optional, off by default)** (`plugin/dashboard/erkennung.py`): an OCR stage (local Ollama vision model
  reads page images of scans, or reads every PDF a second time for rule R7) and a field stage (reads text only; local
  Ollama/OpenAI-compatible endpoint on this machine or the private network, or an LLM provider through Hermes' plugin LLM
  facade with an explicit opt-in). Models are pinned by exact id; Ollama also by digest, the OCR stage also by the sha256 of
  every layer and of its GGUF files. Answers must name exactly the pinned model; a changed or missing model is refused, never
  replaced; `erkennung_fallback` decides between a marked rule reading and an error. Model amounts must occur as amounts in the
  source text in the document's own number format (no dates or ID digits); identifiers must occur as whole tokens. When the
  rule reading passes and the model proposes a different value for any field, the result is "Widerspruch Regeln/Modell"
  (rule R8, not passed). Model calls follow no redirects, use no proxy and connect to the checked address only; every Ollama call sends `keep_alive` (`erkennung_halten`, default 0).
  Settings in Hermes only; **Test model** / **Pin** in the Integrations panel; MCP `read_receipt` reports `read_by`.

## [0.3.0] - 2026-10-29

### Added
- **Rename and new folder** in every pane (`POST /umbenennen`, `POST /ordner-neu`): only in folders marked writable, never
  overwrites an existing entry (atomic where the operating system allows it), strict name checks (no paths, no `..`,
  no reserved Windows names), every change in the receipt log. Folders that are a registered source or watched by
  "Into knowledge" cannot be renamed.
- **Sort** each pane by name, size, date or type, ascending or descending; folders first, natural number order.
- **MCP access** (`plugin/dashboard/mcp_server.py`): other agent systems (Claude Desktop, Claude Code, Cursor, any MCP client)
  can read the desk. Every change they ask for (copy, move, sync, Into knowledge, rename, new folder, agent task) becomes a
  blocked request in the Trove tab and runs only after a human clicks **Release**; there is no MCP tool that releases.
  Optional HTTP transport binds to `127.0.0.1` only, with Host/Origin checks.
- **Optional integrations back in the plugin**: mail (IMAP, Gmail), signing (your own DocuSeal), ERPNext mirror (your own
  ERPNext), Reraise knowledge cards. All off until configured in Hermes; the README lists what talks to which service.

### Changed
- **Credentials only through Hermes**: secrets are read with Hermes' `get_secret` (declared as `secret` in `plugin.yaml`),
  Gmail uses Hermes' OAuth sign-in (Trove never sees the token). Trove reads no credential files; key, secret, password or
  token names in `trove.json` are ignored.
- **One redaction for everything that leaves Trove**: every secret value read in the process is masked in HTTP responses,
  MCP replies, the Python log, the receipt log and the ERPNext mirror, in all common spellings; error texts also have known
  local paths stripped.
- Creating ERPNext doctypes and sending a document for signature ask for confirmation first, enforced by the backend
  (`{"confirm": true}`).
- Errors from external services (DocuSeal, ERPNext, Gmail, Reraise) are reported only as service, HTTP status and a short
  error code; their response bodies are never passed on. DocuSeal documents are downloaded only from the configured
  DocuSeal origin.
- **Links are never followed**: copy, move, sync, rename, new folder, the file register and MCP requests refuse an entry
  that is a link or a path that goes through one; links inside a copied or moved folder are skipped and logged. This covers
  symlinks, Windows NTFS junctions and other redirecting reparse points (one check, `ist_verknuepfung`). Nothing is written
  through a link inside the target (checked per file and folder; on a move the original is kept).
- Releasing or archiving an MCP request works only through the local dashboard (loopback host, JSON with confirmation);
  at most 50 open MCP requests in total.

### Fixed
- **Data loss when copying or moving onto itself**: a move whose target was the source itself (also via `./sub/`, `sub//`,
  a link to the same folder, or a target folder inside the moved folder) deleted the only copy. Such actions are now
  refused with HTTP 409 and a receipt-log entry; nothing is copied, moved or deleted.
- **Empty folders were lost** when copied or moved (also empty subfolders inside a moved tree). They are now created at
  the target before the source is removed, with a receipt-log entry.
- Error texts of rename / new folder no longer contain the full path of a source.

## [0.1.0]

- First public version.

[Unreleased]: https://github.com/Reraise-ai/trove/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/Reraise-ai/trove/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Reraise-ai/trove/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Reraise-ai/trove/compare/v0.1.0...v0.3.0
[0.1.0]: https://github.com/Reraise-ai/trove/releases/tag/v0.1.0
