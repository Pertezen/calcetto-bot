#!/usr/bin/env bash
#
# Prepara una VM Ubuntu appena creata a far girare il bot calcetto.
#
#   bash setup_vm.sh
#
# Fa tutto: dipendenze di sistema, ambiente virtuale, file .env, servizio
# systemd che riparte da solo, backup notturno del database.
# Si può rilanciare quante volte si vuole: non rompe niente di già fatto.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$APP_DIR/.env"
SERVICE="botcalcetto"
RUN_AS="$(id -un)"

echo "▸ Cartella dell'app: $APP_DIR"
echo "▸ Utente di servizio: $RUN_AS"

# --- 1. dipendenze di sistema -------------------------------------------
echo "▸ Installo le dipendenze di sistema…"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip sqlite3

# --- 2. ambiente virtuale ------------------------------------------------
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
    echo "▸ Creo l'ambiente virtuale…"
    python3 -m venv "$APP_DIR/.venv"
fi
echo "▸ Installo le librerie Python…"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# --- 3. configurazione ---------------------------------------------------
if [ -f "$ENV_FILE" ]; then
    echo "▸ .env già presente, lo lascio com'è"
else
    echo
    read -rsp "Incolla il token di BotFather e premi Invio: " TOKEN
    echo
    if [ -z "${TOKEN// }" ]; then
        echo "✗ Token vuoto. Rilancia lo script quando ce l'hai." >&2
        exit 1
    fi
    cat > "$ENV_FILE" <<EOF
TELEGRAM_TOKEN=$TOKEN
DB_PATH=$APP_DIR/calcetto.db
TIMEZONE=Europe/Rome
EOF
    chmod 600 "$ENV_FILE"
    echo "▸ Scritto $ENV_FILE (leggibile solo da te)"
fi

# --- 4. servizio systemd -------------------------------------------------
echo "▸ Installo il servizio $SERVICE…"
sudo tee "/etc/systemd/system/$SERVICE.service" > /dev/null <<EOF
[Unit]
Description=Bot Calcetto Telegram
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_AS
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --quiet "$SERVICE"
sudo systemctl restart "$SERVICE"

# --- 5. backup notturno del database ------------------------------------
mkdir -p "$HOME/backup"
if command -v crontab > /dev/null && ! crontab -l 2>/dev/null | grep -qF "$APP_DIR/calcetto.db"; then
    echo "▸ Aggiungo il backup notturno (7 copie a rotazione)"
    (crontab -l 2>/dev/null; \
     echo "30 4 * * * cp $APP_DIR/calcetto.db $HOME/backup/calcetto-\$(date +\%u).db") \
     | crontab -
fi

# --- 6. verifica ---------------------------------------------------------
echo "▸ Aspetto che il bot si presenti a Telegram…"
sleep 5

if systemctl is-active --quiet "$SERVICE"; then
    echo
    echo "✅ Fatto. Il bot è vivo e resta acceso anche a PC spento."
    echo "   Scrivigli /start su Telegram per confermare."
    echo
    echo "   Log dal vivo:  journalctl -u $SERVICE -f"
    echo "   Riavvio:       sudo systemctl restart $SERVICE"
    echo
    sudo journalctl -u "$SERVICE" -n 5 --no-pager
else
    echo
    echo "✗ Il servizio non è partito. Ultimi log:" >&2
    sudo journalctl -u "$SERVICE" -n 25 --no-pager >&2
    exit 1
fi
