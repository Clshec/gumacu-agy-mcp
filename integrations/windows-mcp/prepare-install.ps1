param(
    [Parameter(Mandatory=$true)][string]$TunnelProfile,
    [string]$InstallDirectory = (Join-Path $env:LOCALAPPDATA 'Gumacu\WindowsMCP'),
    [string]$Python = 'python',
    [string]$Uv = 'uv'
)
$ErrorActionPreference = 'Stop'
$install = [IO.Path]::GetFullPath($InstallDirectory)
$profile = Get-Content -LiteralPath $TunnelProfile -Raw | ConvertFrom-Json
$binding = @($profile.mcp.commands | Where-Object { $_.channel -eq 'main' })
if ($binding.Count -ne 1) { throw 'Expected one existing main stdio binding' }
if ($binding[0].command -match 'bridge.py') { throw 'Already using a bridge; review its backend config instead' }
New-Item -ItemType Directory -Path $install -Force | Out-Null
& $Uv venv (Join-Path $install '.venv') --python $Python
if ($LASTEXITCODE -ne 0) { throw 'Environment creation failed' }
$pythonExe = Join-Path $install '.venv\Scripts\python.exe'
& $pythonExe -m ensurepip
if ($LASTEXITCODE -ne 0) { throw 'Package installer initialization failed' }
$lock = Join-Path $PSScriptRoot 'requirements.lock.txt'
$requirements = if (Test-Path -LiteralPath $lock) { $lock } else { Join-Path $PSScriptRoot 'requirements.txt' }
& $pythonExe -m pip install --disable-pip-version-check --no-input -r $requirements
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'bridge.py') -Destination $install
# Use Python's shell lexer only for the existing forward-slash SSH command.
# The runtime key is never read here or copied into backend configuration.
$env:GUMACU_PREPARE_UPSTREAM = $binding[0].command
$env:GUMACU_PREPARE_INSTALL = $install
try {
    @'
import json, os, pathlib, shlex
root = pathlib.Path(os.environ['GUMACU_PREPARE_INSTALL'])
args = shlex.split(os.environ['GUMACU_PREPARE_UPSTREAM'])
config = {
    'upstream': {
        'command': args[0], 'args': args[1:],
        # The MCP SDK's safe inherited environment omits PROGRAMDATA.
        # Windows OpenSSH exits 255 without it, even for local `ssh -G`.
        'env': {'PROGRAMDATA': os.environ.get('PROGRAMDATA', r'C:\ProgramData')},
    },
    'windows': {
        'command': str(root / '.venv/Scripts/python.exe'),
        'args': ['-m', 'windows_mcp', 'serve', '--transport', 'stdio', '--control', '--control-policy', 'relaxed'],
        'env': {'ANONYMIZED_TELEMETRY': 'false', 'PYTHONUTF8': '1'},
    },
}
(root / 'backends.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
'@ | & $pythonExe -
    if ($LASTEXITCODE -ne 0) { throw 'Backend configuration failed' }
} finally {
    Remove-Item Env:\GUMACU_PREPARE_UPSTREAM -ErrorAction SilentlyContinue
    Remove-Item Env:\GUMACU_PREPARE_INSTALL -ErrorAction SilentlyContinue
}
$forward = $install.Replace('\','/')
if ($forward -match '["\s]') { throw 'Use an installation path without spaces for this tunnel launcher' }
[pscustomobject]@{
    InstallDirectory = $install
    ProposedMainCommand = "$forward/.venv/Scripts/python.exe $forward/bridge.py --config $forward/backends.json"
    ProfileChanged = $false
} | ConvertTo-Json
