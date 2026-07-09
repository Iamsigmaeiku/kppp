# USR-W610 Tailscale TCP gateway setup
# Exposes LAN W610 (192.168.0.111:8899) to tailnet via MagicDNS usrw610.<tailnet>.ts.net:8899

param(
    [string]$W610Host = "192.168.0.111",
    [int]$W610Port = 8899,
    [string]$MachineName = "usrw610",
    [switch]$InstallBootTask,
    [switch]$RemoveBootTask
)

$ErrorActionPreference = "Stop"
$TaskName = "TailscaleW610Serve"

function Require-Tailscale {
    if (-not (Get-Command tailscale -ErrorAction SilentlyContinue)) {
        throw "Tailscale CLI not found. Install from https://tailscale.com/download/windows"
    }
}

function Get-TailscaleDnsName {
    $json = tailscale status --json | ConvertFrom-Json
    return $json.Self.DNSName.TrimEnd(".")
}

function Test-W610Lan {
    param([string]$TargetHost, [int]$Port)
    $result = Test-NetConnection -ComputerName $TargetHost -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue
    if (-not $result) {
        throw "W610 not reachable at ${TargetHost}:${Port} on LAN"
    }
}

function Set-W610Serve {
    param([string]$TargetHost, [int]$Port)
    tailscale serve --bg --tcp=$Port "tcp://${TargetHost}:${Port}"
}

function Install-BootTask {
    $scriptPath = $MyInvocation.MyCommand.Path
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    try {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
        Write-Host "Scheduled task '$TaskName' registered (runs at startup)."
    } catch {
        Write-Warning "Could not register boot task (run PowerShell as Administrator with -InstallBootTask): $_"
    }
}

function Remove-BootTask {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Scheduled task '$TaskName' removed (if it existed)."
}

Require-Tailscale

if ($RemoveBootTask) {
    Remove-BootTask
    exit 0
}

Write-Host "Setting Tailscale hostname to $MachineName ..."
tailscale set --hostname=$MachineName

Write-Host "Checking LAN connectivity to W610 at ${W610Host}:${W610Port} ..."
Test-W610Lan -TargetHost $W610Host -Port $W610Port

Write-Host "Configuring tailscale serve TCP proxy ..."
Set-W610Serve -TargetHost $W610Host -Port $W610Port

$dnsName = Get-TailscaleDnsName
Write-Host ""
Write-Host "Done. W610 available on tailnet at:"
Write-Host "  ${dnsName}:${W610Port}"
Write-Host ""
tailscale serve status

if ($InstallBootTask) {
    Install-BootTask
    Write-Host ""
    Write-Host "Re-run after reboot: tailscale serve status"
}
