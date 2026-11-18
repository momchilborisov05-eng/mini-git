# Quick end-to-end demo of mgit in a throwaway folder.
$ErrorActionPreference = 'Continue'
$repo = Split-Path $PSScriptRoot -Parent
$env:PYTHONPATH = $repo
function mgit { python -m mgit @args }

$d = Join-Path $env:TEMP 'mgit-demo'
Remove-Item $d -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $d | Out-Null
Set-Location $d

Write-Host '$ mgit init' -ForegroundColor Cyan; mgit init
Set-Content app.py @('def hello(name):', "    return 'Hello ' + name")
Write-Host '$ mgit add app.py; mgit commit' -ForegroundColor Cyan
mgit add app.py; mgit commit -m 'Add greeting app'

Write-Host '$ mgit checkout -b shout  (edit app.py)  mgit diff' -ForegroundColor Cyan
mgit checkout -b shout
Set-Content app.py @('def hello(name):', "    return 'HELLO ' + name.upper()")
mgit diff
mgit add app.py; mgit commit -m 'Shout the greeting'

Write-Host '$ mgit checkout main  (add bye())  mgit commit' -ForegroundColor Cyan
mgit checkout main
Add-Content app.py @('', 'def bye():', "    return 'Bye'")
mgit add app.py; mgit commit -m 'Add bye()'

Write-Host '$ mgit merge shout' -ForegroundColor Cyan; mgit merge shout
Write-Host '$ mgit log --oneline --graph' -ForegroundColor Cyan; mgit log --oneline --graph
Write-Host '$ type app.py' -ForegroundColor Cyan; Get-Content app.py
