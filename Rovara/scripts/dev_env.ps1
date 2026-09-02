# Load .env into the current PowerShell session.
#
#     . .\scripts\dev_env.ps1          # note the leading dot — it must run in *this* shell
#
# Dot-sourced rather than executed, because a child process cannot set its parent's
# environment. Running it without the leading dot appears to work and sets nothing.
#
# One source of truth. Retyping credentials into each terminal is how the API and the
# worker end up pointed at different databases — which presents as "cases open but never
# progress", with nothing in either log saying why.

$envFile = Join-Path (Split-Path -Parent $PSScriptRoot) '.env'
if (-not (Test-Path $envFile)) {
    Write-Error "no .env at $envFile"
    return
}

$loaded = 0
Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq '' -or $line.StartsWith('#') -or $line -notmatch '=') { return }

    $name, $value = $line -split '=', 2
    $name = $name.Trim()
    $value = $value.Trim().Trim('"').Trim("'")

    # An empty assignment is a placeholder, not a value. Setting it would override a real
    # value already exported in this shell — REVORA_TEST_DATABASE_URL is blank in .env on
    # purpose, and clobbering it would silently repoint the test tier.
    if ($value -eq '') { return }

    Set-Item -Path "Env:$name" -Value $value
    $loaded++
}

Write-Host "loaded $loaded variables from .env" -ForegroundColor Green

# Report the two facts worth confirming before anything else runs, without printing secrets.
$dbHost = if ($env:REVORA_DATABASE_URL -match '@([^/?]+)') { $Matches[1] } else { '(unset)' }
Write-Host "  database : $dbHost"
Write-Host "  role     : $(if ($env:REVORA_ROLE) { $env:REVORA_ROLE } else { '(unset)' })"

$missing = @(
    'REVORA_DATABASE_URL',
    'REVORA_PAYLOAD_ENCRYPTION_KEYS',
    'REVORA_CUSTOMER_KEY_SECRET',
    'REVORA_SESSION_TOKEN_SECRET',
    'REVORA_RAZORPAY_KEY_ID',
    'REVORA_RAZORPAY_KEY_SECRET'
) | Where-Object { -not (Get-Item "Env:$_" -ErrorAction SilentlyContinue) }

if ($missing.Count -gt 0) {
    Write-Host "  MISSING  : $($missing -join ', ')" -ForegroundColor Yellow
    Write-Host "  Each of these fails at the moment it is first needed, not at startup." -ForegroundColor Yellow
}
