#!/usr/bin/env bash
set -euo pipefail

# --------------------------------------------------
# deploy.sh - Deploy Barber Telegram Bot on Ubuntu VPS
# Run as: bash deploy.sh
# --------------------------------------------------

REPO_URL="https://github.com/askarovsuhrob1996-hub/barberbot.git"
DEPLOY_DIR="/home/ubuntu/barberbot"
SERVICE_NAME="barberbot"

echo "========================================="
echo "  Barber Telegram Bot - Deployment Script"
echo "========================================="
echo ""

# --- Step 1: Install system dependencies ---
echo "[1/6] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip git

# --- Step 2: Clone or update the repository ---
echo "[2/6] Setting up repository..."
if [ -d "$DEPLOY_DIR" ]; then
    echo "  Directory $DEPLOY_DIR already exists."
    read -rp "  Pull latest changes? (y/n): " pull_choice
    if [[ "$pull_choice" =~ ^[Yy]$ ]]; then
        cd "$DEPLOY_DIR"
        git pull
    fi
else
    git clone "$REPO_URL" "$DEPLOY_DIR"
fi

cd "$DEPLOY_DIR"

# --- Step 3: Create .env file ---
echo "[3/6] Configuring environment variables..."
if [ -f "$DEPLOY_DIR/.env" ]; then
    echo "  .env file already exists."
    read -rp "  Overwrite it? (y/n): " overwrite_env
    if [[ ! "$overwrite_env" =~ ^[Yy]$ ]]; then
        echo "  Keeping existing .env file."
    else
        rm "$DEPLOY_DIR/.env"
    fi
fi

if [ ! -f "$DEPLOY_DIR/.env" ]; then
    read -rp "  Enter BOT_TOKEN: " bot_token
    read -rp "  Enter BARBER_CHAT_ID: " barber_chat_id

    if [ -z "$bot_token" ] || [ -z "$barber_chat_id" ]; then
        echo "ERROR: Both BOT_TOKEN and BARBER_CHAT_ID are required."
        exit 1
    fi

    cat > "$DEPLOY_DIR/.env" <<EOF
BOT_TOKEN=${bot_token}
BARBER_CHAT_ID=${barber_chat_id}
EOF
    chmod 600 "$DEPLOY_DIR/.env"
    echo "  .env file created."
fi

# --- Step 4: Create virtual environment and install dependencies ---
echo "[4/6] Setting up Python virtual environment..."
if [ ! -d "$DEPLOY_DIR/venv" ]; then
    python3 -m venv "$DEPLOY_DIR/venv"
fi
"$DEPLOY_DIR/venv/bin/pip" install --upgrade pip -q
"$DEPLOY_DIR/venv/bin/pip" install -r "$DEPLOY_DIR/requirements.txt" -q
echo "  Dependencies installed."

# --- Step 5: Install systemd service ---
echo "[5/6] Installing systemd service..."
sudo cp "$DEPLOY_DIR/barberbot.service" /etc/systemd/system/${SERVICE_NAME}.service
sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}
echo "  Service installed and enabled."

# --- Step 6: Start the service ---
echo "[6/6] Starting the bot..."
sudo systemctl restart ${SERVICE_NAME}
sleep 2

if sudo systemctl is-active --quiet ${SERVICE_NAME}; then
    echo ""
    echo "========================================="
    echo "  Deployment complete. Bot is running."
    echo "========================================="
else
    echo ""
    echo "WARNING: Service may not have started correctly."
    echo "Check logs with: sudo journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
fi

echo ""
echo "Useful commands:"
echo "  Status:       sudo systemctl status ${SERVICE_NAME}"
echo "  Logs:         sudo journalctl -u ${SERVICE_NAME} -f"
echo "  Restart:      sudo systemctl restart ${SERVICE_NAME}"
echo "  Stop:         sudo systemctl stop ${SERVICE_NAME}"
