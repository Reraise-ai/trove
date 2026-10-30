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
- **The receipt log records what happens on the desk**: which file was read, copied, moved, requested or released, by whom
  (the MCP client's self-reported name or the tab), and when. It is stored locally and is yours.
- **Secrets** are read only through Hermes' secret store and are masked in every output that leaves the process.

When Trove is installed as a Claude plugin, Claude Desktop or Claude Code starts the MCP server as a child process on your machine
and exchanges tool calls with it. What the model provider sees of those tool results is governed by that provider's terms, not by Trove.

Questions: open an issue at https://github.com/Reraise-ai/trove/issues.
