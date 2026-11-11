# Production TLS setup

The app ships with a **self-signed development certificate** in `certs/`. That is
fine for `https://127.0.0.1:8443`, but browsers will warn and it must never be
used for real users.

## Option A — certificate on the app (simple)

1. Obtain a certificate for your hostname (Let's Encrypt example):

   ```bash
   certbot certonly --standalone -d console.example.com
   # /etc/letsencrypt/live/console.example.com/fullchain.pem
   # /etc/letsencrypt/live/console.example.com/privkey.pem
   ```

2. Start the app with those files:

   ```powershell
   $env:APP_ENV = "production"
   .\start_https.ps1 -Port 8443 -Cert C:\certs\fullchain.pem -Key C:\certs\privkey.pem
   ```

   The launcher **refuses the dev certificate when `APP_ENV=production`**.

3. Renewal: `certbot renew` writes new files in place. Restart the app (or reload
   the process) after renewal so uvicorn picks up the new certificate.

4. Keep certificates out of git — `sim_aggregator/.gitignore` already ignores
   `certs/`, `*.pem`, `*.key`, `*.pfx` and `.env`.

## Option B — TLS terminated by nginx (recommended)

Run the app on plain HTTP bound to localhost and let nginx terminate TLS:

```powershell
$env:PUBLIC_BASE_URL = "https://console.example.com"
.\start_behind_proxy.ps1 -Port 8000
```

`start_behind_proxy.ps1` sets `FORCE_HTTPS=1`, `COOKIE_SECURE=1` and starts
uvicorn with `--proxy-headers --forwarded-allow-ips 127.0.0.1`.

nginx example:

```nginx
server {
    listen 80;
    server_name console.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name console.example.com;

    ssl_certificate     /etc/letsencrypt/live/console.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/console.example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host  $host;
    }
}
```

## Verifying

```bash
curl -I https://console.example.com/system/health        # 200 + HSTS
curl -I http://console.example.com/system/health         # 301 to https
```

Cookies are issued as `HttpOnly; Secure; SameSite=Lax`; the app returns 401 for
anonymous API calls and redirects plain HTTP to HTTPS when `FORCE_HTTPS=1`.

## Notes

- `PUBLIC_BASE_URL` must be the public https origin; it is used in emailed
  password-reset links and payment redirects.
- Only trust proxy headers from your proxy: `--forwarded-allow-ips` is limited to
  loopback in both launchers. Do not expose uvicorn directly to the internet.
- `COOKIE_SECURE` defaults to on when the request is HTTPS, so Option A also
  produces secure cookies.
