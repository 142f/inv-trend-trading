param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PytestArgs
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = 'python'
}

# pytest's system TEMP can be unavailable on scheduled Windows jobs.  Use a
# per-process workspace path and deliberately leave cleanup to the workspace
# retention policy, avoiding a second scan of a directory another process may
# still hold open.
$baseTemp = Join-Path $repoRoot ('.tmp\pytest-{0}' -f $PID)
& $python -m pytest --basetemp $baseTemp @PytestArgs
exit $LASTEXITCODE
