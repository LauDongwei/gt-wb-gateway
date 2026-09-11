#Requires -RunAsAdministrator
<#
  Undo what setup.ps1 did: scheduled task, firewall rule, running process.
  config.json is left alone on purpose (it holds your API key) - delete it by hand
  if you really want to.
#>
param(
  [int]$Port = 8787
)

Write-Host "== gt-wb-gateway teardown ==" -ForegroundColor Cyan

# scheduled task
Unregister-ScheduledTask -TaskName "gt-wb-gateway" -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "task    : removed"

# firewall rule
$ruleName = "gt-wb-gateway (TCP $Port)"
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue |
  Remove-NetFirewallRule -ErrorAction SilentlyContinue
Write-Host "firewall: removed"

# listening process
$c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($c) {
  $c | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
  Write-Host "process : stopped (port $Port)"
} else {
  Write-Host "process : nothing listening on $Port"
}

Write-Host ""
Write-Host "done. config.json kept (contains your API key)." -ForegroundColor Yellow
