# Optional integrations (NOT part of the Hermes plugin)

These scripts come from the standalone Trove repository. They talk to outside services (IMAP mailbox, DocuSeal, ERPNext, Reraise)
and **read credentials from files or environment variables on disk**. They are therefore **not included in the plugin package
(`plugin/`) and not part of the Hermes catalog build.** The plugin never imports them.

If you still want them, run them by hand from a checkout of this repository, for example
`python extras/integrations/mail_abholer.py --help`. They use Trove's receipt log from `plugin/dashboard/`.

| Script | What it does | Credentials it reads |
| --- | --- | --- |
| `mail_abholer.py` | Fetches mail attachments over IMAP into a source folder | IMAP login from a credential file |
| `docuseal.py` | Sends documents to DocuSeal for signature | DocuSeal API token file |
| `spiegel_erpnext.py` | Mirrors receipts into ERPNext | ERPNext API login file |
| `reraise_hochladen.py` | Uploads public knowledge cards to Reraise | Reraise API token file |

Inside Hermes, do not use these. Let a Hermes agent fetch mail attachments or drive files with Hermes's own connectors and drop them
into a source folder that Trove watches. Trove never asks for or stores your passwords.

## `extras/legacy/`

Old one-off scripts from the standalone repository (register migration, a Windows copy runner that reads an SSH key path, measurement
scripts). They are kept only for history, are **not part of the plugin or the catalog build**, and nothing imports them.
