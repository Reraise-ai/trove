"""Credentials and settings for Trove's optional integrations: only through Hermes (catalog rule 11).

Secrets are read with agent.secret_scope.get_secret(NAME). Every NAME is declared in plugin.yaml (config_schema, type secret),
so the user enters it in the Hermes Desktop (Settings -> Plugins -> Trove) or with `hermes config` and Hermes stores it.
Plain settings (URLs, host names) come from plugins.entries.reraise-trove.settings (hermes_cli.config.load_config_readonly).
Gmail uses an OAuth client declared under oauth: in plugin.yaml; Hermes signs in, stores and refreshes the token and attaches
it to each request (hermes_cli.plugin_provider_requests.credentialed_provider_request). Trove never sees that token.

Trove reads no credential file, no .env and no auth.json. Outside Hermes (no agent package) nothing is configured, so every
integration stays inactive. Secret values never go into logs or responses: schwaerzen() masks every value read here.
"""
import logging
import re
import threading

PLUGIN_ID = "reraise-trove"
_LOG = logging.getLogger("trove")

# secret environment names (declared in plugin.yaml config_schema as type secret)
DOCUSEAL_KEY = "TROVE_DOCUSEAL_API_KEY"
ERPNEXT_KEY = "TROVE_ERPNEXT_API_KEY"
ERPNEXT_SECRET = "TROVE_ERPNEXT_API_SECRET"
IMAP_PASSWORT = "TROVE_IMAP_PASSWORD"
RERAISE_KEY = "TROVE_RERAISE_KEY"
GMAIL_CLIENT_ID = "TROVE_GMAIL_CLIENT_ID"          # oauth client_id_env (declared under optional_env and config_schema)
GMAIL_CLIENT_SECRET = "TROVE_GMAIL_CLIENT_SECRET"  # oauth client_secret_env
GEHEIMNISSE = (DOCUSEAL_KEY, ERPNEXT_KEY, ERPNEXT_SECRET, IMAP_PASSWORT, RERAISE_KEY, GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET)
GMAIL_OAUTH_ID = "gmail"  # oauth: id in plugin.yaml; sign-in: hermes auth add reraise-trove:gmail

# plain settings (config_schema) and their neutral defaults: nothing points at any installation
STANDARD = {"docuseal_url": "", "erpnext_url": "", "erpnext_host_header": "", "imap_host": "", "imap_user": "", "imap_port": 993,
            "imap_folders": "INBOX", "gmail_query": ""}

# test seams: the self-tests replace these with mocks (never real services)
geheimnis_quelle = None      # callable(name) -> str | None
einstellungen_quelle = None  # callable() -> dict
anfrage_quelle = None        # callable(provider, method, url, headers, timeout) -> (status, bytes)

_bekannt = set()
_formen = set()
_formen_sortiert = []
_sperre = threading.Lock()


class NichtEingerichtet(RuntimeError):
    """The integration is not configured in Hermes (inactive)."""


def geheimnis(name):
    """Secret by its declared name, through Hermes only; None when not set or not readable (never a fallback)."""
    if name not in GEHEIMNISSE:
        raise KeyError("undeclared secret name")
    if geheimnis_quelle is not None:
        wert = geheimnis_quelle(name)
    else:
        try:
            from agent.secret_scope import get_secret
        except ImportError:
            return None  # not running inside Hermes: nothing is configured
        try:
            wert = get_secret(name)
        except Exception as ex:  # e.g. no profile scope under multiplexing: fail closed, never another profile's value
            _LOG.warning("trove: secret %s not readable here (%s)", name, type(ex).__name__)
            return None
    wert = (wert or "").strip()
    _merken(wert)
    return wert or None


def ist_gesetzt(name):
    return geheimnis(name) is not None


