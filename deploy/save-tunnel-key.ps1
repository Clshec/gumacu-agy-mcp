param([Parameter(Mandatory=$true)][string]$CredentialPath, [switch]$SelfTest)
$ErrorActionPreference = 'Stop'
$CredentialPath = [IO.Path]::GetFullPath($CredentialPath)
if ($SelfTest -and [IO.Path]::GetFileName($CredentialPath) -ne 'dpapi-self-test.tmp') {
    throw 'Self-test may only write dpapi-self-test.tmp'
}
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object Windows.Forms.Form
$form.Text = 'Gumacu - Secure tunnel key (diagnostic)'
$form.Size = New-Object Drawing.Size(560,210)
$form.StartPosition = 'CenterScreen'
$form.TopMost = $true
$form.ShowInTaskbar = $true
$label = New-Object Windows.Forms.Label
$label.Text = 'Paste the dedicated Tunnels Read + Use key. It stays encrypted on this PC.'
$label.Location = New-Object Drawing.Point(15,15)
$label.Size = New-Object Drawing.Size(510,45)
$inputBox = New-Object Windows.Forms.TextBox
$inputBox.Location = New-Object Drawing.Point(15,65)
$inputBox.Size = New-Object Drawing.Size(510,25)
$inputBox.UseSystemPasswordChar = $true
$save = New-Object Windows.Forms.Button
$save.Text = 'Save securely'
$save.Location = New-Object Drawing.Point(370,110)
$save.Size = New-Object Drawing.Size(150,30)
$saveHandler = {
    if (-not $inputBox.Text.StartsWith('sk-') -or $inputBox.Text.Length -lt 25) {
        [Windows.Forms.MessageBox]::Show('Enter a valid dedicated API key.') | Out-Null
        return
    }
    try {
        $stage = 'directory'
        $parent = Split-Path -Parent $CredentialPath
        [IO.Directory]::CreateDirectory($parent) | Out-Null
        $stage = 'encrypt'
        $encrypted = ConvertTo-SecureString $inputBox.Text -AsPlainText -Force | ConvertFrom-SecureString
        $stage = 'write'
        [IO.File]::WriteAllText($CredentialPath, $encrypted)
        $stage = 'permissions'
        $acl = New-Object Security.AccessControl.FileSecurity
        $acl.SetAccessRuleProtection($true,$false)
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','Allow')
        $acl.AddAccessRule($rule)
        Set-Acl -LiteralPath $CredentialPath -AclObject $acl
        $inputBox.Clear()
        $form.Close()
    } catch {
        $diagnostic = @{stage=$stage; error_type=$_.Exception.GetType().FullName}
        [IO.File]::WriteAllText($CredentialPath + '.diagnostic.json', ($diagnostic | ConvertTo-Json))
        if ($SelfTest) { $form.Close(); return }
        [Windows.Forms.MessageBox]::Show('Secure save failed at: ' + $stage + '. No key was logged.') | Out-Null
    }
}.GetNewClosure()
$save.Add_Click($saveHandler)
$form.Controls.AddRange(@($label,$inputBox,$save))
$form.AcceptButton = $save
$form.Add_Shown({$inputBox.Focus()})
if ($SelfTest) {
    $inputBox.Text = 'sk-dummy-gumacu-self-test-not-a-real-key'
    $testTimer = New-Object Windows.Forms.Timer
    $testTimer.Interval = 100
    $testTimer.Add_Tick({$testTimer.Stop(); $save.PerformClick()}.GetNewClosure())
    $testTimer.Start()
    [void]$form.ShowDialog()
    $testTimer.Dispose()
    if (-not (Test-Path -LiteralPath $CredentialPath)) { throw 'Self-test did not save' }
    $restored = Get-Content -LiteralPath $CredentialPath -Raw | ConvertTo-SecureString
    $restored.Dispose()
    Remove-Item -LiteralPath $CredentialPath
    Write-Output 'capture_self_test=passed'
} else {
    [void]$form.ShowDialog()
}
$inputBox.Dispose()
$form.Dispose()
