[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ArgsList
)
$env:PYTHONPATH = "$PSScriptRoot\src;$env:PYTHONPATH"
& python -m repo_janitor.cli @ArgsList
