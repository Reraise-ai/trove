# extras (NOT part of the Hermes plugin)

The optional integrations (mail, DocuSeal, ERPNext mirror, Reraise) used to live here because they read credentials from files.
They are now part of the plugin (`plugin/dashboard/`), off by default, and get their credentials only from Hermes. See
"Optional integrations" in the [README](../README.md#optional-integrations).

## `extras/legacy/`

Old one-off scripts from the standalone repository (register migration, measurement scripts). They are kept only for history, are **not part of the plugin or the catalog build**, and nothing imports them.
