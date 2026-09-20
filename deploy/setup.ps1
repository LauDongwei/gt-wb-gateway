#Requires -RunAsAdministrator
<#
  gt-wb-gateway one-shot deployment (ASCII only, Windows PowerShell 5.1 compatible)

  What it does:
    1. generates a strong API key (or reuses the key already in config.json)
    2. writes config.json  (listen host + key + log path + resilience defaults)
    3. adds an inbound firewall rule scoped to the given subnet
    4. registers a scheduled task (at logon) that keeps the gateway running

  Usage examples:
    .\setup.ps1 -ZeroTier                       # RECOMMENDED: auto-detect ZeroTier subnet, allow it only
    .\setup.ps1                                 # 0.0.0.0:8787, allow 100.64.0.0/10 (Tailscale)
    .\setup.ps1 -AllowSubnet 192.168.0.0/24     # LAN only
    .\setup.ps1 -ListenHost 127.0.0.1           # local only, no firewall rule
    .\setup.ps1 -SkipFirewall -SkipTask         # only (re)write config.json
#>
param(
  [int]$Port = 8787,
  [string]$ListenHost = "0.0.0.0",
  [string]$AllowSubnet = "100.64.0.0/10",
  [string]$ApiKey = "",
  [switch]$ZeroTier,
  [switch]$SkipFirewall,
  [switch]$SkipTask
)

$ErrorActionPreference = "Stop"

# ---------- 0. ZeroTier auto-detect ----------
# Reads the live ZeroTier virtual adapter and scopes everything to that subnet.
# Nothing is hard-coded: if the network is re-created and the subnet changes,
# re-run this switch and both config.json and the firewall rule follow.
if ($ZeroTier) {
  $ztIf = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.InterfaceAlias -like "*ZeroTier*" -and $_.IPAddress -notlike "169.254.*" } |
    Select-Object -First 1
  if (-not $ztIf) {
    throw "ZeroTier adapter not found. Install ZeroTier and join the network first."
  }

  $bytes = [System.Net.IPAddress]::Parse($ztIf.IPAddress).GetAddressBytes()
  $pfx   = [int]$ztIf.PrefixLength
  $mask  = New-Object byte[] 4
  for ($i = 0; $i -lt 4; $i++) {
    if     ($pfx -ge (($i + 1) * 8)) { $mask[$i] = 255 }
    elseif ($pfx -le ($i * 8))       { $mask[$i] = 0 }
    else { $mask[$i] = [byte](256 - [math]::Pow(2, (($i + 1) * 8 - $pfx))) }
  }
  $net = New-Object byte[] 4
  for ($i = 0; $i -lt 4; $i++) { $net[$i] = [byte]($bytes[$i] -band $mask[$i]) }

  $ztSubnet    = "$([System.Net.IPAddress]::new($net).ToString())/$pfx"
  $AllowSubnet = $ztSubnet
  $ListenHost  = "0.0.0.0"
  Write-Host "zerotier: iface=$($ztIf.InterfaceAlias) ip=$($ztIf.IPAddress) subnet=$ztSubnet" -ForegroundColor Cyan
}

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "== gt-wb-gateway setup ==" -ForegroundColor Cyan
Write-Host "root    : $root"
Write-Host "listen  : ${ListenHost}:${Port}"

# ---------- 1. virtualenv ----------
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  throw "virtualenv not found: $py  (run start.bat once to create it)"
}

# ---------- 2. api key ----------
$cfgPath = Join-Path $root "config.json"
if (-not $ApiKey -and (Test-Path $cfgPath)) {
  try {
    $old = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($old.api_key) { $ApiKey = $old.api_key }
  } catch { }
}
if (-not $ApiKey) {
  $buf = New-Object byte[] 32
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  $rng.GetBytes($buf)
  $ApiKey = [Convert]::ToBase64String($buf).Replace('+','-').Replace('/','_').TrimEnd('=')
}
$masked = if ($ApiKey.Length -gt 12) {
  $ApiKey.Substring(0,8) + "..." + $ApiKey.Substring($ApiKey.Length-4)
} else { "***" }
Write-Host "api key : $masked"

# ---------- 3. config.json ----------
if (Test-Path $cfgPath) {
  $bak = "$cfgPath.bak." + (Get-Date -Format "yyyyMMdd-HHmmss")
  Copy-Item $cfgPath $bak
  Write-Host "backup  : $bak" -ForegroundColor DarkGray
}

