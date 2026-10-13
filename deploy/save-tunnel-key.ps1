param([Parameter(Mandatory=$true)][string]$CredentialPath)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object Windows.Forms.Form
$form.Text = 'Gumacu - Secure tunnel key'
$form.Size = New-Object Drawing.Size(560,210)
$form.StartPosition = 'CenterScreen'
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
$save.Add_Click({
    if (-not $inputBox.Text.StartsWith('sk-') -or $inputBox.Text.Length -lt 25) {
        [Windows.Forms.MessageBox]::Show('Enter a valid dedicated API key.') | Out-Null
        return
    }
    try {
        $parent = Split-Path -Parent $CredentialPath
        [IO.Directory]::CreateDirectory($parent) | Out-Null
        $encrypted = ConvertTo-SecureString $inputBox.Text -AsPlainText -Force | ConvertFrom-SecureString
        [IO.File]::WriteAllText($CredentialPath, $encrypted)
        $acl = New-Object Security.AccessControl.FileSecurity
        $acl.SetAccessRuleProtection($true,$false)
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','Allow')
        $acl.AddAccessRule($rule)
        Set-Acl -LiteralPath $CredentialPath -AclObject $acl
        $inputBox.Clear()
        $form.Close()
    } catch {
        [Windows.Forms.MessageBox]::Show('Could not save the key securely. No key was logged.') | Out-Null
    }
})
$form.Controls.AddRange(@($label,$inputBox,$save))
$form.AcceptButton = $save
$form.Add_Shown({$inputBox.Focus()})
[void]$form.ShowDialog()
$inputBox.Dispose()
$form.Dispose()
