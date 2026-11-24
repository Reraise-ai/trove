# Privacy

Trove runs on your own computer. This page describes what the software does with data, in plain words.

- **Everything stays local.** Trove reads the folders you add as sources and writes only into its own data folder
  (`~/Trove` by default, or what `TROVE_DATEN` / `trove.json` says): the receipt log, markdown twins, snapshots, the file register.
- **No telemetry, no accounts, no update checks.** Trove sends nothing to the Trove maintainers and does not phone home.
- **No network listener by default.** The MCP server talks to its client over stdin/stdout. The optional `--http` mode binds to
  `127.0.0.1` only and refuses other hosts and browser origins.
- **Optional integrations are off until you configure them** in Hermes: mail (IMAP / Gmail), signing (your own DocuSeal),
  an ERPNext mirror (your own ERPNext), Reraise knowledge cards, and an optional recognition model. Each one talks only to the
  service you configured. The README lists what talks to which service.
- **The receipt log records what happens on the desk**: which file was read, copied, moved, requested, started or released, by whom
  (the MCP client's self-reported name or the tab), and when. It is stored locally and is yours.
- **Agents act directly by default.** Over MCP and from the tab, file actions start at once inside the folders you marked writable; nothing is overwritten.
  An agent task starts a Hermes agent with Hermes' own permissions. Trove's folder rights do not bind it. If `TROVE_ARBEITSORDNER` is set, the agent's working directory is that folder (with the switch `arbeitsordner_pruefen` on, Trove starts the task only when that folder is marked writable), but the agent can still reach other files with Hermes' rights. Without `TROVE_ARBEITSORDNER` the task runs in a Hermes scratch workspace with Hermes' rights. With `freigabe_pflicht` off, an MCP client can therefore trigger work outside the folder rights; switch it on if you do not want that.
  The optional switch `freigabe_pflicht` makes every new task card and every MCP request wait for Release in the Trove tab. Set it with `"freigabe_pflicht": true` in `trove.json` (the recommended way); the environment variable `TROVE_FREIGABE_PFLICHT` can only switch it on, never off, and applies only to the process that sees it. Unclear values, a `trove.json` that cannot be read and a `TROVE_KONFIG` that points to a missing file count as on, with a warning in `/status`. Whoever sets the environment of the MCP server process (`TROVE_KONFIG`, `TROVE_DATEN`) decides which `trove.json` it reads; `status` shows the file (`freigabe_pflicht_datei`). That holds back MCP clients and foreign web pages, not programs on the same computer: a local program can call the tab's
  release route itself.
- **The dictation button sends audio to Google.** The new-task form can dictate into the task text. This uses the speech recognition of the browser or
  of Electron (language `de-DE`); in Chromium-based browsers the recording goes to an online speech service of Google. Trove itself neither sends nor
  stores audio, and the button does nothing until you press it. Type instead if you do not want that.
- **Knowledge cards stay on your machine** unless you set `karten_befehl`: then Trove hands each card to that command of yours (a local program you chose), which decides where it goes.
- **Secrets** are read only through Hermes' secret store and are masked in every output that leaves the process.

When Trove is installed as a Claude plugin, Claude Desktop or Claude Code starts the MCP server as a child process on your machine
and exchanges tool calls with it. What the model provider sees of those tool results is governed by that provider's terms, not by Trove.

Questions: open an issue at https://github.com/Reraise-ai/trove/issues.
