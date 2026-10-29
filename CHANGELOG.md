# Changelog

All notable changes to Trove are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
- Creating ERPNext doctypes and sending a document for signature ask for confirmation first.
- **Links are never followed**: copy, move, sync, rename, new folder, the file register and MCP requests refuse an entry
  that is a link or a path that goes through one; links inside a copied or moved folder are skipped and logged. This covers
  symlinks and Windows NTFS junctions (one check, `ist_verknuepfung`).
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

[Unreleased]: https://github.com/Reraise-ai/trove/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Reraise-ai/trove/compare/v0.1.0...v0.3.0
[0.1.0]: https://github.com/Reraise-ai/trove/releases/tag/v0.1.0
