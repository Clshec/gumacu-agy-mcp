param(
    [Parameter(Mandatory=$true)][string]$RuntimeDirectory,
    [Parameter(Mandatory=$true)][string]$TunnelId
)
$ErrorActionPreference = 'Stop'
if ($TunnelId -notmatch '^tunnel_[a-zA-Z0-9_-]+$') { throw 'Invalid tunnel ID' }
$runtime = [IO.Path]::GetFullPath($RuntimeDirectory)
$keyPath = Join-Path $runtime 'key.dpapi'
if (-not (Test-Path -LiteralPath $keyPath)) { throw 'Save the dedicated key securely first.' }
$secret = Get-Content -LiteralPath $keyPath -Raw | ConvertTo-SecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = Join-Path $runtime 'bin/tunnel-client.exe'
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.EnvironmentVariables['CONTROL_PLANE_API_KEY'] = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    $info.EnvironmentVariables['XDG_CONFIG_HOME'] = Join-Path $runtime 'config'
    $profiles = Join-Path $runtime 'profiles'
    $profileText = Get-Content -LiteralPath (Join-Path $profiles 'gumacu-netcup.yaml') -Raw
    $match = [regex]::Match($profileText, '(?m)^\s+"?command"?:\s+(".*")\s*$')
    if (-not $match.Success) { throw 'Reviewed stdio command is missing.' }
    $command = $match.Groups[1].Value | ConvertFrom-Json
    # The generated command uses forward slash paths; quote it as one Windows argument.
    if ($command -match '\\' -or $profiles -match '"') { throw 'Runtime paths need reviewed quoting.' }
    $escapedCommand = $command.Replace('"','\"')
    $info.Arguments = 'runtimes connect --alias gumacu-netcup --tunnel-id ' + $TunnelId +
        ' --profile gumacu-netcup --profile-dir "' + $profiles + '"' +
        ' --runtime-api-key env:CONTROL_PLANE_API_KEY --mcp-command "' + $escapedCommand + '" --json'
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    [void]$process.Start()
    $outTask = $process.StandardOutput.ReadToEndAsync()
    $errTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(45000)) { $process.Kill(); throw 'Managed runtime startup timed out.' }
    $output = $outTask.Result
    if ($process.ExitCode -ne 0) { throw 'Managed runtime start failed. Check redacted runtime diagnostics.' }
    $output
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    $secret.Dispose()
    if ($info) { $info.EnvironmentVariables.Remove('CONTROL_PLANE_API_KEY') }
}
