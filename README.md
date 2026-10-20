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

License: Apache-2.0
