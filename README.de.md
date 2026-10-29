<div align="center">

<img src="docs/assets/trove-banner.svg" alt="Trove – der Aufräum-Schreibtisch für dich und deine KI-Agenten" width="100%">

<p>
  <strong>Der Aufräum-Schreibtisch, an dem du und deine KI-Agenten gemeinsam Datei-Chaos ordnen.</strong><br>
  Du siehst, klickst und gibst frei. Die Agenten erledigen die Arbeit. Jeder Schritt ist fälschungssicher protokolliert.
</p>

<p>
  <a href="LICENSE"><img alt="Lizenz: Apache-2.0" src="https://img.shields.io/badge/Lizenz-Apache--2.0-7c6fe0?style=flat-square"></a>
  <img alt="Status: frühe Alpha" src="https://img.shields.io/badge/Status-fr%C3%BChe%20Alpha-f0a35e?style=flat-square">
  <img alt="Hermes-Desktop-Plugin" src="https://img.shields.io/badge/Hermes%20Desktop-Plugin%20%E2%89%A5%200.19-5b4fd0?style=flat-square">
  <img alt="Lokal zuerst" src="https://img.shields.io/badge/lokal-keine%20Telemetrie-3fb68b?style=flat-square">
</p>

<p>
  <a href="README.md">English</a> (Standard) · Deutsch
</p>

</div>

---

Trove legt Ordner nebeneinander auf einen Schreibtisch. Du siehst dir ein Dokument an, prüfst es, vergleichst es und legst es dorthin, wo es hingehört.
Deine Hermes-Agenten helfen an denselben Dateien. Alles, was ein Agent tun soll, beginnt als gesperrte Aufgabenkarte und läuft erst nach deiner Freigabe.

Trove läuft auf deinem Rechner. Deine Dateien bleiben, wo sie sind.

> **Trove fragt nie nach deinen Passwörtern und speichert sie nicht.**
> Es gibt keine Mail-Anmeldung, keine Cloud-Anmeldung und keine Eingabe von API-Schlüsseln. Mail-Anhänge und Dateien aus dem Cloud-Speicher kommen so zu Trove:
> Deine Hermes-Agenten holen sie über die eigenen Verbindungen von Hermes und legen die Dateien in einen Ordner, den Trove beobachtet.

## Sicherheit: was Trove auf deinem Rechner tut

**Passwörter und Verbindungen.** Trove liest keine Zugangsdaten-Dateien und enthält keine Mail-, Cloud-, Signatur- oder Buchhaltungs-Anbindung.
Alte eigenständige Anbindungen, die Zugangsdaten lasen, liegen in [`extras/`](extras/integrations/README.md), außerhalb des Plugins, und gehören nicht zum Katalog-Build.

**Schreibrechte pro Ordner.** Ein hinzugefügter Ordner ist **nur lesbar**. Du schaltest ihn je Ordner (mit Rückfrage) auf „beschreibbar“ und kannst ihn zurückstellen.
- *Kopieren*: wird durch Zurücklesen und sha256-Prüfsumme bestätigt; vorhandene Dateien werden nie überschrieben.
- *Verschieben*: kopiert, prüft und **löscht dann das Original**. Quelle und Ziel müssen beschreibbar sein; es braucht einen zweiten Klick.
- *Abgleichen*: kopiert fehlende Dateien von links nach rechts, überschreibt nie.

**Was Trove außerhalb deiner Ordner schreibt:** den eigenen Datenordner (`TROVE_DATEN`, Standard `~/Trove`: Belegprotokoll, Dateiregister, Quellenliste, Aufgabenliste);
bei „Ins Wissen“ Markdown-Zwillinge unter `<wiki>/../raw/trove/` und Karten unter `<wiki>/quellen/` (Standard-Wiki: `<TROVE_DATEN>/knowledge/wiki`, oder `TROVE_WIKI`)
plus eine Zeile je Lauf in `<wiki>/log.md`; neue Karten haben den Status `to-review` (`TROVE_CARD_STATUS`). Originale werden nie verändert.
Das Häkchen „Remember folder for the continuous run“ trägt den Ordner in die Beobachtungsliste in `trove.json` ein.

