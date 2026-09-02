# enable_ssh_access.ps1 -- prepare a WINDOWS EC2 instance for SSH port-forwarding
# access to this app, so nothing has to be opened in a shared Security Group.
#
# Run this ON THE VM, in an ELEVATED PowerShell (Run as Administrator).
#
#   .\enable_ssh_access.ps1 -PublicKeyPath C:\Users\Administrator\id_ed25519.pub
#   .\enable_ssh_access.ps1 -PublicKey "ssh-ed25519 AAAAC3... me@laptop"
#   .\enable_ssh_access.ps1 -PublicKeyPath ... -RemoveAppFirewallRule -AppPort 8000
#
# Why this is needed: EC2's .pem key is only used to decrypt the Administrator
# password for RDP on Windows instances. Windows has no SSH server running by
# default and no authorized_keys set up, so "ssh -i key.pem user@host" -- which is
# what you'd do on a Linux instance -- fails until both exist. This script
# installs/starts OpenSSH Server and installs your public key with the ACLs
# sshd requires.

param(
    [string]$PublicKeyPath,
    [string]$PublicKey,
    [int]$AppPort = 8000,
    [switch]$RemoveAppFirewallRule
)

$ErrorActionPreference = 'Stop'

# ---- must be elevated --------------------------------------------------------

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error "Run this in an elevated PowerShell (right-click -> Run as Administrator)."
}

# ---- resolve the public key --------------------------------------------------

if (-not $PublicKey -and -not $PublicKeyPath) {
    Write-Error @"
Give the PUBLIC key of the keypair you will connect with, as -PublicKey "<contents>" or -PublicKeyPath <file>.

Generate one on your LAPTOP if you don't have it:
    ssh-keygen -t ed25519 -C "$env:USERNAME@laptop"
then pass the contents of %USERPROFILE%\.ssh\id_ed25519.pub here (the .pub file --
never the private key).

Do not use the EC2 .pem here. On a Windows instance that key only decrypts the
Administrator password for RDP; it is not an SSH credential.
"@
}
if ($PublicKeyPath) {
    if (-not (Test-Path $PublicKeyPath)) { Write-Error "No such file: $PublicKeyPath" }
    $PublicKey = (Get-Content $PublicKeyPath -Raw).Trim()
}
$PublicKey = $PublicKey.Trim()

if ($PublicKey -match 'PRIVATE KEY') {
    Write-Error "That looks like a PRIVATE key. Pass the matching .pub file instead -- the private key must never leave your laptop."
}
if ($PublicKey -notmatch '^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-\S+)\s+\S+') {
    Write-Error "That doesn't look like an OpenSSH public key (expected a line starting 'ssh-ed25519' / 'ssh-rsa' / 'ecdsa-sha2-...')."
}

# ---- install and start OpenSSH Server ----------------------------------------

Write-Host "Checking OpenSSH Server ..." -ForegroundColor Cyan
$cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Select-Object -First 1
if (-not $cap) {
    Write-Error "OpenSSH Server capability not found on this Windows build. Install OpenSSH manually (https://github.com/PowerShell/Win32-OpenSSH/releases) or ask CloudOps for a reverse proxy / ALB instead."
}
if ($cap.State -ne 'Installed') {
    Write-Host "  installing $($cap.Name) ..." -ForegroundColor Cyan
    Add-WindowsCapability -Online -Name $cap.Name | Out-Null
} else {
    Write-Host "  already installed." -ForegroundColor Green
}

Set-Service -Name sshd -StartupType Automatic
if ((Get-Service sshd).Status -ne 'Running') { Start-Service sshd }
Write-Host "  sshd: $((Get-Service sshd).Status), startup $((Get-Service sshd).StartType)" -ForegroundColor Green

# The OpenSSH installer normally adds this rule; create it if it's missing.
if (-not (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue)) {
    Write-Host "  adding Windows Firewall rule for TCP 22 ..." -ForegroundColor Cyan
    New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' `
        -Direction Inbound -Protocol TCP -LocalPort 22 -Action Allow | Out-Null
} else {
    Write-Host "  Windows Firewall rule for TCP 22 present." -ForegroundColor Green
}

# ---- install the public key --------------------------------------------------
#
# For accounts in the Administrators group, sshd reads
# C:\ProgramData\ssh\administrators_authorized_keys -- NOT the user's
# ~\.ssh\authorized_keys -- and refuses the file unless its ACL grants only
# Administrators and SYSTEM. Getting that ACL wrong is the single most common
# cause of "Permission denied (publickey)" on Windows.

$akPath = 'C:\ProgramData\ssh\administrators_authorized_keys'
Write-Host "Installing public key into $akPath ..." -ForegroundColor Cyan

$existing = if (Test-Path $akPath) { Get-Content $akPath } else { @() }
if ($existing -contains $PublicKey) {
    Write-Host "  key already present." -ForegroundColor Green
} else {
    Add-Content -Path $akPath -Value $PublicKey
    Write-Host "  key added." -ForegroundColor Green
}

# Required ACL: no inheritance, Administrators + SYSTEM only.
icacls $akPath /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
Write-Host "  ACL set (Administrators + SYSTEM only)." -ForegroundColor Green

Restart-Service sshd
Write-Host "  sshd restarted." -ForegroundColor Green

# ---- optionally close the app's own port ------------------------------------
#
# With a tunnel, the app should go back to listening on 127.0.0.1 only and its
# inbound firewall rule is no longer needed -- the SSH connection reaches it from
# inside the VM.

if ($RemoveAppFirewallRule) {
    $rule = Get-NetFirewallRule -DisplayName "TS Debug Helper $AppPort" -ErrorAction SilentlyContinue
    if ($rule) {
        Remove-NetFirewallRule -DisplayName "TS Debug Helper $AppPort"
        Write-Host "Removed Windows Firewall rule 'TS Debug Helper $AppPort' -- not needed with a tunnel." -ForegroundColor Green
    } else {
        Write-Host "No 'TS Debug Helper $AppPort' firewall rule to remove." -ForegroundColor DarkGray
    }
}

# ---- report ------------------------------------------------------------------

Write-Host ""
Write-Host "Listeners:" -ForegroundColor Cyan
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in @(22, $AppPort) } |
    Select-Object LocalAddress, LocalPort |
    Sort-Object LocalPort | Format-Table -AutoSize

if (-not (Get-NetTCPConnection -State Listen -LocalPort $AppPort -ErrorAction SilentlyContinue)) {
    Write-Host "Nothing is listening on $AppPort yet -- start the app:" -ForegroundColor Yellow
    Write-Host "    cd <repo>\webapp; .\start_server.ps1 -Port $AppPort" -ForegroundColor Yellow
    Write-Host "(-Listen Local is correct and preferred when using a tunnel.)" -ForegroundColor Yellow
}

$user = "$env:USERNAME"
Write-Host ""
Write-Host "Now, from your laptop while on VPN:" -ForegroundColor Green
Write-Host "    ssh -i <your-private-key> -N -L ${AppPort}:localhost:${AppPort} ${user}@<instance-public-ip>"
Write-Host "then browse to http://localhost:$AppPort"
