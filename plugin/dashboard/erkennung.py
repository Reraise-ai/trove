"""Trove recognition model ("Erkennungsmodell"): optional, pinned models for receipt reading. Off by default.

Two stages, each pinned on its own (Hermes plugin settings, human-only; no MCP tool changes them):
  OCR stage    erkennung_ocr_modell: a local Ollama vision model (e.g. unlimited-ocr-3b:latest) reads page images.
               erkennung_ocr_einsatz "scans" = replaces the local rapidocr for scans; "second_reading" = reads every PDF a second
               time (page images -> model text -> the same field rules) so rule R7 compares the two readings.
               Pin: Ollama digest + sha256 of every model layer + sha256 of the GGUF files it was built from
               (erkennung_ocr_dateien, hashed again in every process and on Test/Pin). Any difference is refused.
  Field stage  erkennung_modus off | local | provider, erkennung_modell (exact id). Proposes fields from TEXT only (text layer,
               e-invoice XML, or OCR text of a scan); it never reads images, so every value it returns can be checked.
               local = Ollama or OpenAI-compatible endpoint on this machine or the private network. provider = an LLM provider
               configured in Hermes, called through Hermes' plugin LLM facade (agent.plugin_llm.PluginLlm); Hermes holds the
               credentials, Trove never sees them. Sends receipt text off the machine: needs the opt-in erkennung_extern_erlaubt.

A missing model, a changed digest/layer/file or an answer that does not name exactly the pinned model is refused; Trove never
switches to another model. Digest pinning exists only for Ollama; an OpenAI-compatible endpoint is pinned by id and endpoint.
erkennung_fallback "rules" (default) keeps the rule reading and marks it ("model unavailable, rules used"); "none" is an error.

The model only proposes. Every amount it returns must occur as an amount in the source text, read in the document's own number
format (1.234,56 or 1,234.56, thousands groups of exactly three digits; dates, times, percentages and invoice/ID numbers are not
amounts); identifiers must occur as a whole token. Anything else is dropped and reported. The fixed rules (verprobung.py) still
decide; if the rule reading passes and the model's proposal differs in any field, the result is "Widerspruch Regeln/Modell" and
does not pass. Model calls never follow redirects and use no proxy. Every Ollama call sends keep_alive (erkennung_halten, default 0
= unload right after the read; the GPU is shared).
"""
import functools
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import konfig  # noqa: E402
import zugang  # noqa: E402

MODI = ("off", "local", "provider")
FALLBACKS = ("rules", "none")
PIN_SCHLUESSEL = "erkennungsmodell_pin"
PIN_OCR = "erkennungsmodell_pin_ocr"
OCR_EINSATZ = ("scans", "second_reading")
OCR_PROMPT = "<image>\nFree OCR."
OCR_DPI = 144
OCR_TOKEN = 3000
STANDARD_ENDPOINT = "http://127.0.0.1:11434"
STANDARD_HALTEN = "0"
ZWISCHEN_HALTEN = 60  # seconds the OCR model stays loaded between the pages of one read
REGELN_GENUTZT = "model unavailable, rules used"
WIDERSPRUCH = "Widerspruch Regeln/Modell"
MAX_TEXT = 16000
ZEIT_S = 300
NETZE = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]

FELDER = ("brutto", "netto", "steuer", "satz", "datum", "rechnungsnummer", "aussteller", "cuit", "ust_id", "iban", "waehrung")
BETRAEGE = ("brutto", "netto", "steuer")
KENNUNGEN = ("rechnungsnummer", "cuit", "ust_id", "iban")
SCHEMA = {"type": "object", "properties": {
    **{k: {"type": ["number", "null"]} for k in BETRAEGE + ("satz",)},
    **{k: {"type": ["string", "null"]} for k in ("datum", "rechnungsnummer", "aussteller", "cuit", "ust_id", "iban", "waehrung")}},
    "required": list(FELDER)}
ANWEISUNG = ("You read ONE receipt or invoice and return ONE JSON object, nothing else. Keys: "
             "brutto (gross total to pay), netto (net amount before tax), steuer (tax / VAT / IVA amount), "
             "satz (tax rate in percent, e.g. 21 or 10.5), datum (invoice date as YYYY-MM-DD), rechnungsnummer (invoice number), "
             "aussteller (name of the issuing company), cuit (Argentine CUIT of the issuer, NN-NNNNNNNN-N), "
             "ust_id (German VAT ID, DE and 9 digits), iban, waehrung (ISO currency code such as EUR, ARS, USD). "
             "Use null for anything that is not printed on the document. Copy every number exactly as printed, written with a dot "
             "as decimal separator and without thousands separators. Never calculate or guess a value.")
PROBE_TEXT = ("ACME Supplies GmbH\nInvoice No. RE-2026-0417\nDate: 15.01.2026\nVAT ID DE123456789\n"
              "Net amount 1.234,50 EUR\nVAT 19 % 234,56 EUR\nTotal 1.469,06 EUR\n")
PROBE_ERWARTET = {"brutto": 1469.06, "netto": 1234.5, "steuer": 234.56}
HERMES_VERBOT = ("Hermes does not allow Trove to choose this model. In Hermes set plugins.entries.reraise-trove.llm.allow_model_override "
                 "to true (and allow_provider_override when erkennung_provider is set; optionally list the id in allowed_models).")

# test seam: replaces the network for local mode (callable(method, url, body_dict|None, headers, timeout) -> (status, dict|list))
http_quelle = None


class ErkennungFehler(RuntimeError):
    """The pinned model cannot be used. art: config | not_local | not_pinned | missing | digest_changed | other_model | unreachable |
    bad_answer | not_allowed | no_hermes. The text is Trove's own (never an upstream body)."""

    def __init__(self, art, text):
        super().__init__(text)
        self.art = art


# ---------- settings and pin ----------

def _wort(v, std):
    """A word setting. YAML reads unquoted off/no/false as False and on/yes/true as True: map them back."""
    if v is False:
        return "off"
    if v is True:
        return "on"
    if v is None or str(v).strip() == "":
        return std
    return str(v).strip().lower()


def _ja(v):
    return v is True or str(v).strip().lower() in ("true", "1", "yes", "on")


def konfiguration():
    """The recognition settings from Hermes (zugang.einstellungen), normalised. Never raises."""
    s = zugang.einstellungen()
    halten = s.get("erkennung_halten")
    return {"modus": _wort(s.get("erkennung_modus"), "off"),
            "endpoint": str(s.get("erkennung_endpoint") or STANDARD_ENDPOINT).strip().rstrip("/"),
            "modell": str(s.get("erkennung_modell") or "").strip(),
            "provider": str(s.get("erkennung_provider") or "").strip(),
            "fallback": _wort(s.get("erkennung_fallback"), "rules"),
            "extern_erlaubt": _ja(s.get("erkennung_extern_erlaubt")),
            "halten": STANDARD_HALTEN if halten is None or halten is False or str(halten).strip() == "" else str(halten).strip(),
            "ocr_modell": str(s.get("erkennung_ocr_modell") or "").strip(),
            "ocr_einsatz": _wort(s.get("erkennung_ocr_einsatz"), "scans"),
            "ocr_dateien": [x.strip() for x in str(s.get("erkennung_ocr_dateien") or "").split(",") if x.strip()],
            "ocr_sha256": [x.strip().lower() for x in str(s.get("erkennung_ocr_sha256") or "").split(",") if x.strip()],
            "ocr_prompt": str(s.get("erkennung_ocr_prompt") or OCR_PROMPT).replace("\\n", "\n"),
            "ocr_seiten": _ganz(s.get("erkennung_ocr_seiten"), 5)}


def _ganz(v, std):
    try:
        return max(1, min(int(v), 20))
    except (TypeError, ValueError):
        return std