**Programme, die Trove startet** (ohne Shell, feste Argumentlisten): die eigenen Hilfsskripte (Lesen, OCR, PDF-Seiten, Wissen) in einem getrennten Python-Prozess;
die `hermes`-Kommandozeile (`hermes kanban …`) für Aufgabenkarten (immer **gesperrt** angelegt); falls vorhanden `qmd update` / `qmd embed`; nur auf Wunsch Stirling-PDF
(`TROVE_STIRLING`, Standard `http://127.0.0.1:8090`) und Ollama (`TROVE_OLLAMA`, Standard `http://127.0.0.1:11434`, nur bei Ollama-basierter OCR; Seitenbilder gehen an diese Adresse);
unter macOS `sips` für HEIC-Bilder.

**Optionaler Installer und Zeitplaner** (nicht Teil einer Katalog-Installation): `python tools/installieren.py` kann eine eigene Python-Umgebung anlegen, `trove.json` schreiben,
`trove` in `plugins.enabled` der Hermes-`config.yaml` eintragen (nach einer Sicherung) und nur mit `--dauerlauf` einen LaunchAgent (macOS) oder eine geplante Aufgabe (Windows) einrichten.
Ohne dass du ihn startest, passiert nichts davon.

**MCP-Server (nur wenn du ein anderes Agentensystem anschließt).** `plugin/dashboard/mcp_server.py` ist ein kleiner MCP-Server. Er startet nur, wenn du einen
MCP-Client (Claude Desktop, Claude Code, Cursor …) selbst so einrichtest; der Client startet ihn als Unterprozess und spricht über stdin/stdout mit ihm.
Er nutzt dieselben Backend-Funktionen und Prüfungen wie der Tab. MCP-Clients dürfen **lesen**. Sie dürfen **keine Dateien ändern**: Kopieren, Verschieben,
Abgleichen und „Ins Wissen“ werden nur zu einem **gesperrten Antrag** in der Aufgabenliste des Trove-Tabs (mit dem Namen des Clients) und laufen erst,
wenn du dort **Release** klickst. Es gibt kein MCP-Werkzeug, das freigibt. Jeder Antrag, jede Ablehnung, jede Freigabe und jedes Ergebnis steht mit dem
Client-Namen im Belegprotokoll; Anträge liegen in `mcp_auftraege.json` im Datenordner. Grenze: Die Freigabe schützt vor dem, was ein Agent *über Trove* tut.
Ein Agent, der zusätzlich freien Shell- oder Dateizugriff auf deinen Rechner hat, kann jedes Werkzeug umgehen, auch Trove.

**Netzwerk und Telemetrie.** Keine Telemetrie, keine Statistik, kein Auto-Update. Trove selbst verbindet sich nur mit den oben genannten lokalen Diensten, falls du sie betreibst.
Trove öffnet keinen Port, außer dem optionalen MCP-HTTP-Zugang auf `127.0.0.1`, wenn du den Server mit `--http` startest.

**Sprache.** Die Oberfläche ist standardmäßig Englisch und über die Plugin-Übersetzung von Hermes auch Deutsch (folgt deiner Hermes-Spracheinstellung). Meldungen des Backends sind englisch.

## Was heute funktioniert

