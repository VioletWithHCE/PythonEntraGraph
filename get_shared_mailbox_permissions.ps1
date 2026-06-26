param(
    [string]$TenantDomain,
    [string]$AppId,
    [string]$CertificateFilePath,
    [string]$CertificatePassword,
    [string]$OutputFolder
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
    if ($Value.PSObject.Properties['UserPrincipalName'] -and $Value.UserPrincipalName) { return $Value.UserPrincipalName.ToString() }
    if ($Value.PSObject.Properties['Name'] -and $Value.Name) { return $Value.Name.ToString() }
    return $Value.ToString()
}

function Resolve-MultiValue {
    param([object]$Value)

    if ($null -eq $Value) { return @() }

    $items = @()
    foreach ($entry in @($Value)) {
        $resolved = Resolve-RecipientValue $entry
        if (-not [string]::IsNullOrWhiteSpace($resolved)) {
            $items += $resolved
        }
    }

    return $items
}

function Is-UsefulPrincipal {
    param([string]$Principal)

    if ([string]::IsNullOrWhiteSpace($Principal)) { return $false }

    $skipPatterns = @(
        '^NT AUTHORITY\\SELF$',
        '^S-1-5-',
        '^HealthMailbox',
        '^DiscoverySearchMailbox',
        '^SystemMailbox',
        '^FederatedEmail',
        '^Microsoft Exchange'
    )

    foreach ($pattern in $skipPatterns) {
        if ($Principal -match $pattern) { return $false }
    }

    return $true
}

function Get-RuleForwardTargets {
    param([object]$Rule)

    $targets = New-Object System.Collections.Generic.List[string]

    foreach ($fieldName in @('ForwardTo','ForwardAsAttachmentTo','RedirectTo')) {
        if ($Rule.PSObject.Properties[$fieldName]) {
            $resolvedValues = Resolve-MultiValue $Rule.$fieldName
            foreach ($value in $resolvedValues) {
                if (-not [string]::IsNullOrWhiteSpace($value) -and -not $targets.Contains($value)) {
                    $targets.Add($value)
                }
            }
        }
    }

    return @($targets)
}