def keep_alive(k):
    """erkennung_halten as Ollama keep_alive: seconds as a number, or a duration such as 10m; -1 = keep loaded."""
    h = k["halten"]
    if not re.fullmatch(r"-?\d+(?:\.\d+)?[smh]?", h):
        raise ErkennungFehler("config", "Setting erkennung_halten must be a number of seconds or a duration such as 10m (0 = unload at once).")
    return int(h) if re.fullmatch(r"-?\d+", h) else h


def aktiv(k=None):
    return (k or konfiguration())["modus"] != "off"


def endpoint_pruefen(ep):
    """local / OCR stage: http(s) without user info, host on this machine or the private network only (127/8, ::1, 10/8, 172.16/12,
    192.168/16; localhost and *.local names must resolve there). Anything else is provider territory."""
    if not re.match(r"^https?://[^/@\s?#]+(/[^\s@]*)?$", ep, re.I):
        raise ErkennungFehler("config", "Setting erkennung_endpoint must be an http(s) URL without user name or password.")
    try:
        host = (urllib.parse.urlsplit(ep).hostname or "").lower()
    except ValueError:
        host = ""
    nicht_lokal = ErkennungFehler("not_local", "Endpoint host '%s' is not on this machine or the private network (127.x, ::1, 10.x, 172.16-31.x, "
                                               "192.168.x, localhost, *.local). Local models stay local; an outside service needs provider "
                                               "mode with its opt-in." % host[:80])
    try:
        adressen = {ipaddress.ip_address(host)}
    except ValueError:
        if host != "localhost" and not host.endswith(".local"):
            raise nicht_lokal
        try:
            adressen = {ipaddress.ip_address(i[4][0].split("%")[0]) for i in socket.getaddrinfo(host, None)}
        except (OSError, ValueError):
            raise ErkennungFehler("unreachable", "The endpoint name '%s' cannot be resolved." % host[:80])
    gepruefte = []
    for a in sorted(adressen, key=lambda x: (x.version, int(x))):
        a2 = getattr(a, "ipv4_mapped", None) or a
        if not any(a2 in n for n in NETZE if n.version == a2.version):
            raise nicht_lokal
        gepruefte.append(str(a2))
    _AUFGELOEST[host] = (gepruefte[0], time.monotonic())
    return gepruefte[0]


_AUFGELOEST = {}  # host -> (validated IP, when): the connection goes to exactly this IP, the name is not resolved again
AUFLOESUNG_S = 300


