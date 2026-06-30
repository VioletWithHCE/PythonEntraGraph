param(
    [string]$TenantDomain,
    [string]$AppId,
    [string]$CertificateFilePath,
    [string]$CertificatePassword,
    [string]$OutputFolder = "./exo_output"
)

$ErrorActionPreference = 'Stop'

function Get-DotEnvMap {
    $dotenvPath = Join-Path (Get-Location) '.env'
    if (-not (Test-Path -LiteralPath $dotenvPath)) {
        return @{}
    }

    $map = @{}
    foreach ($line in Get-Content -LiteralPath $dotenvPath) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }

        $idx = $trimmed.IndexOf('=')
        if ($idx -lt 1) { continue }

        $key = $trimmed.Substring(0, $idx).Trim()
        $value = $trimmed.Substring($idx + 1).Trim()

        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }

        $map[$key] = $value
    }

    return $map
}

function First-Value {
    param([string[]]$Candidates)
    foreach ($candidate in $Candidates) {
        if (-not [string]::IsNullOrWhiteSpace($candidate)) {
            return $candidate
        }
    }
    return $null
}

function Resolve-RecipientValue {
    param([object]$Value)

    if ($null -eq $Value) { return $null }
    if ($Value -is [string]) { return $Value }
    if ($Value.PSObject.Properties['PrimarySmtpAddress'] -and $Value.PrimarySmtpAddress) { return $Value.PrimarySmtpAddress.ToString() }
    if ($Value.PSObject.Properties['WindowsEmailAddress'] -and $Value.WindowsEmailAddress) { return $Value.WindowsEmailAddress.ToString() }
    if ($Value.PSObject.Properties['ExternalEmailAddress'] -and $Value.ExternalEmailAddress) { return $Value.ExternalEmailAddress.ToString() }
    if ($Value.PSObject.Properties['UserPrincipalName'] -and $Value.UserPrincipalName) { return $Value.UserPrincipalName.ToString() }
    if ($Value.PSObject.Properties['Name'] -and $Value.Name) { return $Value.Name.ToString() }
    return $Value.ToString()
}

if (-not (Get-Module -ListAvailable -Name ExchangeOnlineManagement)) {
    throw "ExchangeOnlineManagement module is not installed. Run: Install-Module ExchangeOnlineManagement -Scope CurrentUser"
}

$dotenv = Get-DotEnvMap

$TenantDomain = First-Value @(
    $TenantDomain,
    $env:EXO_TENANT_DOMAIN,
    $env:TENANT_DOMAIN,
    $dotenv['EXO_TENANT_DOMAIN'],
    $dotenv['TENANT_DOMAIN']
)

$AppId = First-Value @(
    $AppId,
    $env:EXO_APP_ID,
    $env:CLIENT_ID,
    $dotenv['EXO_APP_ID'],
    $dotenv['CLIENT_ID']
)

$CertificateFilePath = First-Value @(
    $CertificateFilePath,
    $env:EXO_CERT_FILE,
    $dotenv['EXO_CERT_FILE']
)

$CertificatePassword = First-Value @(
    $CertificatePassword,
    $env:EXO_CERT_PASSWORD,
    $dotenv['EXO_CERT_PASSWORD']
)

$OutputFolder = First-Value @(
    $OutputFolder,
    $env:EXO_OUTPUT_DIR,
    $dotenv['EXO_OUTPUT_DIR'],
    './exo_output'
)

if ([string]::IsNullOrWhiteSpace($TenantDomain)) { throw 'Missing TENANT_DOMAIN (or EXO_TENANT_DOMAIN).' }
if ([string]::IsNullOrWhiteSpace($AppId)) { throw 'Missing CLIENT_ID (or EXO_APP_ID).' }
if ([string]::IsNullOrWhiteSpace($CertificateFilePath)) { throw 'Missing EXO_CERT_FILE.' }
if ([string]::IsNullOrWhiteSpace($CertificatePassword)) { throw 'Missing EXO_CERT_PASSWORD.' }
if (-not (Test-Path -LiteralPath $CertificateFilePath)) { throw "Certificate file not found: $CertificateFilePath" }

New-Item -ItemType Directory -Path $OutputFolder -Force | Out-Null
Import-Module ExchangeOnlineManagement -ErrorAction Stop

$securePassword = ConvertTo-SecureString -String $CertificatePassword -AsPlainText -Force

