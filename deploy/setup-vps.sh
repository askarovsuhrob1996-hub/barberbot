#!/bin/bash
# One-time VPS setup: install barberapi service + nginx config
# Run as ubuntu user on the VPS: bash deploy/setup-vps.sh

set -e
cd /home/ubuntu/barberbot

echo "=== 1. Installing barberapi systemd service ==="
sudo cp deploy/barberapi.service /etc/systemd/system/barberapi.service
sudo systemctl daemon-reload
sudo systemctl enable barberapi
sudo systemctl start barberapi
sudo systemctl status barberapi --no-pager

echo ""
echo "=== 2. Installing nginx config ==="
echo "  Edit deploy/nginx-miniapp.conf first — replace 'yourdomain.com' with your actual domain."
echo "  Then run:"
echo "    sudo cp deploy/nginx-miniapp.conf /etc/nginx/sites-available/barberapp"
echo "    sudo ln -sf /etc/nginx/sites-available/barberapp /etc/nginx/sites-enabled/"
echo "    sudo nginx -t && sudo systemctl reload nginx"

echo ""
echo "=== 3. SSL certificate (if not yet set up) ==="
echo "    sudo certbot --nginx -d yourdomain.com"

echo ""
echo "=== 4. Add MINIAPP_URL to .env ==="
echo "    echo 'MINIAPP_URL=https://yourdomain.com/' >> /home/ubuntu/barberbot/.env"
echo "    sudo systemctl restart barberbot"

echo ""
echo "=== Done. barberapi is running on port 8000. ==="
