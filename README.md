# Trove - Datei-Zentrale

A local-first file manager with an integrity receipt log (Belegprotokoll).

- Bind sources: a local folder or an rclone remote.
- Copy / move / sync; every action is logged with a sha256 read-back check.
- Nothing is overwritten; a move deletes the source only after the read-back passes, and only for local sources (rclone remotes are read-only mirrors).
- Duplicate detection via a per-tenant SQLite register.
- Optional local document signing via DocuSeal (https://github.com/docusealco/docuseal).
- Hands a task to an agent (agent command/communication).

Runs as a Hermes desktop plugin (see plugin/). Code comments are in German.
Secrets are read from external files/env, never from the repo. Private data and registers are not included.

## Configuration

Trove has no fixed paths. Everything is set by environment variables (defaults in brackets):

| Variable | Meaning |
| --- | --- |
| `TROVE_DATEN` | data folder holding one sub-folder per tenant (`~/trove-daten`) |
| `TROVE_REPO` | location of this repository for the Hermes plugin (`~/trove`) |
| `TROVE_REGISTER_DB` | optional read-only register of an earlier source (`<TROVE_DATEN>/_altindex/register.db`) |
| `TROVE_PAPERLESS`, `TROVE_PAPERLESS_TOKEN` | optional Paperless-ngx URL and token file |
| `TROVE_DOCUSEAL`, `TROVE_DOCUSEAL_SCHLUESSEL` | optional DocuSeal URL and key file |
| `TROVE_QUELLORT` | value of the register column `ort` that `gegenprobe.py` and `trove_laeufer.sh` read (`quelle`) |

`trove_laeufer.sh` additionally needs `TROVE_SERVER`, `TROVE_REGISTER_WIN`, `TROVE_ZIEL_WIN` and `TROVE_REPO_WIN`; see the header of the script.

Before a release, `bash tools/spuren-pruefen.sh` checks that no trace of a private installation (host names, internal addresses, fixed drive paths, personal names) is left in tracked files.

## Install (Hermes desktop plugin)

Requires Hermes Desktop >= 0.19.

1. `git clone https://github.com/Reraise-ai/trove.git`
2. `python plugin/installieren.py`
3. Reload Hermes (or restart it) — the "Trove" tab appears.

The installer locates the Hermes home automatically on Windows, macOS and Linux. If it cannot find it, set `HERMES_HOME` to your Hermes data folder and run step 2 again. It creates `config.yaml` if missing and enables the plugin idempotently (an existing `config.yaml` is backed up before any change).

## Disclosures (plugin catalog)

- Third-party network calls, all optional and only when the user configures them:
  - reraise.ai: uploading public knowledge cards. Only with a stored Reraise key; without a key the button only opens the reraise.ai sign-up page (no upload). The server keeps the upload route closed in customer mode.
  - A user-provided ERPNext instance (TROVE_ERPNEXT): mirroring receipts; inactive without configuration.
  - Gmail, read-only, via the user's existing Hermes Google token: e-mail archive.
  - A user-provided DocuSeal instance (TROVE_DOCUSEAL): optional document signing.
  - Optional Paperless-ngx (TROVE_PAPERLESS).
- Subprocesses: OCR and PDF-splitting run as a separate Python process (TROVE_PYTHON) because the Hermes runtime Python has no OCR libraries.
- Background process: the optional Dauerlauf watches configured folders and updates knowledge twins on a schedule (LaunchAgent on macOS, Scheduled Task on Windows). Off unless the user enables it.
- Stored credentials: ERPNext key/secret, the Reraise key, DocuSeal and Paperless tokens are read from local files or environment variables that the user sets (see Configuration). They are never bundled in the repo. Trove reads these from its own files/env, not from Hermes' secret_scope.
- Optional OCR dependencies (onnxruntime, rapidocr-onnxruntime) are installed separately by the installer and may be skipped (then "no OCR"); they are not hard python_dependencies.
- No telemetry.

License: Apache-2.0
