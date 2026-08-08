#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$HOME/.local/lib/token-limits"
BIN="$HOME/.local/bin"
CONFIG="$HOME/.config/token-limits/config.json"
SYSTEMD="$HOME/.config/systemd/user"

mkdir -p "$LIB/token_limits" "$BIN" "$(dirname "$CONFIG")" "$SYSTEMD"
cp "$ROOT"/token_limits/*.py "$LIB/token_limits/"
cat > "$BIN/token-limits-collector" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$LIB\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m token_limits.collector "\$@"
EOF
chmod 755 "$BIN/token-limits-collector"

if [[ ! -e "$CONFIG" ]]; then
    cp "$ROOT/config.example.json" "$CONFIG"
    chmod 600 "$CONFIG"
    sed -i "s|/home/USER|$HOME|g" "$CONFIG"
    echo "Konfiguration angelegt: $CONFIG"
else
    echo "Bestehende Konfiguration bleibt unverändert: $CONFIG"
fi
cp "$ROOT/systemd/token-limits.service" "$ROOT/systemd/token-limits.timer" "$SYSTEMD/"

if kpackagetool6 --type Plasma/Applet --show de.max.tokenlimits >/dev/null 2>&1; then
    kpackagetool6 --type Plasma/Applet --upgrade "$ROOT/package"
else
    kpackagetool6 --type Plasma/Applet --install "$ROOT/package"
fi

systemctl --user daemon-reload
systemctl --user enable --now token-limits.timer
systemctl --user start token-limits.service || true

echo
echo "Token Limits installiert. Füge das Widget 'Token Limits' über Plasma hinzu."
echo "Konten und Schwellen: $CONFIG"