Trove ist in früher Alpha. Eine Tabelle mit allen Funktionen und ihrem Stand steht in der [englischen README](README.md#what-works-today).
Kurz: zwei Spalten mit Vorschau, verifiziertes Kopieren/Verschieben/Abgleichen, Änderungsprüfung, Suche, PDF-Stapel trennen, Aufgaben an Hermes-Agenten (gesperrt bis zur Freigabe),
MCP-Zugang für andere Agentensysteme (gleiche Freigabe), fälschungssicheres Protokoll, Belege lesen (E-Rechnung → PDF-Text → lokale OCR) mit Regelprüfung für Deutschland und Argentinien, „Ins Wissen“ (Zwilling + Karte).

## Schnellstart

**Voraussetzung:** [Hermes Desktop](https://hermes-agent.nousresearch.com/desktop) ab 0.19, Python 3.

**Aus dem Hermes-Plugin-Katalog** (sobald gelistet): `hermes plugins install reraise-trove`, Plugin aktivieren, Hermes neu laden. In der Seitenleiste erscheint **Trove**.

**Aus diesem Repository** (Entwickler/offline):

```bash
git clone https://github.com/Reraise-ai/trove.git
cd trove
python tools/installieren.py
```

Gesundheitsprüfung: `python tools/installieren.py --pruefen` (installiert nichts). Eine bestehende `config.yaml` wird vor jeder Änderung gesichert.

Erste Schritte: **+ Folder** → Ordner wählen (nur lesbar) → Datei anklicken für die Vorschau → **Read** → Dateien markieren → **Into knowledge**.
Zum Kopieren in einen Ordner diesen zuerst auf **Make writable** stellen.

Einstellungen (Umgebungsvariablen, dann `trove.json`, dann Standardwerte) sind in der [englischen README](README.md#configuration) aufgelistet.

## Trove aus anderen Agentensystemen nutzen (MCP)

Claude, Cursor oder jeder andere MCP-Client arbeitet am selben Schreibtisch wie du und deine Hermes-Agenten, mit derselben Freigabe.

- **Lesen, sofort:** `status`, `list_sources`, `list_folder`, `preview`, `search`, `file_trail`, `task_status`, `list_tasks`; dazu `read_receipt` und
  `check_changes` (schreiben nur in Troves eigenen Datenordner und ins Protokoll).
- **Ändern, nur als Antrag:** `request_copy`, `request_move`, `request_sync`, `request_into_knowledge`, `request_agent_task` legen einen **gesperrten Antrag** an.
  Er erscheint im Trove-Tab als `MCP <Client-Name>: <was>`. **Release** führt ihn mit den Ordnerrechten aus, die in dem Moment gelten; **Archive** lehnt ihn ab.
  Der Agent fragt mit `task_status` und der erhaltenen Nummer nach. Anträge in einen nur lesbaren Ordner werden sofort abgelehnt.

**Einrichten.** Python 3.10+ mit den Paketen `mcp` (getestet mit 2.3.0, der Version, die Hermes festlegt) und `fastapi`: `python3 -m pip install mcp fastapi`.
Pfad: `<HERMES_HOME>/plugins/reraise-trove/dashboard/mcp_server.py` (Katalog-Installation) oder `<repo>/plugin/dashboard/mcp_server.py`.

Claude Desktop (`claude_desktop_config.json`, *Einstellungen → Entwickler → Konfiguration bearbeiten*):

```json
{
  "mcpServers": {
    "trove": {
      "command": "python3",
      "args": ["/pfad/zu/trove/plugin/dashboard/mcp_server.py"]
    }
  }
}
```

Claude Code:

```bash
claude mcp add trove -- python3 /pfad/zu/trove/plugin/dashboard/mcp_server.py
```

Unter Windows `python` und einen Pfad wie `C:/pfad/zu/trove/plugin/dashboard/mcp_server.py`. Hast du Datenordner oder Mandant (`TROVE_DATEN`, `TROVE_MANDANT`)
nur in der Hermes-Umgebung geändert, gib dieselben Werte im `env`-Block des Clients mit (oder `--mandant <name>`). Optional: `python3 mcp_server.py --http 8765`
bedient MCP über HTTP unter `http://127.0.0.1:8765/mcp` (nur lokal).

## FAQ

**Ist Trove GoBD-konform?** Trove unterstützt eine ordentliche Ablage: nichts wird überschrieben, jede Bewegung wird protokolliert, das Protokoll ist fälschungssicher.
**Es ist keine zertifizierte Compliance-Lösung** und ersetzt weder deine Verfahrensdokumentation noch die Beratung durch deine Steuerberatung.

**Verlassen meine Daten den Rechner?** Nein. Trove liest und schreibt lokal und hat keine Telemetrie. Verbindungen deiner Agenten liegen außerhalb von Trove.

**Wie kommen Mails und Cloud-Dateien zu Trove?** Über deine Hermes-Agenten: Ein Agent holt Anhänge oder Dateien mit den Hermes-Verbindungen und legt sie in einen Quellordner. Trove braucht nie deine Zugangsdaten.

**Welche Agenten arbeiten mit Trove?** Die Agenten in Hermes (Aufgaben landen gesperrt auf dem Hermes-Kanban-Board) und jeder MCP-Client (Claude Desktop, Claude Code, Cursor …)
über Troves MCP-Server: Er liest frei, jede Änderung wartet als gesperrter Antrag im Trove-Tab auf deine Freigabe.

**Braucht das Lesen von Belegen Cloud oder bezahlte KI-Token?** Nein. Erst eingebettete E-Rechnungsdaten, dann der PDF-Text, zuletzt lokale OCR. Ob ein Beleg besteht, entscheiden feste Regeln, keine KI.

## Mitmachen

Issues und Pull-Requests sind willkommen, auf Englisch oder Deutsch. Code-Kommentare und viele Bezeichner sind deutsch.

## Lizenz

[Apache-2.0](LICENSE)

---

<div align="center">
  <img src="docs/assets/trove-logo.svg" alt="Trove-Logo" width="48"><br>
  <sub>Entwickelt von <a href="https://reraise.ai">Reraise</a></sub>
</div>