def _ziel_ip(url):
    """The validated IP for the host of url: from the check of this read (no second resolution), else checked now."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    e = _AUFGELOEST.get(host)
    if e and time.monotonic() - e[1] < AUFLOESUNG_S:
        return e[0]
    return endpoint_pruefen(_basis(url))


def _pruefen(k):
    if k["modus"] not in MODI:
        raise ErkennungFehler("config", "Setting erkennung_modus must be off, local or provider (is '%s')." % k["modus"][:40])
    if k["fallback"] not in FALLBACKS:
        raise ErkennungFehler("config", "Setting erkennung_fallback must be rules or none.")
    if not k["modell"]:
        raise ErkennungFehler("config", "Recognition model is on but no model is pinned: set erkennung_modell to an exact model id in Hermes.")
    if k["modus"] == "local":
        endpoint_pruefen(k["endpoint"])
        keep_alive(k)
    if k["modus"] == "provider" and not k["extern_erlaubt"]:
        raise ErkennungFehler("not_allowed", "Provider mode sends receipt content to an outside provider. Turn on erkennung_extern_erlaubt "
                                             "in Hermes (Settings -> Plugins -> Trove) if you accept that.")


def pin_lesen():
    p = konfig.lesen().get(PIN_SCHLUESSEL)
    return p if isinstance(p, dict) else None


def _pin_schreiben(eintrag):
    konfig.eintrag_setzen(PIN_SCHLUESSEL, eintrag)


def _pin_passt(k, pin):
    if not pin or pin.get("modus") != k["modus"] or pin.get("modell") != k["modell"]:
        return False
    if k["modus"] == "local":
        return pin.get("endpoint") == k["endpoint"]
    return (pin.get("provider") or "") == k["provider"]


# ---------- local endpoint (Ollama or OpenAI-compatible) ----------

class _KeineWeiterleitung(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None  # never follow: a redirect would carry the key and the receipt to another host


class _FesteIP(http.client.HTTPConnection):
    """Connects to the validated IP; Host header stays the configured name."""

    def __init__(self, *a, ziel_ip=None, **kw):
        super().__init__(*a, **kw)
        self._ziel_ip = ziel_ip

    def connect(self):
        self.sock = socket.create_connection((self._ziel_ip, self.port), self.timeout)


class _FesteIPS(http.client.HTTPSConnection):
    """TLS to the validated IP; certificate and SNI are checked against the configured name."""

    def __init__(self, *a, ziel_ip=None, **kw):
        super().__init__(*a, **kw)
        self._ziel_ip = ziel_ip

    def connect(self):
        roh = socket.create_connection((self._ziel_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(roh, server_hostname=self.host)


class _IPHandler(urllib.request.HTTPHandler):
    def __init__(self, ip):
        super().__init__()
        self._ip = ip

    def http_open(self, req):
        return self.do_open(functools.partial(_FesteIP, ziel_ip=self._ip), req)


class _IPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, ip):
        super().__init__(context=ssl.create_default_context())
        self._ip = ip

    def https_open(self, req):
        return self.do_open(functools.partial(_FesteIPS, ziel_ip=self._ip, context=self._context), req)


def oeffner(url):
    """Opener for one model call: no proxy, no redirect, connection to the validated IP of the endpoint."""
    ip = _ziel_ip(url)
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _KeineWeiterleitung, _IPHandler(ip), _IPSHandler(ip))


def _kopf():
    key = zugang.geheimnis(zugang.ERKENNUNG_KEY)
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if key:
        h["Authorization"] = "Bearer " + key
    return h


def _umgeleitet(url, status):
    return ErkennungFehler("bad_answer", "The model endpoint %s answered with a redirect (HTTP %d); Trove does not follow redirects." % (_basis(url), status))


def _http(methode, url, body=None, timeout=10):
    headers = _kopf()
    if http_quelle is not None:
        st, d = http_quelle(methode, url, body, headers, timeout)
    else:
        daten = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=daten, headers=headers, method=methode)
        try:
            with oeffner(url).open(req, timeout=timeout) as h:
                roh, st = h.read(), h.status
            try:
                d = json.loads(roh.decode("utf-8", "replace"))
            except ValueError:
                d = None
        except urllib.error.HTTPError as ex:
            st, d = ex.code, None
        except (urllib.error.URLError, OSError, TimeoutError) as ex:
            raise ErkennungFehler("unreachable", "The recognition model endpoint %s cannot be reached (%s)." % (_basis(url), zugang.fehlertext(ex, 120)))
    if isinstance(st, int) and 300 <= st < 400:
        raise _umgeleitet(url, st)
    return st, d


def _basis(url):
    m = re.match(r"^(https?://[^/]+)", url)
    return m.group(1) if m else "?"


def lokal_pruefen(k):
    """Which API the endpoint speaks, whether the exact model id is there, its digest (Ollama) and image support."""
    ep = k["endpoint"]
    st, d = _http("GET", ep + "/api/tags", timeout=10)
    if st == 200 and isinstance(d, dict) and isinstance(d.get("models"), list):
        for m in d["models"]:
            if isinstance(m, dict) and k["modell"] in (m.get("name"), m.get("model")):
                caps = [str(c) for c in (m.get("capabilities") or [])]
                if not caps:  # older Ollama: capabilities only in /api/show
                    st2, d2 = _http("POST", ep + "/api/show", {"model": k["modell"]}, timeout=20)
                    caps = [str(c) for c in ((d2 or {}).get("capabilities") or [])] if st2 == 200 and isinstance(d2, dict) else []
                return {"api": "ollama", "digest": str(m.get("digest") or "") or None, "vision": "vision" in caps,
                        "thinking": "thinking" in caps, "groesse": m.get("size")}
        namen = {str(m.get("name")) for m in d["models"] if isinstance(m, dict)}
        aehnlich = k["modell"] + ":latest" if k["modell"] + ":latest" in namen else ""
        raise ErkennungFehler("missing", "Model '%s' is not installed at %s. Trove does not use another model; install it or change the pinned id%s."
                              % (k["modell"], _basis(ep), (" (the exact id is '%s')" % aehnlich) if aehnlich else ""))
    st, d = _http("GET", ep + "/v1/models", timeout=10)
    if st == 200 and isinstance(d, dict):
        ids = [str(x.get("id") or x.get("name") or "") for x in (d.get("data") or d.get("models") or []) if isinstance(x, dict)]
        if k["modell"] in ids:
            return {"api": "openai", "digest": None, "vision": False, "thinking": False, "groesse": None}
        raise ErkennungFehler("missing", "Model '%s' is not offered at %s. Trove does not use another model." % (k["modell"], _basis(ep)))
    if st in (401, 403):
        raise ErkennungFehler("unreachable", "The recognition model endpoint %s refused access (HTTP %d): set TROVE_ERKENNUNG_API_KEY in Hermes." % (_basis(ep), st))
    raise ErkennungFehler("unreachable", "No Ollama or OpenAI-compatible model list at %s (HTTP %s)." % (_basis(ep), st))


def modell_pruefen(geantwortet, gepinnt):
    """The answer must name exactly the pinned model; an answer without a model name is refused too."""
    g = str(geantwortet or "").strip()
    if not g:
        raise ErkennungFehler("other_model", "The endpoint did not say which model answered (pinned is '%s'). Answer discarded." % gepinnt)
    if g != gepinnt:
        raise ErkennungFehler("other_model", "The endpoint answered with model '%s', pinned is '%s'. Answer discarded." % (g[:80], gepinnt))


def _lokal_fragen(k, info, text):
    ep = k["endpoint"]
    inhalt = "Receipt text:\n" + text[:MAX_TEXT]
    if info["api"] == "ollama":
        body = {"model": k["modell"], "stream": False, "format": SCHEMA, "options": {"temperature": 0, "num_predict": 800},
                "keep_alive": keep_alive(k), "messages": [{"role": "system", "content": ANWEISUNG}, {"role": "user", "content": inhalt}]}
        if info.get("thinking"):
            body["think"] = False
        st, d = _http("POST", ep + "/api/chat", body, timeout=ZEIT_S)
        if st != 200 or not isinstance(d, dict):
            raise ErkennungFehler("bad_answer", "Recognition model endpoint answered HTTP %s." % st)
        modell_pruefen(d.get("model"), k["modell"])
        antwort = str(((d.get("message") or {}).get("content")) or "")
    else:
        body = {"model": k["modell"], "temperature": 0, "max_tokens": 800, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": ANWEISUNG}, {"role": "user", "content": inhalt}]}
        st, d = _http("POST", ep + "/v1/chat/completions", body, timeout=ZEIT_S)
        if st != 200 or not isinstance(d, dict):
            raise ErkennungFehler("bad_answer", "Recognition model endpoint answered HTTP %s." % st)
        modell_pruefen(d.get("model"), k["modell"])
        try:
            antwort = str(d["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            raise ErkennungFehler("bad_answer", "Recognition model answer has no message.")
    return _json_aus(antwort)


# ---------- provider through Hermes ----------

def _llm():
    try:
        from agent.plugin_llm import PluginLlm
    except ImportError:
        raise ErkennungFehler("no_hermes", "Provider mode works only inside Hermes (it calls the provider configured there).")
    return PluginLlm(plugin_id=zugang.PLUGIN_ID)


def _provider_fragen(k, text):
    llm = _llm()
    try:
        r = llm.complete_structured(instructions=ANWEISUNG, input=[{"type": "text", "text": "Receipt text:\n" + text[:MAX_TEXT]}], json_mode=True,
                                    json_schema=SCHEMA, schema_name="trove_receipt", provider=k["provider"] or None, model=k["modell"],
                                    temperature=0, max_tokens=800, timeout=ZEIT_S, purpose="trove receipt field extraction")
    except PermissionError:  # PluginLlmTrustError: fixed text, Hermes' wording is not passed on
        raise ErkennungFehler("not_allowed", HERMES_VERBOT)
    except Exception as ex:
        raise ErkennungFehler("unreachable", "The provider call through Hermes failed (%s)." % zugang.fehlertext(ex, 120))
    modell = str(getattr(r, "model", "") or "")
    if not modell.strip() or modell.strip().lower() != k["modell"].lower():
        raise ErkennungFehler("other_model", "Hermes answered with model '%s', pinned is '%s'. Answer discarded." % (modell[:80], k["modell"]))
    prov = str(getattr(r, "provider", "") or "")
    if k["provider"] and prov.strip().lower() != k["provider"].lower():
        raise ErkennungFehler("other_model", "Hermes routed to provider '%s', pinned is '%s'. Answer discarded." % (prov[:40], k["provider"]))
    parsed = getattr(r, "parsed", None)
    return parsed if isinstance(parsed, dict) else _json_aus(str(getattr(r, "text", "") or ""))


def _json_aus(antwort):
    t = re.sub(r"^```(?:json)?|```$", "", antwort.strip(), flags=re.M).strip()
    a, e = t.find("{"), t.rfind("}")
    if a < 0 or e <= a:
        raise ErkennungFehler("bad_answer", "The recognition model did not return JSON.")
    try:
        d = json.loads(t[a:e + 1])
    except ValueError:
        raise ErkennungFehler("bad_answer", "The recognition model returned invalid JSON.")
    if not isinstance(d, dict):
        raise ErkennungFehler("bad_answer", "The recognition model did not return a JSON object.")
    return d


# ---------- resolve the pinned model ----------

def aufloesen(k, pin_pruefen=True):
    """Check settings and the pinned model. Returns info (api, digest). Raises ErkennungFehler."""
    _pruefen(k)
    if k["modus"] == "local":
        info = lokal_pruefen(k)
    else:
        info = {"api": "hermes", "digest": None, "vision": False, "thinking": False, "groesse": None}
    if pin_pruefen:
        pin = pin_lesen()
        if not _pin_passt(k, pin):
            raise ErkennungFehler("not_pinned", "Model '%s' is not pinned yet: press 'Test model' (or 'Pin') under Integrations in the Trove tab."
                                  % k["modell"])
        if info["api"] == "ollama" and pin.get("digest") != info["digest"]:
            raise ErkennungFehler("digest_changed", "Model '%s' has changed (digest now %s, pinned %s). Trove refuses to use it; check the "
                                                    "new version, then press 'Pin' in the Trove tab." % (k["modell"], (info["digest"] or "?")[:12], (pin.get("digest") or "?")[:12]))
    return info


def fragen(k, info, text):
    if k["modus"] == "local":
        return _lokal_fragen(k, info, text)
    return _provider_fragen(k, text)


# ---------- anti-hallucination: values must be in the source text ----------

def _lt():
    import lese_treppe
    return lese_treppe


_NICHT_BETRAG = re.compile(r"(?<!\d)(?:\d{1,2}\s?[./-]\s?\d{1,2}\s?[./-]\s?(?:\d{4}|\d{2})|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}:\d{2}(?::\d{2})?"
                          r"|(?:0?[1-9]|[12]\d|3[01])\.(?:0?[1-9]|1[0-2])\.(?![\d,]))(?!\d)")  # also a date without year: 15.01.
_FORMATE = {
    "de": [re.compile(r"\d{1,3}(?:\.\d{3})+,\d{2}|\d+,\d{2}"), re.compile(r"\d{1,3}(?:[   ']\d{3})+,\d{2}")],
    "en": [re.compile(r"\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2}"), re.compile(r"\d{1,3}(?:[   ']\d{3})+\.\d{2}")],
}
_GANZ = {"de": re.compile(r"\d{1,3}(?:\.\d{3})+|\d+"), "en": re.compile(r"\d{1,3}(?:,\d{3})+|\d+")}
_WAEHRUNG_VOR = re.compile(r"(?:(?<![A-Za-z])(?:EUR|USD|ARS) ?|[€$] ?)$")
_WAEHRUNG_NACH = re.compile(r"^(?: ?(?:€|\$)| ?(?:EUR|USD|ARS)(?![A-Za-z0-9\-/])|,-(?![\d]))")
_ZAEHL_KONTEXT = re.compile(r"(?:pos|nr|no|art|menge|stk|st\xfcck|qty|anz|anzahl|kto|konto|jahr|year|tel|fax)\.?\s*[:#]?\s*$", re.I)


def _ohne_nichtbetraege(text):
    """Dates and times become blanks of the same length (positions stay)."""
    return _NICHT_BETRAG.sub(lambda m: " " * len(m.group(0)), text)


def _frei(t, a, e):
    """A number token stands on its own: not part of a word, an ID (RE-2026-77, 12/34), a longer number or a percentage."""
    vor = t[a - 1] if a > 0 else " "
    if vor == "-":
        vor = t[a - 2] if a > 1 else " "
        if vor.isalnum():
            return False
    elif vor.isalnum() or vor in "_/.,":
        return False
    nach = t[e] if e < len(t) else " "
    if nach.isalnum() or nach in "_/-":
        return False
    if nach in ".," and e + 1 < len(t) and t[e + 1].isdigit():
        return False
    return not re.match(r"\s?%", t[e:e + 2])


def _wert(tok, art):
    s = re.sub(r"[   ']", "", tok)
    s = s.replace(".", "").replace(",", ".") if art == "de" else s.replace(",", "")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def zahlformat(text):
    """The document's number format: ("de",) for 1.234,56, ("en",) for 1,234.56; () without decimal amounts or when both formats
    occur equally often (ambiguous: then no amount is accepted)."""
    t = _ohne_nichtbetraege(text)
    n = {art: sum(1 for m in _FORMATE[art][0].finditer(t) if _frei(t, m.start(), m.end())) for art in _FORMATE}
    if n["de"] == n["en"]:
        return ()
    return ("de",) if n["de"] > n["en"] else ("en",)


def _ganzzahl_betrag(t, a, e):
    """A whole number counts as an amount only with a currency directly attached (12 EUR, 12€, € 12, 49,-), not after a counter or
    reference word (Pos., Nr., Art., Menge, Konto ...) and not when it looks like a year."""
    tok = t[a:e]
    if re.fullmatch(r"(?:19|20)\d\d", tok):
        return False
    if _ZAEHL_KONTEXT.search(t[max(0, a - 12):a]):
        return False
    vor, nach = t[max(0, a - 4):a], t[e:e + 5]
    return bool(_WAEHRUNG_VOR.search(vor) or _WAEHRUNG_NACH.match(nach))


def zahlen_im_text(text):
    """Every amount in the text, read in the document's own format (an ambiguous format gives no amounts). Decimal amounts need
    exactly 2 decimals; whole amounts count only with a directly attached currency. Dates, times, percentages and numbers inside IDs
    are not amounts."""
    t = _ohne_nichtbetraege(text)
    werte = set()
    formate = zahlformat(text)
    for art in formate:
        for rx in _FORMATE[art]:
            for m in rx.finditer(t):
                if _frei(t, m.start(), m.end()) and not _ZAEHL_KONTEXT.search(t[max(0, m.start() - 12):m.start()]):  # Art.-Nr. 4711,00
                    v = _wert(m.group(0), art)
                    if v is not None:
                        werte.add(abs(v))
    for m in re.finditer(r"\d+", t):  # whole amounts: no thousands separators at all (conservative)
        a, e = m.start(), m.end()
        strich = t[e:e + 2] == ",-"
        if not strich and not _frei(t, a, e):
            continue
        if not strich and e < len(t) and t[e] in ".,":
            continue
        if a > 0 and (t[a - 1].isalnum() or t[a - 1] in "_/.,-"):
            continue
        if _ganzzahl_betrag(t, a, e):
            werte.add(float(m.group(0)))
    return werte


def saetze_im_text(text):
    return {round(float(m.group(1).replace(",", ".")), 2) for m in re.finditer(r"(?<![\d.,])(\d{1,2}(?:[.,]\d{1,2})?)\s?%", text)}


def _als_token(wert, text, genau=False):
    """wert occurs as a whole token, not inside a longer word or number. genau: as written (spaces flexible); otherwise character by
    character with optional separators (DE 123 456 789, AR CUIT with or without dashes)."""
    if genau:
        kern = r"\s+".join(re.escape(w) for w in wert.split())
    else:
        zeichen = [re.escape(c) for c in re.sub(r"[\s\-./]", "", wert)]
        if not zeichen:
            return False
        kern = r"[\s\-./]?".join(zeichen)
    if genau:  # a token is the whole run of letters, digits and - / . _ (RE-2026-0417 is one token; 2026 alone is not in it)
        return re.search(r"(?<![A-Za-z0-9\-/._])" + kern + r"(?![A-Za-z0-9\-/_]|\.[A-Za-z0-9])", text, re.I) is not None
    return re.search(r"(?<![A-Za-z0-9])" + kern + r"(?![A-Za-z0-9])", text, re.I) is not None


def datum_im_text(iso, text):
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    if not m:
        return False
    y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
    D = "(?:%d|%02d)" % (d, d)
    M = "(?:%d|%02d)" % (mo, mo)
    Y = "(?:%s|%s)" % (y, y[2:])
    sep = r"\s*[./\-]\s*"
    muster = [r"(?<!\d)%s%s%s%s%s(?!\d)" % (D, sep, M, sep, Y), r"(?<!\d)%s%s%s%s%s(?!\d)" % (M, sep, D, sep, Y),
              r"(?<!\d)%s%s%s%s%s(?!\d)" % (y, sep, M, sep, D), r"(?<!\d)%s%02d%02d(?!\d)" % (y, mo, d)]
    namen = [n for n, v in _lt().MONATE.items() if v == mo]
    if namen:
        N = "(?:%s)[a-zäéè]*\\.?" % "|".join(re.escape(n) for n in namen)
        muster += [r"(?<!\d)%s\.?\s*(?:de\s+)?%s,?\s*(?:de\s+|del\s+)?%s(?!\d)" % (D, N, y), r"%s\s+%s,?\s+%s(?!\d)" % (N, D, y)]
    return any(re.search(p, text, re.I) for p in muster)


WAEHRUNG_ZEICHEN = {"EUR": ("€",), "USD": ("US$", "U$S", "USD"), "ARS": ("$", "ARS")}


def normalisieren(roh):
    """Model answer -> typed field values (unknown keys and unusable values dropped). Returns (werte, verworfen)."""
    werte, verworfen = {}, []
    for k in FELDER:
        v = roh.get(k)
        if v is None or v == "":
            continue
        if k in BETRAEGE or k == "satz":
            if isinstance(v, bool):
                verworfen.append({"feld": k, "grund": "not a number"}); continue
            z = round(float(v), 2) if isinstance(v, (int, float)) else _lt()._zahl(str(v).replace("%", "").replace("$", "").replace("€", ""))
            if z is None:
                verworfen.append({"feld": k, "grund": "not a number"}); continue
            if k == "satz" and 0 < z < 1:
                z = round(z * 100, 2)
            werte[k] = z
            continue
        s = " ".join(str(v).split())[:200]
        grund = None
        if k == "datum" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            grund = "not a date (YYYY-MM-DD)"
        elif k == "waehrung":
            s = s.upper()
            grund = None if re.fullmatch(r"[A-Z]{3}", s) else "not an ISO currency code"
        elif k == "cuit":
            z = re.sub(r"\D", "", s)
            grund = None if len(z) == 11 else "not 11 digits"
            s = "%s-%s-%s" % (z[:2], z[2:10], z[10:]) if not grund else s
        elif k == "ust_id":
            s = re.sub(r"\s", "", s).upper()
            grund = None if re.fullmatch(r"DE\d{9}", s) else "not DE and 9 digits"
        elif k == "iban":
            s = re.sub(r"\s", "", s).upper()
            grund = None if re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", s) else "not a complete IBAN"
        elif k == "rechnungsnummer":
            grund = None if len(re.sub(r"\s", "", s)) >= 3 and re.search(r"\d", s) else "too short or without a digit"
        elif k == "aussteller":
            grund = None if len(s) >= 3 else "too short"
        if grund:
            verworfen.append({"feld": k, "wert": s, "grund": grund}); continue
        werte[k] = s
    return werte, verworfen


def im_text(feld, wert, text, zahlen):
    """Does the value occur in the source text, as what it claims to be?"""
    if feld in BETRAEGE:
        return any(abs(abs(wert) - z) < 0.005 for z in zahlen)
    if feld == "satz":
        return any(abs(wert - z) < 0.005 for z in saetze_im_text(text))
    if feld == "datum":
        return datum_im_text(wert, text)
    if feld in ("cuit", "ust_id", "iban"):
        return _als_token(wert, text)
    if feld == "rechnungsnummer":
        return _als_token(wert, text, genau=True)
    if feld == "aussteller":
        woerter = [re.escape(w) for w in wert.split()]
        return re.search(r"(?<![\w])" + r"\s+".join(woerter) + r"(?![\w])", text, re.I) is not None
    if feld == "waehrung":
        if re.search(r"\b%s\b" % wert, text):
            return True
        return any(z in text for z in WAEHRUNG_ZEICHEN.get(wert, ()))
    return False


def gegen_text(werte, text):
    """Keep only values that occur in the text; everything else is dropped and reported."""
    zahlen = zahlen_im_text(text)
    gut, verworfen = {}, []
    for f, w in werte.items():
        if im_text(f, w, text, zahlen):
            gut[f] = w
        else:
            verworfen.append({"feld": f, "wert": w, "grund": "not in the source text"})
    return gut, verworfen


# ---------- one receipt ----------

def _text_quelle(methode, ocr):
    if ocr and ocr.get("status") == "ocr":
        return "ocr model %s@%s" % (ocr.get("modell"), (ocr.get("digest") or "?")[:12])
    if ocr and ocr.get("status") == "second_reading_used":
        return "%s, replaced by second reading of ocr model %s@%s" % (methode or "?", ocr.get("modell"), (ocr.get("digest") or "?")[:12])
    t = methode or "?"
    if ocr and ocr.get("status") == "second_reading":
        t += "; second reading: ocr model %s@%s" % (ocr.get("modell"), (ocr.get("digest") or "?")[:12])
    if ocr and ocr.get("status") == REGELN_GENUTZT:
        t += " (ocr model unavailable)"
    return t


def gelesen_von(bericht, ocr=None, methode=None):
    """For clients (MCP field read_by): where the text came from and which fields came from which model. "rules" when no model is set."""
    if not bericht and not ocr:
        return "rules"
    teile = ["text: " + _text_quelle(methode, ocr)]
    if not bericht:
        teile.append("fields: rules")
    elif bericht.get("status") == REGELN_GENUTZT:
        teile.append("fields: rules (field model unavailable)")
    elif bericht.get("status") == "rules":
        teile.append("fields: rules (%s)" % (bericht.get("grund") or "model values dropped"))
    else:
        ziel = ("local:" if bericht.get("modus") == "local" else "provider:" + (bericht["provider"] + "/" if bericht.get("provider") else "")) + bericht.get("modell", "")
        if bericht.get("digest"):
            ziel += "@" + bericht["digest"][:12]
        t = "fields: model %s [%s]; rules [%s]" % (ziel, ", ".join(bericht.get("felder_modell") or []), ", ".join(bericht.get("felder_regeln") or []))
        if bericht.get("status") == "conflict":
            t += "; %s [%s]" % (WIDERSPRUCH, ", ".join(sorted(bericht.get("widerspruch") or {})))
        elif bericht.get("status") == "rules_kept":
            t += " (model proposal failed the rules)"
        teile.append(t)
    return "; ".join(teile)


def erkennen(zwilling, k=None):
    """Apply the field model to a twin dict (text only). Returns (fields, report); report None in mode off.
    With fallback none every failure raises ErkennungFehler; with rules the rule fields stay and the report says why."""
    import verprobung as vp
    k = k or konfiguration()
    if k["modus"] == "off":
        return zwilling.get("felder") or {}, None
    regeln = dict(zwilling.get("felder") or {})
    t0 = time.time()
    bericht = {"modus": k["modus"], "modell": k["modell"], "provider": k["provider"] if k["modus"] == "provider" else "",
               "endpoint": k["endpoint"] if k["modus"] == "local" else "", "digest": None, "status": "",
               "felder_modell": [], "felder_regeln": sorted(regeln), "verworfen": [], "widerspruch": {}, "grund": ""}
    try:
        info = aufloesen(k)
        bericht["digest"] = info.get("digest")
        text = zwilling.get("text") or ""
        if not text.strip():
            raise ErkennungFehler("bad_answer", "No text to read (the field model reads text only).")
        werte, verworfen = normalisieren(fragen(k, info, text))
        werte, verworfen2 = gegen_text(werte, text)
        bericht["verworfen"] = verworfen + verworfen2
    except ErkennungFehler as ex:
        bericht["dauer_s"] = round(time.time() - t0, 3)
        if k.get("fallback") != "rules":
            raise
        bericht.update(status=REGELN_GENUTZT, grund=zugang.kurz(str(ex), 300), art=ex.art)
        return regeln, bericht
    kombiniert = dict(regeln)
    kombiniert.update(werte)
    if "brutto" in werte:
        kombiniert.pop("brutto_quelle", None)
    v_neu, v_alt = vp.verproben(kombiniert), vp.verproben(regeln)
    abweichung = {f: {"regeln": regeln[f], "modell": werte[f]} for f in FELDER if f in werte and regeln.get(f) not in (None, "")
                  and not _gleich(f, regeln[f], werte[f])}
    if not werte:
        final = regeln
        bericht.update(status="rules", grund="model values dropped" if bericht["verworfen"] else "the model proposed no values")
    elif v_alt["bestanden"] and abweichung:
        final = regeln  # never a silent replacement of a passed rule reading, whatever the field
        bericht.update(status="conflict", widerspruch=abweichung,
                       grund="%s: the rule reading passed and the model proposes something else for %s"
                             % (WIDERSPRUCH, ", ".join(sorted(abweichung))))
    elif not v_neu["bestanden"]:
        final = regeln  # a proposal that does not pass the rules never replaces the rule reading
        bericht.update(status="rules_kept", grund="the model's proposal failed the rules (%s)" % "; ".join(v_neu["gruende"]))
    else:
        final = kombiniert
        bericht["status"] = "model"
    bericht["felder_modell"] = sorted(f for f in werte if final is kombiniert and not _gleich(f, regeln.get(f), werte[f]))
    bericht["felder_regeln"] = sorted(f for f in final if f not in bericht["felder_modell"])
    bericht["dauer_s"] = round(time.time() - t0, 3)
    return final, bericht


def _gleich(feld, a, b):
    if a is None or b is None:
        return a is b
    if feld in BETRAEGE or feld == "satz":
        try:
            return abs(float(a) - float(b)) < 0.005
        except (TypeError, ValueError):
            return False
    if feld in ("rechnungsnummer", "cuit", "ust_id", "iban"):
        return _kompakt(a) == _kompakt(b)
    return " ".join(str(a).split()).casefold() == " ".join(str(b).split()).casefold()


def _kompakt(s):
    return re.sub(r"[\s\-./:_]", "", str(s)).upper()


def zwilling_erkennen(pfad, k=None):
    """erkennen() on the twin file: fields, rule fields and the report are written back into the twin. Returns the report."""
    with open(pfad, encoding="utf-8") as f:
        z = json.load(f)
    felder, bericht = erkennen(z, k)
    if bericht is None:
        return None
    z["felder_regeln"] = z.get("felder") or {}
    z["felder"] = felder
    z["erkennung"] = bericht
    with open(pfad + ".neu", "w", encoding="utf-8") as f:
        json.dump(z, f, ensure_ascii=False, indent=1)
    os.replace(pfad + ".neu", pfad)
    return bericht


# ---------- OCR stage (local vision model reads page images) ----------

def ocr_aktiv(k=None):
    return bool((k or konfiguration())["ocr_modell"])


def _ocr_pruefen(k):
    if k["ocr_einsatz"] not in OCR_EINSATZ:
        raise ErkennungFehler("config", "Setting erkennung_ocr_einsatz must be scans or second_reading.")
    endpoint_pruefen(k["endpoint"])
    keep_alive(k)
    if k["fallback"] not in FALLBACKS:
        raise ErkennungFehler("config", "Setting erkennung_fallback must be rules or none.")
    for h in k["ocr_sha256"]:
        if not re.fullmatch(r"[0-9a-f]{64}", h):
            raise ErkennungFehler("config", "Setting erkennung_ocr_sha256 must list sha256 values (64 hex characters), comma-separated.")


_HASHES = {}  # only inside this process: a new process (and every Test/Pin) hashes the files again


def datei_sha256(pfad, neu=False):
    """sha256 of a model file. Remembered only within this process for the same size and modification time; neu=True always reads."""
    import hashlib
    try:
        st = os.stat(pfad)
    except OSError:
        raise ErkennungFehler("missing", "Model file %s is missing." % os.path.basename(pfad))
    probe = hashlib.sha256()
    with open(pfad, "rb") as f:  # cheap fingerprint: first and last MiB (a full hash of a 2 GB file only once per process)
        probe.update(f.read(1 << 20))
        if st.st_size > 2 << 20:
            f.seek(-(1 << 20), os.SEEK_END)
            probe.update(f.read(1 << 20))
    schluessel = (os.path.normcase(os.path.abspath(pfad)), st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino, probe.hexdigest())
    if not neu and schluessel in _HASHES:
        return _HASHES[schluessel]
    h = hashlib.sha256()
    with open(pfad, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    _HASHES[schluessel] = h.hexdigest()
    return _HASHES[schluessel]


def ocr_info_holen(k, neu=False):
    """Model present (exact id), image support, Ollama digest, sha256 of every layer, sha256 of the configured GGUF files."""
    _ocr_pruefen(k)
    ok = dict(k, modell=k["ocr_modell"])
    info = lokal_pruefen(ok)
    if info["api"] != "ollama":
        raise ErkennungFehler("config", "The OCR stage needs an Ollama endpoint (it pins the model digest and layers).")
    if not info.get("vision"):
        raise ErkennungFehler("config", "Model '%s' does not read images; the OCR stage needs a vision model." % k["ocr_modell"])
    st, d = _http("POST", k["endpoint"] + "/api/show", {"model": k["ocr_modell"]}, timeout=30)
    if st != 200 or not isinstance(d, dict):
        raise ErkennungFehler("unreachable", "Ollama did not describe model '%s' (HTTP %s)." % (k["ocr_modell"], st))
    schichten = sorted(set(re.findall(r"sha256-([0-9a-f]{64})", str(d.get("modelfile") or ""))))
    if not schichten:
        raise ErkennungFehler("bad_answer", "Ollama did not list the layers of model '%s'." % k["ocr_modell"])
    dateien = {os.path.basename(p): datei_sha256(p, neu) for p in k["ocr_dateien"]}
    if k["ocr_sha256"]:
        bekannt = set(schichten) | set(dateien.values())
        fehlt = [h[:12] for h in k["ocr_sha256"] if h not in bekannt]
        fremd = [n for n, h in dateien.items() if h not in k["ocr_sha256"]]
        if fehlt or fremd:
            raise ErkennungFehler("digest_changed", "Model '%s' does not match the expected sha256 (%s). Trove refuses to use it."
                                  % (k["ocr_modell"], ", ".join(["missing " + h for h in fehlt] + ["unexpected file " + n for n in fremd])))
    return {**info, "schichten": schichten, "dateien": dateien}


def ocr_aufloesen(k, pin_pruefen=True, neu=False):
    info = ocr_info_holen(k, neu)
    if pin_pruefen:
        pin = konfig.lesen().get(PIN_OCR)
        if not isinstance(pin, dict) or pin.get("modell") != k["ocr_modell"] or pin.get("endpoint") != k["endpoint"]:
            raise ErkennungFehler("not_pinned", "OCR model '%s' is not pinned yet: press 'Test model' (or 'Pin') under Integrations in the Trove tab."
                                  % k["ocr_modell"])
        anders = []
        if pin.get("digest") != info["digest"]:
            anders.append("digest %s, pinned %s" % ((info["digest"] or "?")[:12], (pin.get("digest") or "?")[:12]))
        if sorted(pin.get("schichten") or []) != info["schichten"]:
            anders.append("model layers")
        if (pin.get("dateien") or {}) != info["dateien"]:
            anders.append("GGUF files")
        if anders:
            raise ErkennungFehler("digest_changed", "OCR model '%s' has changed (%s). Trove refuses to use it; check the new version, then "
                                                    "press 'Pin' in the Trove tab." % (k["ocr_modell"], "; ".join(anders)))
    return info


_LAYOUT = re.compile(r"^(?:title|text|image|table|figure|header|footer|list|caption|formula|page_number|sub_title|equation)\s*\[\s*\d+(?:\s*,\s*\d+){3}\s*\]\s*", re.I)


def ocr_text_saeubern(roh):
    """OCR model output -> plain lines: layout tags and coordinates removed, empty/placeholder lines and repeats dropped."""
    out, gesehen = [], set()
    for z in str(roh).splitlines():
        z = re.sub(r"(.{1,16}?)\1{7,}", r"\1", _LAYOUT.sub("", z)).strip()  # a short piece repeated 8+ times stays once
        if not z or z.upper() in ("[NO TEXT]", "[NON-TEXT]"):
            continue
        if z in gesehen:
            continue
        gesehen.add(z)
        out.append(z)
    return "\n".join(out)


_SCHLEIFE = re.compile(r"(.{1,16}?)\1{24,}$", re.S)


def _wiederholt(zeilen, text=""):
    """Generation is looping: the last 12 lines were all seen before, or the text ends in one short piece repeated 25 times."""
    if text and _SCHLEIFE.search(text[-600:]):
        return True
    if len(zeilen) < 24:
        return False
    frueher = set(zeilen[:-12])
    return all(z in frueher for z in zeilen[-12:])


def ocr_seite(k, b64, halten=None):
    """One page image through the pinned OCR model (Ollama /api/chat, streamed; stopped when it starts repeating).
    halten: keep_alive for this call (default: erkennung_halten)."""
    body = {"model": k["ocr_modell"], "stream": True, "options": {"temperature": 0, "num_predict": OCR_TOKEN},
            "keep_alive": keep_alive(k) if halten is None else halten,
            "messages": [{"role": "user", "content": k["ocr_prompt"], "images": [b64]}]}
    url = k["endpoint"] + "/api/chat"
    if http_quelle is not None:
        st, teile = http_quelle("POST", url, body, _kopf(), ZEIT_S)
        if isinstance(st, int) and 300 <= st < 400:
            raise _umgeleitet(url, st)
        if st != 200 or not isinstance(teile, list):
            raise ErkennungFehler("bad_answer", "OCR model endpoint answered HTTP %s." % st)
        stuecke = teile
    else:
        stuecke = _strom(url, body)
    text, zeilen, modell = "", [], ""
    for d in stuecke:
        if not isinstance(d, dict):
            continue
        if d.get("model") not in (None, ""):
            modell_pruefen(d.get("model"), k["ocr_modell"])  # every chunk that names a model must name the pinned one
            modell = str(d["model"])
        text += str((d.get("message") or {}).get("content") or "")
        zeilen = [_LAYOUT.sub("", z).strip() for z in text.splitlines()[:-1]]
        if _wiederholt([z for z in zeilen if z], text):
            break
        if d.get("done"):
            break
    if hasattr(stuecke, "close"):
        stuecke.close()
    modell_pruefen(modell, k["ocr_modell"])
    return ocr_text_saeubern(text)


def _strom(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=_kopf(), method="POST")
    try:
        h = oeffner(url).open(req, timeout=ZEIT_S)
    except urllib.error.HTTPError as ex:
        if 300 <= ex.code < 400:
            raise _umgeleitet(url, ex.code)
        raise ErkennungFehler("bad_answer", "OCR model endpoint answered HTTP %s." % ex.code)
    except (urllib.error.URLError, OSError, TimeoutError) as ex:
        raise ErkennungFehler("unreachable", "The OCR model endpoint %s cannot be reached (%s)." % (_basis(url), zugang.fehlertext(ex, 120)))
    try:
        for zeile in h:
            try:
                yield json.loads(zeile.decode("utf-8", "replace"))
            except ValueError:
                continue
    except (OSError, TimeoutError) as ex:
        raise ErkennungFehler("unreachable", "The OCR model endpoint stopped answering (%s)." % zugang.fehlertext(ex, 120))
    finally:
        h.close()  # closing the connection stops the generation in Ollama


def entladen(k, modell):
    """Unload a model from the GPU now (keep_alive 0); errors are ignored."""
    try:
        _http("POST", k["endpoint"] + "/api/generate", {"model": modell, "keep_alive": 0}, timeout=30)
    except ErkennungFehler:
        pass


def ocr_lesen(k, bilder):
    """Page images -> text with the pinned OCR model. Returns (text, seconds). Between the pages of one read the model stays loaded
    for ZWISCHEN_HALTEN seconds; the last page uses erkennung_halten; after a failure it is unloaded when erkennung_halten is 0."""
    t0 = time.time()
    teile = []
    ende = keep_alive(k)
    try:
        for i, b in enumerate(bilder):
            teile.append(ocr_seite(k, b, ende if i == len(bilder) - 1 else max(ZWISCHEN_HALTEN, ende) if isinstance(ende, int) and ende >= 0 else ende))
    except ErkennungFehler:
        if ende == 0:
            entladen(k, k["ocr_modell"])
        raise
    return "\n\f".join(teile), round(time.time() - t0, 3)


def _ocr_bericht(k, info=None):
    return {"modell": k["ocr_modell"], "einsatz": k["ocr_einsatz"], "digest": (info or {}).get("digest"),
            "schichten": (info or {}).get("schichten"), "dateien": (info or {}).get("dateien"), "status": "", "grund": ""}


def beleg_lesen(treppe, mandant, k=None):
    """Reading with the OCR stage. treppe(extra_args) runs lese_treppe.py and returns its JSON (twin path in "zwilling").
    Returns (erg, ocr_bericht). Without an OCR model this is exactly one plain treppe([]) call. Page images go only to the OCR
    model; the field model reads text."""
    import lese_treppe as lt
    import verprobung as vp
    k = k or konfiguration()
    if not ocr_aktiv(k):
        return treppe([]), None
    bericht = _ocr_bericht(k)
    try:
        info = ocr_aufloesen(k)
        bericht.update(_ocr_bericht(k, info))
    except ErkennungFehler as ex:
        if k["fallback"] == "none":
            raise
        bericht.update(status=REGELN_GENUTZT, grund=zugang.kurz(str(ex), 300), art=ex.art)
        erg = treppe([])
        _zwilling_feld(erg["zwilling"], "ocr_modell", bericht)
        return erg, bericht
    n = str(k["ocr_seiten"])
    if k["ocr_einsatz"] == "scans":
        erg = treppe(["--ohne-ocr", "--seitenbilder", n, "--bild-dpi", str(OCR_DPI)])
        bilder = erg.pop("seitenbilder", None)
        if not erg.get("ocr_ausstehend"):
            bericht["status"] = "not a scan"
            return erg, bericht
        try:
            text, sek = ocr_lesen(k, bilder or [])
        except ErkennungFehler as ex:
            if k["fallback"] == "none":
                try:  # no half-written twin: the pending one (no text yet) goes; the caller logs the failure
                    os.remove(erg["zwilling"])
                except OSError:
                    pass
                raise
            bericht.update(status=REGELN_GENUTZT, grund=zugang.kurz(str(ex), 300), art=ex.art)
            erg = treppe([])
            _zwilling_feld(erg["zwilling"], "ocr_modell", bericht)
            return erg, bericht
        seiten = max(1, len(bilder or []))
        bericht.update(status="ocr", sekunden=sek, s_je_seite=round(sek / seiten, 3), seiten=seiten)
        z = _zwilling_laden(erg["zwilling"])
        z.update(methode="ocr:modell", text=text, felder=lt.felder_aus_text(text), ocr_dauer_s=sek, ocr_s_je_seite=bericht["s_je_seite"],
                 dauer_s=round(float(z.get("dauer_s") or 0) + sek, 3), ocr_modell=bericht)
        z["stufen"] = [s for s in z.get("stufen", []) if s != "ocr:ausstehend"] + ["ocr:modell"]
        z.pop("ocr_ausstehend", None)
        _zwilling_schreiben(erg["zwilling"], z)
        import belegprotokoll as bp
        bp.schreiben(bp.pfad_fuer(mandant), ereignis="gelesen", quelle=z.get("datei", ""),
                     ziel=erg["zwilling"], sha256=z.get("sha256", ""), werkzeug="erkennung.py", mandant=mandant,
                     meldung="ocr:modell %s, %d pages, %.2fs" % (k["ocr_modell"], seiten, sek), methode="ocr:modell", felder=z["felder"])
        erg.update(methode="ocr:modell", felder=z["felder"], dauer_s=z["dauer_s"], text_anfang=text[:1500])
        erg.pop("ocr_ausstehend", None)
        return erg, bericht
    # second reading on every PDF
    erg = treppe(["--bilder-alle", "--seitenbilder", n, "--bild-dpi", str(OCR_DPI)])
    bilder = erg.pop("seitenbilder", None)
    try:
        text, sek = ocr_lesen(k, bilder or [])
    except ErkennungFehler as ex:
        if k["fallback"] == "none":
            raise
        bericht.update(status=REGELN_GENUTZT, grund=zugang.kurz(str(ex), 300), art=ex.art)
        _zwilling_feld(erg["zwilling"], "ocr_modell", bericht)
        return erg, bericht
    seiten = max(1, len(bilder or []))
    zweite = lt.felder_aus_text(text)
    bericht.update(status="second_reading", sekunden=sek, s_je_seite=round(sek / seiten, 3), seiten=seiten, zweite_lesung=zweite)
    z = _zwilling_laden(erg["zwilling"])
    erste = z.get("felder") or {}
    if not vp.verproben(erste, zweite)["bestanden"] and vp.verproben(zweite, erste)["bestanden"]:
        # the first reading fails, the model reading passes the rules AND agrees with the first reading on the gross amount (R7)
        z["felder_erste_lesung"] = erste
        z["felder"] = zweite
        z["zweite_lesung"] = erste
        bericht["status"] = "second_reading_used"
        bericht["grund"] = "the first reading failed the rules, the OCR model reading passed them and agrees on the gross amount"
        erg["felder"] = zweite
    else:
        z["zweite_lesung"] = zweite
    z["text_zweite_lesung"] = text
    z["ocr_modell"] = bericht
    _zwilling_schreiben(erg["zwilling"], z)
    return erg, bericht


def _zwilling_laden(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _zwilling_schreiben(p, z):
    with open(p + ".neu", "w", encoding="utf-8") as f:
        json.dump(z, f, ensure_ascii=False, indent=1)
    os.replace(p + ".neu", p)


def _zwilling_feld(p, name, wert):
    z = _zwilling_laden(p)
    z[name] = wert
    _zwilling_schreiben(p, z)


OCR_PROBE_PNG = "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"


# ---------- human actions (Trove tab only) ----------

def _ocr_pin():
    p = konfig.lesen().get(PIN_OCR)
    return p if isinstance(p, dict) else None


def status():
    """Settings, pins and problems for the tab. No network."""
    k = konfiguration()
    pin, opin = pin_lesen(), _ocr_pin()
    probleme = []
    if k["modus"] != "off":
        try:
            _pruefen(k)
        except ErkennungFehler as ex:
            probleme.append(str(ex))
        if k["modell"] and not _pin_passt(k, pin):
            probleme.append("Field model '%s' is not pinned yet: press 'Test model'." % k["modell"])
    ocr_pin_ok = bool(opin and opin.get("modell") == k["ocr_modell"] and opin.get("endpoint") == k["endpoint"])
    if k["ocr_modell"]:
        try:
            _ocr_pruefen(k)
        except ErkennungFehler as ex:
            probleme.append(str(ex))
        if not ocr_pin_ok:
            probleme.append("OCR model '%s' is not pinned yet: press 'Test model'." % k["ocr_modell"])
    return {"mode": k["modus"], "model": k["modell"], "endpoint": k["endpoint"] if (k["modus"] == "local" or k["ocr_modell"]) else "",
            "provider": k["provider"], "fallback": k["fallback"], "keep_loaded": k["halten"], "external_allowed": k["extern_erlaubt"],
            "pin": pin if _pin_passt(k, pin) else None,
            "ocr": {"model": k["ocr_modell"], "use": k["ocr_einsatz"], "pin": opin if ocr_pin_ok else None} if k["ocr_modell"] else None,
            "problems": probleme}


def _pin_eintrag(k, info, durch):
    return {"modus": k["modus"], "modell": k["modell"], "endpoint": k["endpoint"] if k["modus"] == "local" else "",
            "provider": k["provider"] if k["modus"] == "provider" else "", "digest": info.get("digest"),
            "api": info.get("api"), "zeit": time.strftime("%Y-%m-%dT%H:%M:%S"), "durch": durch}


def _ocr_pin_eintrag(k, info, durch):
    return {"modell": k["ocr_modell"], "endpoint": k["endpoint"], "digest": info["digest"], "schichten": info["schichten"],
            "dateien": info["dateien"], "zeit": time.strftime("%Y-%m-%dT%H:%M:%S"), "durch": durch}


def testen():
    """'Test model': each configured stage reads a built-in sample (a short text / a small picture of it; no receipt content leaves
    the machine). A stage is pinned on its first successful test when nothing is pinned for it yet."""
    k = konfiguration()
    if k["modus"] == "off" and not k["ocr_modell"]:
        raise ErkennungFehler("config", "No recognition model is configured (erkennung_modus = off, erkennung_ocr_modell empty).")
    out = {"ok": True}
    if k["ocr_modell"]:
        t0 = time.time()
        info = ocr_aufloesen(k, pin_pruefen=False, neu=True)
        text = ocr_seite(k, OCR_PROBE_PNG)
        zahlen = zahlen_im_text(text)
        treffer = {f: any(abs(v - z) < 0.005 for z in zahlen) for f, v in PROBE_ERWARTET.items()}
        ok = all(treffer.values())
        opin, gepinnt, hinweis = _ocr_pin(), False, ""
        pin_ok = bool(opin and opin.get("modell") == k["ocr_modell"] and opin.get("endpoint") == k["endpoint"])
        if ok and not pin_ok:
            konfig.eintrag_setzen(PIN_OCR, _ocr_pin_eintrag(k, info, "first successful test"))
            gepinnt = True
        elif pin_ok and (opin.get("digest"), sorted(opin.get("schichten") or []), opin.get("dateien") or {}) != (info["digest"], info["schichten"], info["dateien"]):
            hinweis = "The installed version differs from the pinned one; press 'Pin' to accept it."
        out["ocr"] = {"ok": ok, "model": k["ocr_modell"], "digest": info["digest"], "layers": info["schichten"], "files": info["dateien"],
                      "sample": treffer, "seconds": round(time.time() - t0, 2), "pinned_now": gepinnt, "note": hinweis, "text": text[:300]}
        out["ok"] = out["ok"] and ok
    if k["modus"] != "off":
        t0 = time.time()
        info = aufloesen(k, pin_pruefen=False)
        werte, _ = normalisieren(fragen(k, info, PROBE_TEXT))
        werte, verworfen = gegen_text(werte, PROBE_TEXT)
        treffer = {f: werte.get(f) == v for f, v in PROBE_ERWARTET.items()}
        ok = all(treffer.values())
        pin, gepinnt, hinweis = pin_lesen(), False, ""
        if ok and not _pin_passt(k, pin):
            _pin_schreiben(_pin_eintrag(k, info, "first successful test"))
            gepinnt = True
        elif _pin_passt(k, pin) and info.get("api") == "ollama" and pin.get("digest") != info.get("digest"):
            hinweis = "The installed version differs from the pinned digest; press 'Pin' to accept it."
        out["fields"] = {"ok": ok, "model": k["modell"], "mode": k["modus"], "digest": info.get("digest"),
                         "api": info.get("api"), "sample": treffer, "dropped": verworfen, "seconds": round(time.time() - t0, 2),
                         "pinned_now": gepinnt, "note": hinweis}
        out["ok"] = out["ok"] and ok
    out["status"] = status()
    return out


def pinnen():
    """'Pin': the current version of each configured model (Ollama digest, layers, GGUF files) becomes the only one Trove uses."""
    k = konfiguration()
    if k["modus"] == "off" and not k["ocr_modell"]:
        raise ErkennungFehler("config", "No recognition model is configured (erkennung_modus = off, erkennung_ocr_modell empty).")
    out = {"ok": True}
    if k["ocr_modell"]:
        e = _ocr_pin_eintrag(k, ocr_aufloesen(k, pin_pruefen=False, neu=True), "Pin button")
        konfig.eintrag_setzen(PIN_OCR, e)
        out["ocr_pin"] = e
    if k["modus"] != "off":
        e = _pin_eintrag(k, aufloesen(k, pin_pruefen=False), "Pin button")
        _pin_schreiben(e)
        out["pin"] = e
    out["status"] = status()
    return out
