[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$DailyArguments
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot 'pyproject.toml') -PathType Leaf)) {
    throw "Could not find the repository root from '$PSScriptRoot'."
}

$projectPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $projectPython -PathType Leaf) {
    $pythonExecutable = $projectPython
}
else {
    $pythonCommand = Get-Command -Name python -CommandType Application -ErrorAction Stop |
        Select-Object -First 1
    $pythonExecutable = $pythonCommand.Source
}

$exitCode = 1
Push-Location -LiteralPath $repoRoot
try {
    & $pythonExecutable -m turtle_detector.daily_cli @DailyArguments
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) {
        $exitCode = 0
    }
}
catch {
    Write-Error "Daily market scan could not start: $($_.Exception.Message)"
    $exitCode = 1
}
finally {
    Pop-Location
}

exit ([int]$exitCode)
