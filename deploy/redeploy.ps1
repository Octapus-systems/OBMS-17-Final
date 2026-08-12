<#
.SYNOPSIS
  Redeploy OBMS (Odoo 17) to the existing AWS EC2 instance.

.DESCRIPTION
  1. Packages the source into a tarball (excluding .git, venv, node_modules, etc.)
  2. Uploads the tarball to the EC2 instance via SCP
  3. SSHs into the server, extracts, rebuilds Docker images, and restarts containers
  4. The PostgreSQL data volume is PRESERVED - only the Odoo app container is rebuilt.

.PARAMETER KeyPath
  Path to the SSH private key file. Default: ~/.ssh/obms-deploy.pem

.PARAMETER RemoteHost
  EC2 public IP or hostname. Default: 3.111.245.98

.PARAMETER User
  SSH user. Default: ubuntu

.EXAMPLE
  .\deploy\redeploy.ps1
  .\deploy\redeploy.ps1 -KeyPath "C:\keys\obms-deploy.pem"
  .\deploy\redeploy.ps1 -RemoteHost "1.2.3.4" -KeyPath "~/.ssh/new-key.pem"
#>

param(
    [string]$KeyPath = "$env:USERPROFILE\.ssh\obms-deploy.pem",
    [string]$RemoteHost = "3.111.245.98",
    [string]$User = "ubuntu"
)

$ErrorActionPreference = "Stop"

# -- Paths -----------------------------------------------------------
$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$TarballName = "obms.tar.gz"
$TarballPath = Join-Path $env:TEMP $TarballName
$RemoteBase = "/opt/obms"

Write-Host ""
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "        OBMS Redeploy -> $RemoteHost" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

# -- Validate SSH key ------------------------------------------------
if (-not (Test-Path $KeyPath)) {
    Write-Host "ERROR: SSH key not found at '$KeyPath'" -ForegroundColor Red
    Write-Host "  Use -KeyPath to specify the correct path." -ForegroundColor Yellow
    exit 1
}
Write-Host "[1/5] SSH key: $KeyPath" -ForegroundColor Green

# -- Package source --------------------------------------------------
Write-Host "[2/5] Packaging source..." -ForegroundColor Green

# Build exclusion list for tar
$ExcludeArgs = @(
    "--exclude=.git",
    "--exclude=.github",
    "--exclude=venv",
    "--exclude=.venv",
    "--exclude=node_modules",
    "--exclude=filestore",
    "--exclude=sessions",
    "--exclude=__pycache__",
    "--exclude=*.pyc",
    "--exclude=*.log",
    "--exclude=Source"
)

$ParentDir = Split-Path $RepoRoot -Parent
$RelDir = Split-Path $RepoRoot -Leaf

Push-Location $ParentDir
try {
    Write-Host "  Running: tar -czf ... (excluding .git, venv, node_modules, etc.)" -ForegroundColor DarkGray
    $tarArgs = @("-czf", $TarballPath) + $ExcludeArgs + @($RelDir)
    & tar @tarArgs
    if ($LASTEXITCODE -ne 0) { throw "tar failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}

$sizeMB = [math]::Round((Get-Item $TarballPath).Length / 1MB, 1)
Write-Host "  Tarball: $TarballPath ($sizeMB MB)" -ForegroundColor DarkGray

# -- Upload ----------------------------------------------------------
Write-Host "[3/5] Uploading to ${User}@${RemoteHost}..." -ForegroundColor Green
& scp -i $KeyPath -o StrictHostKeyChecking=no $TarballPath "${User}@${RemoteHost}:${RemoteBase}/${TarballName}"
if ($LASTEXITCODE -ne 0) { throw "SCP upload failed" }
Write-Host "  Upload complete." -ForegroundColor DarkGray

# -- Remote rebuild --------------------------------------------------
Write-Host "[4/5] Rebuilding on remote..." -ForegroundColor Green

# The remote script is a single-quoted here-string so PowerShell does NOT
# interpolate any $ signs - they pass through verbatim to bash.
$RemoteScript = @'
set -eux
cd /opt/obms

echo '>>> Extracting source...'
rm -rf src.new
mkdir -p src.new
tar -xzf obms.tar.gz -C src.new --strip-components=1
rm -f obms.tar.gz

# Swap atomically
if [ -d src ]; then
    mv src src.old
fi
mv src.new src

# Wire credentials from deploy.env
if [ -f deploy.env ]; then
    cp deploy.env src/deploy/.env
    DB_PASSWORD=$(grep '^DB_PASSWORD=' deploy.env | cut -d= -f2)
    ADMIN_PASSWD=$(grep '^ADMIN_PASSWD=' deploy.env | cut -d= -f2)
    sed -i "s|^db_password = .*|db_password = ${DB_PASSWORD}|" src/deploy/odoo.conf
    sed -i "s|^admin_passwd = .*|admin_passwd = ${ADMIN_PASSWD}|" src/deploy/odoo.conf
else
    echo 'WARNING: deploy.env not found - keeping placeholder credentials in odoo.conf'
fi

cd src/deploy

# Rebuild only the odoo image (postgres image is pulled, not built)
echo '>>> Building Odoo image...'
docker compose build odoo 2>&1 | tail -30

# Recreate only changed containers; db stays up, data volume preserved
echo '>>> Restarting containers...'
docker compose up -d --force-recreate --no-deps odoo
docker compose up -d nginx

# Cleanup
rm -rf /opt/obms/src.old

echo '>>> Waiting for health...'
sleep 10
docker compose ps
echo '>>> Done!'
'@

& ssh -i $KeyPath -o StrictHostKeyChecking=no "${User}@${RemoteHost}" $RemoteScript
if ($LASTEXITCODE -ne 0) { throw "Remote rebuild failed" }

# -- Cleanup local tarball -------------------------------------------
Write-Host "[5/5] Cleaning up..." -ForegroundColor Green
Remove-Item $TarballPath -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "========================================================" -ForegroundColor Green
Write-Host "  Redeployment complete!" -ForegroundColor Green
Write-Host "  URL: http://$RemoteHost" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Green
Write-Host ""
