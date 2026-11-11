param(
    [int]$Port = 8000,
    [string]$PublicUrl = $env:PUBLIC_BASE_URL
)

# Run behind nginx/Caddy that terminates TLS. The proxy must send
# X-Forwarded-Proto, X-Forwarded-For and Host; only 127.0.0.1 is trusted.
if (-not $PublicUrl) {
    throw "Set PUBLIC_BASE_URL (for example https://console.example.com) before starting behind a proxy."
}

$root   = "C:\Users\MAMA TECH SOLUTIONS"
$python = Join-Path $root "sim_aggregator\.venv\Scripts\python.exe"

$env:PUBLIC_BASE_URL = $PublicUrl
$env:COOKIE_SECURE   = "1"
$env:FORCE_HTTPS     = "1"

Set-Location $root
& $python -m uvicorn sim_aggregator.main:app `
    --host 127.0.0.1 --port $Port `
    --proxy-headers --forwarded-allow-ips "127.0.0.1,::1"
