param(
    [int]$Port = 8443,
    [string]$BindHost = "127.0.0.1",
    [string]$Cert = $env:SSL_CERTFILE,
    [string]$Key = $env:SSL_KEYFILE
)

# HTTPS launcher for a certificate you control (e.g. Let's Encrypt).
# Pass -Cert/-Key or set SSL_CERTFILE/SSL_KEYFILE. Without them the local
# self-signed development certificate is used.
$root   = "C:\Users\MAMA TECH SOLUTIONS"
$python = Join-Path $root "sim_aggregator\.venv\Scripts\python.exe"

if (-not $Cert) { $Cert = Join-Path $root "sim_aggregator\certs\dev-cert.pem" }
if (-not $Key)  { $Key  = Join-Path $root "sim_aggregator\certs\dev-key.pem" }

if (-not (Test-Path $Cert) -or -not (Test-Path $Key)) {
    throw "Certificate not found: $Cert / $Key"
}
if ($env:APP_ENV -eq "production" -and $Cert -like "*dev-cert.pem") {
    throw "Refusing the self-signed development certificate in production. Pass -Cert/-Key or set SSL_CERTFILE/SSL_KEYFILE."
}

if (-not $env:PUBLIC_BASE_URL) { $env:PUBLIC_BASE_URL = "https://127.0.0.1:$Port" }
$env:COOKIE_SECURE   = "1"
$env:FORCE_HTTPS     = "0"

Set-Location $root
& $python -m uvicorn sim_aggregator.main:app `
    --host $BindHost --port $Port `
    --proxy-headers --forwarded-allow-ips "127.0.0.1,::1" `
    --ssl-keyfile $Key --ssl-certfile $Cert
