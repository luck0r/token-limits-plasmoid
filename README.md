# Token Limits – Plasma 6 Widget

KDE-Plasma-Widget für Kubuntu/Ubuntu 26.04 LTS (getestet mit Plasma 6.6), das Session- und Wochenlimits mehrerer KI-Abos anzeigt.

## Funktionen

- Beliebig viele Konten je Anbieter, eindeutig über `provider + alias`
- Anthropic/Claude: 5-Stunden- und 7-Tage-Limit
- OpenAI Codex: von der Subscription gemeldete Rate-Limit-Fenster
- Grok und MiniMax über sichere, frei konfigurierbare `command`- oder `json_url`-Adapter
- Reset-Countdown pro Fenster
- Plasma-Benachrichtigung einmal pro Reset-Zyklus beim konfigurierbaren Schwellenwert, optional mit Ton
- Benachrichtigung, sobald ein zuvor limitiertes Konto wieder verfügbar ist
- Keine Tokens im Widget: Nur der lokale Python-Collector liest Credential-Dateien; das Widget liest eine bereinigte Statusdatei

## Installation

```bash
./install.sh
```

Danach im Plasma-Widget-Dialog **Token Limits** suchen und zum Panel oder Desktop hinzufügen.

## Multi-Account-Konfiguration

Datei: `~/.config/token-limits/config.json`

```json
{
  "notifications": {
    "enabled": true,
    "session": 80,
    "weekly": 80,
    "sound": {
      "enabled": true,
      "file": "/usr/share/sounds/freedesktop/stereo/message.oga",
      "volume": 70,
      "events": ["threshold"]
    }
  },
  "accounts": [
    {
      "provider": "anthropic",
      "alias": "Privat",
      "credential_file": "~/.claude/.credentials.json",
      "thresholds": {"session": 80, "weekly": 85}
    },
    {
      "provider": "anthropic",
      "alias": "Work",
      "credential_file": "~/.claude-work/.credentials.json"
    }
  ]
}
```

Jede Kombination aus Anbieter und Alias muss eindeutig sein. Schwellen unter `thresholds` überschreiben die globalen Werte nur für dieses Konto.

### Zweites Anthropic-Konto anmelden

Claude Code unterstützt getrennte Konfigurationsverzeichnisse. Das Work-Konto kann unabhängig angemeldet werden:

```bash
CLAUDE_CONFIG_DIR="$HOME/.claude-work" claude
```

Im gestarteten Claude-Code-Client `/login` ausführen und das Work-Konto wählen. Danach `"enabled": true` für **Work** setzen. Der private Login unter `~/.claude` bleibt unberührt.

Für getrennte Codex-Logins analog unterschiedliche `CODEX_HOME`-Verzeichnisse verwenden, zum Beispiel `~/.codex` und `~/.codex-work`, und deren jeweilige `auth.json` eintragen.

## Grok und MiniMax

- **Grok Build** wird nativ über `~/.grok/auth.json` unterstützt. Der Client stellt derzeit ein Wochenfenster bereit, aber kein separates Sessionfenster. Abgelaufene OAuth-Access-Tokens werden über den vorhandenen Refresh-Token erneuert. Falls auch dieser abgelaufen ist, einmal `grok login` ausführen.
- **MiniMax Coding Plan** wird nativ über den Coding-Plan-Endpunkt unterstützt. Lege den API-Key allein in eine Datei mit Modus `600` und verweise mit `api_key_file` darauf:

```json
{
  "provider": "minimax",
  "alias": "Work",
  "api_key_file": "~/.config/minimax/api-key",
  "base_url": "https://api.minimax.io"
}
```

Je nach MiniMax-Region kann `base_url` angepasst werden. Der Coding-Plan-Endpunkt liefert das aktuelle Intervall; ein zusätzliches Wochenfenster wird nur angezeigt, wenn der Anbieter es tatsächlich liefert.

Für andere oder geänderte Provider-Endpunkte bleiben `command`- und `json_url`-Adapter verfügbar. Ein Kommando schreibt genau ein kanonisches JSON-Objekt nach stdout:

```json
{
  "available": true,
  "session": {"used_percent": 52, "reset_at": "2026-08-08T18:00:00Z"},
  "weekly": {"used_percent": 71, "reset_at": 1786536000}
}
```

Konfiguration:

```json
{
  "provider": "grok",
  "alias": "Privat",
  "adapter": "command",
  "command": ["/home/max/.local/bin/grok-quota-json"]
}
```

Alternativ kann `adapter: "json_url"` mit `url`, optionalen `headers` und `bearer_token_file` verwendet werden. Secrets gehören niemals direkt in die Config oder die Statusdatei.

## Betrieb und Diagnose

```bash
systemctl --user status token-limits.timer token-limits.service
journalctl --user -u token-limits.service --since today
token-limits-collector --no-notify
```

Status: `~/.local/share/token-limits/status.json`  
Interner Alarmzustand: `~/.local/state/token-limits/state.json`

Nach Änderungen an der Config:

```bash
systemctl --user start token-limits.service
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## API-Hinweis

Die Claude- und Codex-Subscription-Endpunkte sind die Endpunkte, die auch die jeweiligen lokalen Clients verwenden, aber keine langfristig garantierten öffentlichen Billing-APIs. Adapterfehler werden pro Konto im Widget angezeigt, ohne andere Konten zu blockieren.
