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

License: Apache-2.0