def einstellungen():
    """This plugin's settings from Hermes (plugins.entries.<id>.settings) over the neutral defaults."""
    roh = {}
    if einstellungen_quelle is not None:
        roh = einstellungen_quelle() or {}
    else:
        try:
            from hermes_cli.config import load_config_readonly
            roh = (((load_config_readonly().get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {}).get("settings") or {}
        except ImportError:
            roh = {}
        except Exception as ex:
            _LOG.warning("trove: Hermes settings not readable (%s)", type(ex).__name__)
            roh = {}
    out = dict(STANDARD)
    for k in STANDARD:
        if roh.get(k) not in (None, ""):
            out[k] = roh[k]
    return out


def einstellung(name):
    return einstellungen()[name]


def url_einstellung(name):
    """A configured base URL (http/https, no user info) or None."""
    u = str(einstellung(name) or "").strip().rstrip("/")
    if not u:
        return None
    if not re.match(r"^https?://[^/@\s]+(/[^\s]*)?$", u, re.I):
        raise NichtEingerichtet("setting %s must be an http(s) URL without user name or password" % name)
    return u


def anfrage_mit_anmeldung(provider, methode, url, headers=None, timeout=60.0):
    """Request with a sign-in that Hermes holds (oauth: in plugin.yaml). Returns (status, body bytes); the token never reaches Trove."""
    if anfrage_quelle is not None:
        return anfrage_quelle(provider, methode, url, headers or {}, timeout)
    try:
        from hermes_cli.plugin_provider_requests import ProviderNotSignedIn, credentialed_provider_request
    except ImportError:
        raise NichtEingerichtet("Gmail needs Hermes (sign-in through Hermes only)")
    try:
        r = credentialed_provider_request(provider, methode, url, headers=headers, timeout=timeout)
    except ProviderNotSignedIn:
        raise NichtEingerichtet("Gmail is not signed in: run `hermes auth add %s:%s` in a terminal" % (PLUGIN_ID, GMAIL_OAUTH_ID))
    except Exception as ex:
        code = getattr(ex, "code", "")
        raise RuntimeError("Gmail sign-in through Hermes failed: %s %s" % (type(ex).__name__, code)) from None
    return r.status, r.body


MIN_TEIL = 6  # shortest fragment of a multi-line secret that is still masked


def _varianten(wert):
    """Every spelling of a secret that can end up in a text: raw, JSON-escaped, repr, URL-encoded, and each line of it."""
    import json as _json
    from urllib.parse import quote, quote_plus
    v = {wert, _json.dumps(wert)[1:-1], _json.dumps(wert, ensure_ascii=False)[1:-1], repr(wert)[1:-1], ascii(wert)[1:-1],
         quote(wert, safe=""), quote(wert), quote_plus(wert), quote_plus(wert, safe="")}
    v |= {_json.dumps(x)[1:-1] for x in list(v)}  # double-encoded (JSON text inside JSON)
    for teil in re.split(r"[\r\n]+", wert):
        teil = teil.strip()
        if len(teil) >= MIN_TEIL:
            v |= {teil, _json.dumps(teil)[1:-1], repr(teil)[1:-1]}
    return {x for x in v if x and len(x) >= min(MIN_TEIL, len(wert))}


def _merken(wert):
    if wert and len(wert) >= 4:
        with _sperre:
            if wert not in _bekannt:
                _bekannt.add(wert)
                _formen.update(_varianten(wert))
                _formen_sortiert[:] = sorted(_formen, key=len, reverse=True)


def schwaerzen(text):
    """THE redaction for every outgoing text (HTTP responses, logging, receipt log, ERPNext mirror): every secret value read in
    this process, in every spelling (_varianten), becomes ***. Redact first, truncate afterwards (kurz)."""
    t = text if isinstance(text, str) else str(text)
    with _sperre:
        formen = list(_formen_sortiert)
    for w in formen:
        if w in t:
            t = t.replace(w, "***")
    return t


def kurz(text, n):
    """Redact, then truncate to n characters (never the other way round: a cut can leave half a secret)."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", "replace")
    return schwaerzen(text)[:n]


def schwaerzen_tief(obj):
    """Redact every string inside dicts/lists/tuples (keys too) before anything is serialised."""
    if isinstance(obj, str):
        return schwaerzen(obj)
    if isinstance(obj, dict):
        return {schwaerzen_tief(k) if isinstance(k, str) else k: schwaerzen_tief(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [schwaerzen_tief(v) for v in obj]
    return obj


class _LogFilter(logging.Filter):
    """Python logging: the formatted message of every 'trove' record is redacted before any handler sees it."""

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg, record.args = schwaerzen(msg), ()
        if record.exc_info:
            import traceback
            record.exc_text = schwaerzen("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return True


_LOG.addFilter(_LogFilter())


def selbsttest():
    global geheimnis_quelle, einstellungen_quelle
    alt = geheimnis_quelle, einstellungen_quelle
    try:
        geheimnis_quelle = lambda n: {RERAISE_KEY: " k-geheim-123 "}.get(n)
        einstellungen_quelle = lambda: {"docuseal_url": "http://127.0.0.1:9/", "imap_port": None}
        assert geheimnis(RERAISE_KEY) == "k-geheim-123" and geheimnis(DOCUSEAL_KEY) is None
        assert schwaerzen("x k-geheim-123 y") == "x *** y"
        import json as _json
        from urllib.parse import quote, quote_plus
        geheimnis_quelle = lambda n: {RERAISE_KEY: 'S3-a\\b"c/d+e f', DOCUSEAL_KEY: "zeile-eins-123\nzeile-zwei-456"}.get(n)
        k, m = geheimnis(RERAISE_KEY), geheimnis(DOCUSEAL_KEY)
        for form in (k, _json.dumps(k), repr(k), quote(k, safe=""), quote_plus(k), _json.dumps({"x": "invalid " + k})):
            assert "S3-a" not in schwaerzen("pre " + form + " post"), form
        assert "zeile-eins" not in schwaerzen("bad header %r" % m) and "zeile-zwei-456" not in schwaerzen("..." + m.split("\n")[1])
        assert kurz("y" * 395 + k + "z", 400) == ("y" * 395 + "***z")[:400]
        assert schwaerzen_tief({"a": ["x " + k], k: 1}) == {"a": ["x ***"], "***": 1}
        assert url_einstellung("docuseal_url") == "http://127.0.0.1:9" and url_einstellung("erpnext_url") is None
        assert einstellung("imap_port") == 993
        try:
            geheimnis("OPENAI_API_KEY")
            raise AssertionError("undeclared name read")
        except KeyError:
            pass
        einstellungen_quelle = lambda: {"erpnext_url": "https://user:pw@example.invalid"}
        try:
            url_einstellung("erpnext_url")
            raise AssertionError("URL with user info accepted")
        except NichtEingerichtet:
            pass
        geheimnis_quelle = einstellungen_quelle = None
        try:
            import agent.secret_scope  # noqa: F401
            in_hermes = True
        except ImportError:
            in_hermes = False
        if not in_hermes:
            assert all(geheimnis(n) is None for n in GEHEIMNISSE), "outside Hermes nothing may be configured"
    finally:
        geheimnis_quelle, einstellungen_quelle = alt


if __name__ == "__main__":
    selbsttest()
    print("zugang selftest ok")