$cfg = [ordered]@{
  host                   = $ListenHost
  port                   = $Port
  api_key                = $ApiKey
  log_path               = "gtwb.log"
  desensitize            = $true
  compact_harness        = $true
  strip_tool_metadata    = $true
  retry_on_filter        = $true
  timeout_s              = 300
  connect_timeout_s      = 30
  max_in_flight          = 3
  state_file             = "state.json"
  allow_account_rotation = $false
}
$json = $cfg | ConvertTo-Json -Depth 6
# Write WITHOUT BOM: python's json.loads rejects a leading BOM.
[System.IO.File]::WriteAllText($cfgPath, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "config  : $cfgPath" -ForegroundColor Green

# ---------- 4. firewall ----------
if (-not $SkipFirewall -and $ListenHost -ne "127.0.0.1") {
  $ruleName = "gt-wb-gateway (TCP $Port)"
  Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule -ErrorAction SilentlyContinue

  if ($AllowSubnet -eq "any") {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
      -Protocol TCP -LocalPort $Port -Profile Any | Out-Null
    Write-Host "firewall: allow ANY -> $Port  (NOT recommended)" -ForegroundColor Yellow
  } else {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
      -Protocol TCP -LocalPort $Port -RemoteAddress $AllowSubnet | Out-Null
    Write-Host "firewall: allow $AllowSubnet -> $Port" -ForegroundColor Green
  }
}

# ---------- 5. scheduled task ----------
if (-not $SkipTask) {
  $bat = Join-Path $root "deploy\serve-loop.bat"
  $taskName = "gt-wb-gateway"

  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

  $action = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $root

  # 触发器 1：登录自启
  # 触发器 2：看门狗 —— 每 5 分钟来一次，配合 MultipleInstances=IgnoreNew 就是
  #          "活着不动、死了补起"。
  #
  # 为什么需要看门狗 —— 实测 serve-loop.bat 被 Ctrl+C / 控制台中断事件打死时退出码是
  # 0xC000013A(STATUS_CONTROL_C_EXIT)，任务计划**不会**为这种终止自动重启
  # （RestartCount 不生效），服务就静默没了。
  #
  # 两个必须踩对、否则看门狗形同虚设的细节（都实测踩过）：
  #   a) 重复触发器的 StartBoundary **不能为空**。把 .Repetition 挂到 Logon 触发器上会得到
  #      StartBoundary=""，重复窗口永不开始、NextRunTime 为空，看门狗一次都不触发。
  #      所以这里单独建一个 Once 触发器，并给它一个未来起点。
  #   b) Duration 不要用 [TimeSpan]::MaxValue —— 会序列化成 P99999999DT23H59M59S，
  #      超出任务计划允许范围，注册直接报 "任务 XML 包含格式不正确或超出范围的值"。
  #      用 3650 天（10 年）足够。
  #
  # 落地后**必须确认 NextRunTime 不为空**，否则等于没配。
  $triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn)
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
      -RepetitionInterval (New-TimeSpan -Minutes 5) `
      -RepetitionDuration (New-TimeSpan -Days 3650))
  )

  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable -MultipleInstances IgnoreNew

  # LogonType 必须显式用 S4U（"不管用户是否登录都运行"，免密存储）。
  # 缺省的 Interactive 会在当前桌面开一个可见的 cmd 窗口：看门狗每 5 分钟
  # 敲门时闪一次，服务重启时更是直接留一个常驻黑窗在桌面上。
  # S4U 让任务跑在 session 0，完全无窗口；副作用是注销后服务也继续跑（正是想要的）。
  # 实测 S4U 下用户 profile 正常加载，auth 文件发现不受影响。
  $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Highest

  Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers `
    -Settings $settings -Principal $principal -Force | Out-Null

  # Register-ScheduledTask 失败时**不是**终止错误，脚本会继续往下走并打印假成功。
  # 必须显式复查，否则会出现"脚本说注册成功、实际任务不存在"。
  $reg = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
  if (-not $reg) {
    throw "failed to register scheduled task '$taskName' (see the Register-ScheduledTask error above)"
  }
  $next = (Get-ScheduledTaskInfo -TaskName $taskName).NextRunTime
  if (-not $next) {
    throw "scheduled task '$taskName' has no NextRunTime - the watchdog trigger is not armed"
  }
  Write-Host "task    : '$taskName' registered (logon + 5min watchdog, next run $next)" -ForegroundColor Green
}

Write-Host ""
Write-Host "done. verify:" -ForegroundColor Cyan
Write-Host "  curl http://127.0.0.1:$Port/health"
Write-Host "  Start-ScheduledTask -TaskName gt-wb-gateway"
