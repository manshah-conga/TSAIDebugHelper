# open_aws_port.ps1 -- open the app's port in this EC2 instance's Security Group.
#
# Run this ON the EC2 instance (it reads the instance's own identity from the
# metadata service). Requires the AWS CLI v2 and credentials with
# ec2:DescribeInstances, ec2:DescribeSecurityGroups and
# ec2:AuthorizeSecurityGroupIngress.
#
#   .\open_aws_port.ps1 -SourceIp 203.0.113.45      # allow just that address
#   .\open_aws_port.ps1 -SourceCidr 10.0.0.0/8      # allow a corporate range
#   .\open_aws_port.ps1 -SourceIp 203.0.113.45 -Port 8000 -WhatIf
#
# Find the value for -SourceIp by running this ON YOUR OWN LAPTOP, not here:
#   curl https://checkip.amazonaws.com
# Run from the VM and you get the VM's address, which is not the source of your
# browser traffic and will not let you in.

param(
    [int]$Port = 8000,
    [string]$SourceIp,
    [string]$SourceCidr,
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'

if (-not $SourceIp -and -not $SourceCidr) {
    Write-Error "Give -SourceIp <your.public.ip> or -SourceCidr <range>. Refusing to default to 0.0.0.0/0: this app serves customer org metadata over plain HTTP."
}
if ($SourceIp -and $SourceCidr) {
    Write-Error "Pass only one of -SourceIp / -SourceCidr."
}
$cidr = if ($SourceCidr) { $SourceCidr } else { "$SourceIp/32" }

if ($cidr -eq '0.0.0.0/0') {
    Write-Error "0.0.0.0/0 exposes the app to the whole internet with no TLS. Use your own /32 or a corporate CIDR."
}

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    Write-Error "AWS CLI not found. Install AWS CLI v2 (https://aws.amazon.com/cli/) or use the EC2 console instead -- see README section 1b."
}

# ---- identify this instance via IMDSv2 --------------------------------------

Write-Host "Reading instance identity from the metadata service ..." -ForegroundColor Cyan
try {
    $token = Invoke-RestMethod -Method PUT -Uri 'http://169.254.169.254/latest/api/token' `
        -Headers @{ 'X-aws-ec2-metadata-token-ttl-seconds' = '21600' } -TimeoutSec 5
    $hdr = @{ 'X-aws-ec2-metadata-token' = $token }
    $instanceId = Invoke-RestMethod -Uri 'http://169.254.169.254/latest/meta-data/instance-id' -Headers $hdr -TimeoutSec 5
    $doc        = Invoke-RestMethod -Uri 'http://169.254.169.254/latest/dynamic/instance-identity/document' -Headers $hdr -TimeoutSec 5
    $region     = $doc.region
} catch {
    Write-Error "Could not reach the EC2 metadata service. Run this script on the EC2 instance itself (not your laptop). Underlying error: $($_.Exception.Message)"
}

try {
    $publicIp = Invoke-RestMethod -Uri 'http://169.254.169.254/latest/meta-data/public-ipv4' -Headers $hdr -TimeoutSec 5
} catch {
    $publicIp = $null
}

Write-Host "  instance : $instanceId"
Write-Host "  region   : $region"
Write-Host "  public IP: $(if ($publicIp) { $publicIp } else { '(none -- the instance has no public IP; see note below)' })"

# ---- find its security group(s) ---------------------------------------------

$sgJson = aws ec2 describe-instances --instance-ids $instanceId --region $region `
    --query "Reservations[].Instances[].SecurityGroups[]" --output json
if ($LASTEXITCODE -ne 0) {
    Write-Error "aws ec2 describe-instances failed. Check that credentials are configured ('aws sts get-caller-identity') and that they allow ec2:DescribeInstances."
}
$groups = $sgJson | ConvertFrom-Json

if (-not $groups) { Write-Error "No security groups found for $instanceId." }

Write-Host ""
Write-Host "Security groups attached:" -ForegroundColor Cyan
$groups | ForEach-Object { Write-Host "  $($_.GroupId)  $($_.GroupName)" }

if ($groups.Count -gt 1) {
    Write-Host ""
    Write-Host "More than one group is attached. This script will add the rule to the FIRST one" -ForegroundColor Yellow
    Write-Host "($($groups[0].GroupId)). If that's not the right one, add the rule by hand:" -ForegroundColor Yellow
    Write-Host "  aws ec2 authorize-security-group-ingress --group-id <sg-id> --protocol tcp --port $Port --cidr $cidr --region $region" -ForegroundColor Yellow
}

$groupId = $groups[0].GroupId

# ---- add the ingress rule ----------------------------------------------------

Write-Host ""
Write-Host "Rule to add:" -ForegroundColor Cyan
Write-Host "  group    : $groupId"
Write-Host "  inbound  : TCP $Port from $cidr"

if ($WhatIf) {
    Write-Host ""
    Write-Host "-WhatIf given -- nothing changed." -ForegroundColor Yellow
    exit 0
}

$out = aws ec2 authorize-security-group-ingress `
    --group-id $groupId `
    --protocol tcp `
    --port $Port `
    --cidr $cidr `
    --region $region `
    --output json 2>&1

if ($LASTEXITCODE -ne 0) {
    if ("$out" -match 'InvalidPermission\.Duplicate') {
        Write-Host ""
        Write-Host "That rule already exists -- nothing to do." -ForegroundColor Green
    } else {
        Write-Host "$out" -ForegroundColor Red
        Write-Error "authorize-security-group-ingress failed (see above). A common cause is credentials without ec2:AuthorizeSecurityGroupIngress."
    }
} else {
    Write-Host ""
    Write-Host "Rule added." -ForegroundColor Green
}

# ---- show the resulting rules ------------------------------------------------

Write-Host ""
Write-Host "Inbound rules now on ${groupId}:" -ForegroundColor Cyan
aws ec2 describe-security-groups --group-ids $groupId --region $region `
    --query "SecurityGroups[].IpPermissions[].{Proto:IpProtocol,From:FromPort,To:ToPort,Sources:IpRanges[].CidrIp}" `
    --output table

Write-Host ""
if ($publicIp) {
    Write-Host "Now browse to:  http://${publicIp}:$Port" -ForegroundColor Green
} else {
    Write-Host "This instance has no public IPv4 address, so it is not reachable from the" -ForegroundColor Yellow
    Write-Host "internet regardless of the Security Group. Either attach an Elastic IP, or" -ForegroundColor Yellow
    Write-Host "reach it over your VPN/Direct Connect using its private IP." -ForegroundColor Yellow
}
Write-Host "From your laptop, test the path with:  Test-NetConnection <ip> -Port $Port"