Connect-ExchangeOnline `
    -AppId $AppId `
    -CertificateFilePath $CertificateFilePath `
    -CertificatePassword $securePassword `
    -Organization $TenantDomain `
    -ShowBanner:$false

try {
    $distributionGroups = @(Get-DistributionGroup -ResultSize Unlimited)

    $groupRows = New-Object System.Collections.Generic.List[object]
    $memberRows = New-Object System.Collections.Generic.List[object]

    $index = 0
    foreach ($group in $distributionGroups) {
        $index++
        $groupEmail = if ($group.PrimarySmtpAddress) { $group.PrimarySmtpAddress.ToString() } else { $group.Alias }
        $groupId = if ($group.ExternalDirectoryObjectId) { $group.ExternalDirectoryObjectId } else { $groupEmail }

        Write-Host "Processing distribution group $index of $($distributionGroups.Count): $groupEmail"

        $managedByValues = @()
        if ($group.ManagedBy) {
            foreach ($owner in @($group.ManagedBy)) {
                $ownerValue = Resolve-RecipientValue $owner
                if (-not [string]::IsNullOrWhiteSpace($ownerValue)) {
                    $managedByValues += $ownerValue
                }
            }
        }

        $groupRows.Add([pscustomobject]@{
            group_id                      = $groupId
            display_name                  = $group.DisplayName
            primary_smtp_address          = $groupEmail
            alias                         = $group.Alias
            name                          = $group.Name
            group_type                    = $group.RecipientTypeDetails
            managed_by                    = ($managedByValues -join ', ')
            require_sender_authentication = [bool]$group.RequireSenderAuthenticationEnabled
            hidden_from_address_lists     = [bool]$group.HiddenFromAddressListsEnabled
            member_join_restriction       = $group.MemberJoinRestriction
            member_depart_restriction     = $group.MemberDepartRestriction
            moderation_enabled            = [bool]$group.ModerationEnabled
            send_moderation_notifications = $group.SendModerationNotifications
            external_directory_object_id  = $group.ExternalDirectoryObjectId
        })

        try {
            $members = @(Get-DistributionGroupMember -Identity $group.Identity -ResultSize Unlimited -ErrorAction Stop)
        }
        catch {
            $memberRows.Add([pscustomobject]@{
                group_id                     = $groupId
                distribution_group_email     = $groupEmail
                distribution_group_name      = $group.DisplayName
                member_display_name          = $null
                member_alias                 = $null
                member_primary_smtp          = $null
                member_external_email        = $null
                member_name                  = $null
                member_recipient_type        = $null
                member_recipient_type_detail = $null
                member_guid                  = $null
                member_notes                 = "ERROR: $($_.Exception.Message)"
            })
            continue
        }

        foreach ($member in $members) {
            $memberPrimary = Resolve-RecipientValue $member.PrimarySmtpAddress
            $memberExternal = Resolve-RecipientValue $member.ExternalEmailAddress
            $memberGuid = $null

            if ($member.PSObject.Properties['ExternalDirectoryObjectId'] -and $member.ExternalDirectoryObjectId) {
                $memberGuid = $member.ExternalDirectoryObjectId.ToString()
            }

            $memberRows.Add([pscustomobject]@{
                group_id                     = $groupId
                distribution_group_email     = $groupEmail
                distribution_group_name      = $group.DisplayName
                member_display_name          = $member.DisplayName
                member_alias                 = $member.Alias
                member_primary_smtp          = $memberPrimary
                member_external_email        = $memberExternal
                member_name                  = $member.Name
                member_recipient_type        = $member.RecipientType
                member_recipient_type_detail = $member.RecipientTypeDetails
                member_guid                  = $memberGuid
                member_notes                 = $null
            })
        }
    }

    $groupsJsonPath = Join-Path $OutputFolder 'distribution_groups.json'
    $groupsCsvPath = Join-Path $OutputFolder 'distribution_groups.csv'
    $membersJsonPath = Join-Path $OutputFolder 'distribution_group_members.json'
    $membersCsvPath = Join-Path $OutputFolder 'distribution_group_members.csv'

    $groupRows | ConvertTo-Json -Depth 8 | Set-Content -Path $groupsJsonPath -Encoding UTF8
    $groupRows | Export-Csv -Path $groupsCsvPath -NoTypeInformation -Encoding UTF8

    $memberRows | ConvertTo-Json -Depth 8 | Set-Content -Path $membersJsonPath -Encoding UTF8
    $memberRows | Export-Csv -Path $membersCsvPath -NoTypeInformation -Encoding UTF8

    Write-Host "Distribution groups exported: $($groupRows.Count)"
    Write-Host "Distribution group members exported: $($memberRows.Count)"
    Write-Host "Output folder: $OutputFolder"
}
finally {
    Disconnect-ExchangeOnline -Confirm:$false
}