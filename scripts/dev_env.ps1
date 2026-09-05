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

# Refuse to run in a child process, loudly.
#
# `powershell -File dev_env.ps1` and `.\scripts\dev_env.ps1` both do the work somewhere the
# calling shell cannot see, so the variables load, the success message prints, and the parent
# shell gets nothing. That failure is invisible: the next command fails with
# "REVORA_DATABASE_URL is not set", which reads like a problem with .env rather than with how
# this script was invoked. When dot-sourced, InvocationName is exactly '.'.
if ($MyInvocation.InvocationName -ne '.') {
    Write-Host ''
    Write-Host 'This script must be DOT-SOURCED, not run.' -ForegroundColor Red
    Write-Host 'A child process cannot set its parent shell''s environment, so running it loads'
    Write-Host 'every variable into a process that then exits. Nothing reaches you.'
    Write-Host ''
    Write-Host '  correct:  . .\scripts\dev_env.ps1' -ForegroundColor Green
    Write-Host '            ^ the leading dot and the space are the whole difference'
    Write-Host ''
    Write-Host '  wrong:    .\scripts\dev_env.ps1'
    Write-Host '  wrong:    powershell -File .\scripts\dev_env.ps1'
    Write-Host ''
    exit 1
}

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
    'REVORA_CUSTOMER_TOKEN_SIGNING_SECRETS',
    'REVORA_SESSION_TOKEN_SECRET',
    'REVORA_RAZORPAY_KEY_ID',
    'REVORA_RAZORPAY_KEY_SECRET'
) | Where-Object { -not (Get-Item "Env:$_" -ErrorAction SilentlyContinue) }

if ($missing.Count -gt 0) {
    Write-Host "  MISSING  : $($missing -join ', ')" -ForegroundColor Yellow
    Write-Host "  Each of these fails at the moment it is first needed, not at startup." -ForegroundColor Yellow
}