if (-not (Get-Module -ListAvailable -Name ExchangeOnlineManagement)) {
    throw "ExchangeOnlineManagement module is not installed. Run inside pwsh: Install-Module ExchangeOnlineManagement -Scope CurrentUser"
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

if ([string]::IsNullOrWhiteSpace($TenantDomain)) { throw 'Missing TENANT_DOMAIN (or EXO_TENANT_DOMAIN) in .env or environment.' }
if ([string]::IsNullOrWhiteSpace($AppId)) { throw 'Missing CLIENT_ID (or EXO_APP_ID) in .env or environment.' }
if ([string]::IsNullOrWhiteSpace($CertificateFilePath)) { throw 'Missing EXO_CERT_FILE in .env or environment.' }
if ([string]::IsNullOrWhiteSpace($CertificatePassword)) { throw 'Missing EXO_CERT_PASSWORD in .env or environment.' }
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
    $mailboxes = Get-EXOMailbox -RecipientTypeDetails SharedMailbox -ResultSize Unlimited

    $mailboxRows = New-Object System.Collections.Generic.List[object]
    $permissionRows = New-Object System.Collections.Generic.List[object]
    $forwardingRuleRows = New-Object System.Collections.Generic.List[object]

    $index = 0

    foreach ($mbx in $mailboxes) {
        $index++
        $smtp = if ($mbx.PrimarySmtpAddress) { $mbx.PrimarySmtpAddress.ToString() } else { $mbx.UserPrincipalName }
        $mailboxId = if ($mbx.ExternalDirectoryObjectId) { $mbx.ExternalDirectoryObjectId } else { $smtp }

        Write-Host "Processing $index of $($mailboxes.Count): $smtp"

        $forwardingAddress = Resolve-RecipientValue $mbx.ForwardingAddress
        $forwardingSmtp = Resolve-RecipientValue $mbx.ForwardingSmtpAddress
        $deliverToMailboxAndForward = [bool]$mbx.DeliverToMailboxAndForward

        $sendOnBehalfList = New-Object System.Collections.Generic.List[string]
        if ($mbx.GrantSendOnBehalfTo) {
            foreach ($delegate in $mbx.GrantSendOnBehalfTo) {
                $delegateValue = Resolve-RecipientValue $delegate
                if (Is-UsefulPrincipal $delegateValue) {
                    if (-not $sendOnBehalfList.Contains($delegateValue)) {
                        $sendOnBehalfList.Add($delegateValue)
                    }

                    $permissionRows.Add([pscustomobject]@{
                        mailbox_id      = $mailboxId
                        mailbox_email   = $smtp
                        mailbox_name    = $mbx.DisplayName
                        trustee         = $delegateValue
                        permission_type = 'SendOnBehalf'
                        access_rights   = 'SendOnBehalf'
                        is_inherited    = $false
                        deny            = $false
                    })
                }
            }
        }

        $fullAccessEntries = @(Get-EXOMailboxPermission -Identity $smtp -ErrorAction SilentlyContinue)
        foreach ($entry in $fullAccessEntries) {
            $principal = $entry.User.ToString()
            if ((-not $entry.IsInherited) -and (-not $entry.Deny) -and (Is-UsefulPrincipal $principal)) {
                $permissionRows.Add([pscustomobject]@{
                    mailbox_id      = $mailboxId
                    mailbox_email   = $smtp
                    mailbox_name    = $mbx.DisplayName
                    trustee         = $principal
                    permission_type = 'FullAccess'
                    access_rights   = (($entry.AccessRights | ForEach-Object { $_.ToString() }) -join ', ')
                    is_inherited    = [bool]$entry.IsInherited
                    deny            = [bool]$entry.Deny
                })
            }
        }

        $sendAsEntries = @(Get-RecipientPermission -Identity $smtp -ErrorAction SilentlyContinue)
        foreach ($entry in $sendAsEntries) {
            $principal = $entry.Trustee.ToString()
            if (Is-UsefulPrincipal $principal) {
                $permissionRows.Add([pscustomobject]@{
                    mailbox_id      = $mailboxId
                    mailbox_email   = $smtp
                    mailbox_name    = $mbx.DisplayName
                    trustee         = $principal
                    permission_type = 'SendAs'
                    access_rights   = 'SendAs'
                    is_inherited    = $false
                    deny            = $false
                })
            }
        }

        $ruleForwardTargets = New-Object System.Collections.Generic.List[string]
        $ruleForwardSummary = $null
        $ruleForwardingEnabled = $false
        $inboxRuleQueryError = $null

        try {
            $rules = @(Get-InboxRule -Mailbox $smtp -ErrorAction Stop)

            foreach ($rule in $rules) {
                if ($rule.Enabled -ne $true) { continue }

                $targets = Get-RuleForwardTargets -Rule $rule
                if ($targets.Count -eq 0) { continue }

                $ruleForwardingEnabled = $true

                foreach ($target in $targets) {
                    if (-not $ruleForwardTargets.Contains($target)) {
                        $ruleForwardTargets.Add($target)
                    }
                }

                $forwardingRuleRows.Add([pscustomobject]@{
                    mailbox_id            = $mailboxId
                    mailbox_email         = $smtp
                    mailbox_name          = $mbx.DisplayName
                    rule_name             = $rule.Name
                    rule_enabled          = [bool]$rule.Enabled
                    forward_to            = ((Resolve-MultiValue $rule.ForwardTo) -join '; ')
                    forward_as_attachment = ((Resolve-MultiValue $rule.ForwardAsAttachmentTo) -join '; ')
                    redirect_to           = ((Resolve-MultiValue $rule.RedirectTo) -join '; ')
                    description           = $rule.Description
                })
            }
        }
        catch {
            $inboxRuleQueryError = $_.Exception.Message
        }

        if ($ruleForwardTargets.Count -gt 0) {
            $ruleForwardSummary = ($ruleForwardTargets -join ', ')
        }

        $mailboxRows.Add([pscustomobject]@{
            mailbox_id                    = $mailboxId
            display_name                  = $mbx.DisplayName
            primary_smtp_address          = $smtp
            alias                         = $mbx.Alias
            user_principal_name           = $mbx.UserPrincipalName
            hidden_from_address_lists     = [bool]$mbx.HiddenFromAddressListsEnabled
            forwarding_smtp_address       = $forwardingSmtp
            forwarding_address            = $forwardingAddress
            deliver_to_mailbox_and_forward = $deliverToMailboxAndForward
            grant_send_on_behalf_to       = ($sendOnBehalfList -join ', ')
            inbox_rule_forwarding_enabled = $ruleForwardingEnabled
            inbox_rule_forward_targets    = $ruleForwardSummary
            inbox_rule_query_error        = $inboxRuleQueryError
            external_directory_object_id  = $mbx.ExternalDirectoryObjectId
        })
    }

    $mailboxJsonPath = Join-Path $OutputFolder 'shared_mailboxes.json'
    $permissionJsonPath = Join-Path $OutputFolder 'shared_mailbox_permissions.json'
    $forwardingRulesJsonPath = Join-Path $OutputFolder 'shared_mailbox_forwarding_rules.json'

    $mailboxCsvPath = Join-Path $OutputFolder 'shared_mailboxes.csv'
    $permissionCsvPath = Join-Path $OutputFolder 'shared_mailbox_permissions.csv'
    $forwardingRulesCsvPath = Join-Path $OutputFolder 'shared_mailbox_forwarding_rules.csv'

    $mailboxRows | ConvertTo-Json -Depth 8 | Set-Content -Path $mailboxJsonPath -Encoding UTF8
    $permissionRows | ConvertTo-Json -Depth 8 | Set-Content -Path $permissionJsonPath -Encoding UTF8
    $forwardingRuleRows | ConvertTo-Json -Depth 8 | Set-Content -Path $forwardingRulesJsonPath -Encoding UTF8

    $mailboxRows | Export-Csv -Path $mailboxCsvPath -NoTypeInformation -Encoding UTF8
    $permissionRows | Export-Csv -Path $permissionCsvPath -NoTypeInformation -Encoding UTF8
    $forwardingRuleRows | Export-Csv -Path $forwardingRulesCsvPath -NoTypeInformation -Encoding UTF8

    Write-Host "Shared mailboxes exported: $($mailboxRows.Count)"
    Write-Host "Permission rows exported: $($permissionRows.Count)"
    Write-Host "Forwarding rule rows exported: $($forwardingRuleRows.Count)"
    Write-Host "Output folder: $OutputFolder"
}
finally {
    Disconnect-ExchangeOnline -Confirm:$false
}