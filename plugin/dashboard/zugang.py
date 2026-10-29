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
_lokal = threading.local()
_gewarnt = set()


class NichtEingerichtet(RuntimeError):
    """The integration is not configured in Hermes (inactive)."""


class DienstFehler(RuntimeError):
    """An outside service failed. The text is written by Trove only (service, HTTP status, at most a validated short error code);
    a response body, reason phrase or other upstream text never goes into it."""


def http_fehler(dienst, status, body=None, *felder):
    """DienstFehler for a non-success answer: only "<service> answered HTTP <status>". Body and any error code in it are dropped
    (body/felder are accepted for call compatibility and ignored on purpose: an upstream code would be a side channel)."""
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 0
    return DienstFehler("%s answered HTTP %d" % (dienst, status))


def fehlertext(ex, n=300):
    """Text of an exception for responses, logs and the receipt log. Trove's own errors keep their text; anything that may carry
    upstream text (HTTP errors with reason phrases, protocol errors, unknown exceptions) is reduced to its type."""
    import http.client
    import urllib.error
    eigene = ("LaufLaeuft", "ProtokollGesperrt", "ProtokollDefekt")
    if isinstance(ex, urllib.error.HTTPError):
        t = "HTTP %s" % ex.code
    elif isinstance(ex, (NichtEingerichtet, DienstFehler, ValueError, KeyError)) or type(ex).__name__ in eigene:
        t = str(ex)
    elif isinstance(ex, urllib.error.URLError):
        r = ex.reason
        t = "%s (%s)" % (type(ex).__name__, getattr(r, "strerror", None) or type(r).__name__)
    elif isinstance(ex, OSError):
        t = "%s (%s)" % (type(ex).__name__, ex.strerror or "no details")
    elif isinstance(ex, http.client.HTTPException):
        t = "%s: protocol error (details withheld)" % type(ex).__name__
    else:
        t = "%s: unexpected error (details withheld)" % type(ex).__name__
    return kurz(t, n)


_GET_SECRET = []


def _hermes_get_secret():
    if not _GET_SECRET:
        try:
            from agent.secret_scope import get_secret
        except ImportError:
            get_secret = None
        _GET_SECRET.append(get_secret)
    return _GET_SECRET[0]


def geheimnis(name):
    """Secret by its declared name, through Hermes only; None when not set or not readable (never a fallback)."""
    if name not in GEHEIMNISSE:
        raise KeyError("undeclared secret name")
    if geheimnis_quelle is not None:
        wert = geheimnis_quelle(name)
    else:
        get_secret = _hermes_get_secret()
        if get_secret is None:
            return None  # not running inside Hermes: nothing is configured
        try:
            wert = get_secret(name)
        except Exception as ex:  # e.g. no profile scope under multiplexing: fail closed, never another profile's value
            if name not in _gewarnt:  # once per name and process
                _gewarnt.add(name)
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


GMAIL_NETZ = "Gmail could not be reached (network unreachable). Check the internet connection and try again."
GMAIL_ANMELDEN = ("Gmail sign-in is missing or has expired: run `hermes auth add %s:%s` in a terminal, then try again."
                  % (PLUGIN_ID, GMAIL_OAUTH_ID))
GMAIL_ABGELEHNT = "Google rejected the Gmail request (HTTP %s)."


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
        raise NichtEingerichtet(GMAIL_ANMELDEN) from None
    except (OSError, TimeoutError):
        raise DienstFehler(GMAIL_NETZ) from None
    except Exception as ex:
        name = type(ex).__name__
        if name == "AuthError" and getattr(ex, "code", "") == "oauth_refresh_failed" or "Connect" in name or "Timeout" in name or "Network" in name:
            raise DienstFehler(GMAIL_NETZ) from None
        if name == "AuthError":  # revoked, expired or rejected client: a new sign-in fixes it
            raise NichtEingerichtet(GMAIL_ANMELDEN) from None
        raise DienstFehler(GMAIL_ABGELEHNT % "?") from None
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


NEU_LADEN_S = 60  # declared secrets are re-read at most this often (or after neu_laden(), e.g. on "Refresh" in the UI)
_geladen = {"um": None, "quelle": None}


def neu_laden():
    """Forget the load time: the next schwaerzen() reads every declared secret again (settings may have changed)."""
    _geladen["um"] = None


def alle_laden():
    """Read every declared secret (through Hermes) so redaction knows them even in a process that has not used them yet.
    Cached: at most every NEU_LADEN_S seconds, after neu_laden(), or when the secret source changes."""
    import time as _time
    jetzt = _time.monotonic()
    if _geladen["um"] is not None and _geladen["quelle"] is geheimnis_quelle and jetzt - _geladen["um"] < NEU_LADEN_S:
        return
    if getattr(_lokal, "laedt", False):
        return
    _lokal.laedt = True
    try:
        for n in GEHEIMNISSE:
            geheimnis(n)
        _geladen["um"], _geladen["quelle"] = jetzt, geheimnis_quelle
    finally:
        _lokal.laedt = False


def schwaerzen(text):
    """THE redaction for every outgoing text (HTTP responses, logging, receipt log, ERPNext mirror): every declared secret (all
    loaded first, alle_laden), in every spelling (_varianten), becomes ***. Redact first, truncate afterwards (kurz).
    Second layer only: upstream bodies are not passed on in the first place (http_fehler, fehlertext)."""
    alle_laden()
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
        # K8: only "<service> answered HTTP <status>", never a code or text from the body
        e = http_fehler("ERPNext", 401, b'{"exc_type": "AuthenticationError", "message": "boom"}', "exc_type")
        assert str(e) == "ERPNext answered HTTP 401", e
        # R1: a secret source that fails warns once per name; schwaerzen() does not re-read on every call
        aufrufe = []

        def kaputt(n):
            aufrufe.append(n)
            raise RuntimeError("no profile scope")
        zaehler = []

        class Z(logging.Handler):
            def emit(self, r):
                zaehler.append(r.getMessage())
        h = Z()
        _LOG.addHandler(h)
        _gewarnt.clear()
        try:
            _GET_SECRET[:] = [kaputt]
            geheimnis_quelle = None
            neu_laden()
            for _ in range(200):
                schwaerzen("zeile")
            assert len(zaehler) == len(GEHEIMNISSE) and len(aufrufe) == len(GEHEIMNISSE), (len(zaehler), len(aufrufe))
            neu_laden(); schwaerzen("x")
            assert len(aufrufe) == 2 * len(GEHEIMNISSE) and len(zaehler) == len(GEHEIMNISSE)  # reloaded, no new warnings
        finally:
            _LOG.removeHandler(h)
            _GET_SECRET[:] = []
            neu_laden()
        # K9: Gmail failures through Hermes have fixed, readable texts
        assert "hermes auth add reraise-trove:gmail" in GMAIL_ANMELDEN and "network unreachable" in GMAIL_NETZ
        assert "RuntimeError" not in GMAIL_ANMELDEN + GMAIL_NETZ + GMAIL_ABGELEHNT
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
