# Tala VPS: Complete Initial Setup Guide

This guide covers the entire process of taking a brand new AlmaLinux Virtual Private Server (VPS) and configuring it as a secure, permanent Tala peer. It includes acquiring a domain, setting up SSL/TLS encryption, and locking down the firewall.

## Prerequisites
1. **A fresh VPS** running AlmaLinux 8 or 9 (e.g., DigitalOcean, Linode, AWS EC2, Hetzner).
2. **A Domain Name** (e.g., `tala.yourdomain.com`).
3. **SSH Access** to your VPS as `root` or a user with `sudo` privileges.

---

## Step 1: DNS Configuration
Before configuring SSL on the VPS, you must point your domain to the VPS's public IP address.

1. Log into your Domain Registrar (Namecheap, Cloudflare, Route53, etc.).
2. Go to the DNS settings for your domain.
3. Create an **A Record**:
   - **Host/Name**: `tala` (or `@` if using the root domain)
   - **Target/Value**: `<YOUR_VPS_PUBLIC_IP>`
   - **TTL**: Auto or 3600
4. Wait a few minutes for the DNS records to propagate. You can verify this by running `ping tala.yourdomain.com` on your local machine.

---

## Step 2: System Preparation & Firewall
SSH into your VPS, update the system packages, and install EPEL for extra packages:

```bash
sudo dnf update -y
sudo dnf install -y epel-release
```

Set up `firewalld` to allow SSH, HTTP, and HTTPS traffic. We will **not** expose Tala's internal ports directly to the internet; they will be proxied through Nginx.

```bash
sudo systemctl enable --now firewalld
sudo firewall-cmd --permanent --add-service=ssh
sudo firewall-cmd --permanent --add-service=http
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --reload
```

---

## Step 3: Install Tala
Create a dedicated system user for Tala and clone the repository.

```bash
# Create the tala user and directory
sudo useradd -r -m -d /opt/tala tala

# Install system dependencies (Git, Python, etc.)
sudo dnf install -y git python3 python3-pip curl

# Clone the repository
git clone https://github.com/Weavor74/TalaV2.git /opt/tala
sudo chown -R tala:tala /opt/tala
```

Switch to the `tala` user and run the installation script:

```bash
sudo su - tala
cd /opt/tala
./install-linux.sh
```

### Configure the Environment
While still logged in as the `tala` user, configure the `.env` file:

```bash
cp .env.example .env
nano .env
```

Ensure the following variables are set for a secure, local-only bind (Nginx will handle the public traffic):

```env
# Bind locally so the ports are NOT exposed to the public internet
WS_HOST=127.0.0.1
WS_PORT=8765

TALA_CHAIN_BIND_HOST=127.0.0.1
TALA_CHAIN_BIND_PORT=8332

TALA_WALLET_ADDRESS=tala-vps-main
TALA_VPS_ACCEPT_ANY_TALA=1
LOG_LEVEL=INFO

# Disable local inference if this is just a relay node
INFERENCE_MODE=local
VLLM_HOST=127.0.0.1
VLLM_PORT=8000
```
Save and exit (`Ctrl+O`, `Enter`, `Ctrl+X`).

Type `exit` to return to your `sudo` user.

---

## Step 4: Systemd Service
Set Tala to run automatically in the background.

```bash
sudo cp /opt/tala/deploy/tala-vps.service /etc/systemd/system/tala-vps.service
sudo systemctl daemon-reload
sudo systemctl enable --now tala-vps
```

Verify Tala is running:
```bash
sudo systemctl status tala-vps
```

---

## Step 5: Nginx Reverse Proxy
We will use Nginx to listen for public traffic on ports 80 and 443, and securely route it to Tala's internal ports.

Install Nginx:
```bash
sudo dnf install -y nginx
```

Create a new Nginx configuration file for Tala:
```bash
sudo nano /etc/nginx/conf.d/tala.conf
```

Paste the following configuration (replace `tala.yourdomain.com` with your actual domain):

```nginx
server {
    listen 80;
    server_name tala.yourdomain.com;

    # Route WebSocket traffic to Tala's P2P harness
    location /ws/ {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "Upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400; # Keep WebSockets alive
    }

    # Route standard HTTP traffic to Tala's Discovery/Blockchain node
    location / {
        proxy_pass http://127.0.0.1:8332;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Enable the configuration and restart Nginx:
```bash
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl restart nginx
```
*Note: AlmaLinux `nginx` automatically loads files from `conf.d/`, so no symlinks are necessary.*

---

## Step 6: SSL/TLS Encryption with Certbot
Public WebSocket (`ws://`) and HTTP traffic is unencrypted. We will use Let's Encrypt to instantly provide free, auto-renewing SSL certificates.

Install Certbot and the Nginx plugin:
```bash
sudo dnf install -y certbot python3-certbot-nginx
```

Run Certbot to fetch and install the certificate:
```bash
sudo certbot --nginx -d tala.yourdomain.com
```

Certbot will ask for your email and whether you want to redirect HTTP traffic to HTTPS. **Choose option 2 (Redirect)** so all traffic is forced over secure connections.

---

## Step 7: Connecting Your Clients
Your VPS is now fully set up, secured by TLS, and acting as the central relay!

1. Open the Tala client on your desktop.
2. If prompted on first launch, select **"Connect to Tala VPS"**.
3. Use the following connection details:
   - **Endpoint**: `wss://tala.yourdomain.com/ws/`
   - **Wallet Address**: `tala-vps-main` (or whatever you set in `.env`)
   - **Shared Secret**: The secret generated when Tala first started.

### Finding the Shared Secret
If you need to retrieve the VPS's shared secret, you can view the logs or check the vault on the VPS:
```bash
sudo cat /opt/tala/vault/secrets.json
```

---

## Step 8: OTA Updates
Because you cloned the repository via Git, your VPS is ready for Over-The-Air (OTA) updates. 
To update the VPS and all connected clients in the future, simply tell the AI assistant in your Tala chat:
*"Trigger a server update"*

The VPS will pull the latest code, package it, and distribute it securely to your desktop clients over the encrypted WebSocket connection.
