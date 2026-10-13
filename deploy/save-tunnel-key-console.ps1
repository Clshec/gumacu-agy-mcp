param([switch]$SelfTest)
$ErrorActionPreference = 'Stop'
$runtime = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../gumacu-tunnel-runtime'))
$file = Join-Path $runtime $(if ($SelfTest) {'console-self-test.tmp'} else {'key.dpapi'})
$stage = 'input'
$secret = $null
try {
    if ($SelfTest) {
        $secret = ConvertTo-SecureString 'dummy-gumacu-console-test' -AsPlainText -Force
    } else {
        Write-Host 'Gumacu secure key entry. Do not paste the key in chat.'
        Write-Host 'Paste the dedicated Tunnels Read + Use key below, then press Enter.'
        $secret = Read-Host 'Key (hidden)' -AsSecureString
    }
    if ($secret.Length -lt 25) { throw 'Key is too short' }
    $stage = 'encrypt'
    $encrypted = ConvertFrom-SecureString -SecureString $secret
    $stage = 'write'
    [IO.Directory]::CreateDirectory($runtime) | Out-Null
    [IO.File]::WriteAllText($file, $encrypted)
    $stage = 'permissions'
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true,$false)
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','Allow')))
    Set-Acl -LiteralPath $file -AclObject $acl
    $stage = 'verify'
    $restored = Get-Content -LiteralPath $file -Raw | ConvertTo-SecureString
    if ($restored.Length -ne $secret.Length) { throw 'Verification failed' }
    $restored.Dispose()
    if ($SelfTest) {
        Remove-Item -LiteralPath $file
        Write-Output 'console_secure_storage_test=passed'
    } else {
        Write-Host 'SAVED SECURELY. You can close this window.' -ForegroundColor Green
        [void](Read-Host 'Press Enter to close')
    }
} catch {
    if ($SelfTest) { throw }
    Write-Host ('SAVE FAILED AT: ' + $stage + '. No key was logged.') -ForegroundColor Red
    [void](Read-Host 'Press Enter to close')
} finally {
    if ($secret) { $secret.Dispose() }
    $encrypted = $null
}
