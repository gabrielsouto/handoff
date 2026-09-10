<#
.SYNOPSIS
    Runs tools/handoff.py from this folder against any repository.

.DESCRIPTION
    Keep this folder as the single copy of the tool. Point it at whatever
    checkout you are working in with --repo, or set $env:HANDOFF_REPO once.

.EXAMPLE
    .\handoff.ps1 doctor --repo D:\Vida\Profissional\htdocs\ementa
    .\handoff.ps1 recover claude --ai
    $env:HANDOFF_REPO = "D:\Vida\Profissional\htdocs\ementa"; .\handoff.ps1 status
#>
$script = Join-Path $PSScriptRoot "tools\handoff.py"

# Probe by running it: a "python3" on PATH may be the Microsoft Store alias,
# which resolves through Get-Command but is not an interpreter.
$python = $null
foreach ($candidate in @("python3", "python", "py")) {
    if (-not (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
    & $candidate -c "import sys" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
}
if (-not $python) {
    Write-Error "handoff: no working Python 3 interpreter found on PATH"
    exit 2
}

& $python $script @args
exit $LASTEXITCODE
