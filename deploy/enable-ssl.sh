#!/bin/bash
# Run on VPS when your domain DNS is pointed to 92.5.23.226
# Usage: bash deploy/enable-ssl.sh barber.example.com
#
# Prerequisites:
#   1. Domain A-record → 92.5.23.226  (wait for DNS propagation)
#   2. Port 80 and 443 open in firewall

set -e
DOMAIN="${1:?Usage: $0 <domain>}"

echo "=== 1. Install certbot ==="
sudo apt-get install -y certbot python3-certbot-nginx

echo "=== 2. Update nginx config for domain ==="
sudo tee /etc/nginx/sites-available/barberapp > /dev/null << EOF
server {
    listen 80;
    server_name $DOMAIN;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    server_name $DOMAIN;

    ssl_certificate     /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    include             /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam         /etc/letsencrypt/ssl-dhparams.pem;

    root /home/ubuntu/barberbot/miniapp/dist;
    index index.html;

    location / {
        try_files \$uri \$uri/ /index.html;
    }

    location /api/ {
        proxy_pass       http://127.0.0.1:8000/api/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 30s;
    }
}
EOF

echo "=== 3. Obtain SSL certificate ==="
sudo certbot certonly --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "admin@$DOMAIN"

echo "=== 4. Reload nginx ==="
sudo nginx -t && sudo systemctl reload nginx

echo "=== 5. Enable MINIAPP in bot ==="
cd /home/ubuntu/barberbot
grep -q "^MINIAPP_ENABLED=" .env && sed -i "s|^MINIAPP_ENABLED=.*|MINIAPP_ENABLED=true|" .env || echo "MINIAPP_ENABLED=true" >> .env
grep -q "^MINIAPP_URL=" .env    && sed -i "s|^MINIAPP_URL=.*|MINIAPP_URL=https://$DOMAIN/|" .env || echo "MINIAPP_URL=https://$DOMAIN/" >> .env
sudo systemctl restart barberbot

echo ""
echo "✅ Done!"
echo "   Mini App: https://$DOMAIN/"
echo "   API:      https://$DOMAIN/api/services"
echo "   Bot:      MINIAPP_ENABLED=true, restart done"
echo ""
echo "   Don't forget to set the WebApp URL in @BotFather:"
echo "   /setmenubutton → https://$DOMAIN/"
