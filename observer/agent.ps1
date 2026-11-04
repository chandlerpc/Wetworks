<#
The Observer's helper for Windows, with nothing to install (PowerShell only). Same behaviour as agent.py:
foreground app + window title + idle time every 10 seconds, a screenshot about once a minute while you're
active (Wetworks describes it with the local model and throws the image away), and Pause/Record from the
console. No keystrokes. Sensitive screens get task-level descriptions only; never_watch lists aren't captured.

  powershell -ExecutionPolicy Bypass -File observer\agent.ps1 -Server https://YOUR-MSI.ts.net -Device work

The console access code is read from $env:WETWORKS_TOKEN or %USERPROFILE%\.wetworks-observer (first line).
Ctrl+C stops it.
#>
param(
  [Parameter(Mandatory = $true)][string]$Server,
  [string]$Device = $env:COMPUTERNAME
)
$ErrorActionPreference = "Stop"
$Server = $Server.TrimEnd("/")
$token = $env:WETWORKS_TOKEN
$tokFile = Join-Path $env:USERPROFILE ".wetworks-observer"
if (-not $token -and (Test-Path $tokFile)) { $token = (Get-Content $tokFile -TotalCount 1).Trim() }
if (-not $token) { Write-Host "Put the console access code in $tokFile first."; exit 1 }
$headers = @{ Authorization = "Bearer $token" }

Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type @"
using System; using System.Runtime.InteropServices; using System.Text;
public class WW {
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);
 [StructLayout(LayoutKind.Sequential)] public struct L { public uint cb; public uint t; }
 [DllImport("user32.dll")] public static extern bool GetLastInputInfo(ref L l);
}
"@

function Front {
  $h = [WW]::GetForegroundWindow(); $sb = New-Object System.Text.StringBuilder 512
  [void][WW]::GetWindowText($h, $sb, 512)
  $procId = 0; [void][WW]::GetWindowThreadProcessId($h, [ref]$procId)
  $name = (Get-Process -Id $procId -ErrorAction SilentlyContinue).ProcessName
  $l = New-Object WW+L; $l.cb = 8; [void][WW]::GetLastInputInfo([ref]$l)
  $idle = ([Environment]::TickCount - $l.t) / 1000
  return @{ app = "$name"; title = $sb.ToString(); idle = $idle }
}

function Shot {
  $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
  $bmp = New-Object Drawing.Bitmap $b.Width, $b.Height; $g = [Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
  $w = [Math]::Min(1280, $b.Width); $hgt = [int]($b.Height * $w / $b.Width)
  $s = New-Object Drawing.Bitmap $bmp, $w, $hgt; $m = New-Object IO.MemoryStream
  $s.Save($m, [Drawing.Imaging.ImageFormat]::Jpeg)
  $g.Dispose(); $bmp.Dispose(); $s.Dispose()
  return $m.ToArray()
}

function NeverWatch($cfg, $app, $title) {
  $a = "$app".ToLower(); $t = "$title".ToLower()
  foreach ($x in $cfg.never_apps) { if ($a.Contains("$x".ToLower())) { return $true } }
  foreach ($w in $cfg.never_title_words) { if ($t.Contains("$w".ToLower()) -or $a.Contains("$w".ToLower())) { return $true } }
  return $false
}

$q = [uri]::EscapeDataString($Device)
try { $cfg = Invoke-RestMethod -Uri "$Server/api/observer/helper?device=$q" -Headers $headers }
catch { Write-Host "Can't reach Wetworks at ${Server}: $($_.Exception.Message)"; exit 1 }
Write-Host "Observer helper running on $Device (windows). Ctrl+C to stop. Pause any time from the console."

$buf = New-Object System.Collections.ArrayList
$lastSend = Get-Date; $lastShot = [datetime]::MinValue; $lastCfg = Get-Date
while ($true) {
  $t0 = Get-Date
  try {
    if ($cfg.enabled) {
      $f = Front
      if ($f.app -and $f.idle -lt $cfg.idle_after) {
        $private = NeverWatch $cfg $f.app $f.title
        [void]$buf.Add(@{ ts = (Get-Date).ToUniversalTime().ToString("o"); app = $(if ($private) { "Private" } else { $f.app });
                          title = $(if ($private) { "" } else { $f.title }); secs = 10 })
        $rec = $cfg.recording
        $every = if ($rec) { $cfg.record_every } else { $cfg.shot_every }
        if (-not $private -and ($rec -or $cfg.screenshots) -and (($t0 - $lastShot).TotalSeconds -ge $every)) {
          $img = Shot; $lastShot = $t0
          $rid = if ($rec) { $rec.id } else { 0 }
          $qs = "device=$q&app=$([uri]::EscapeDataString($f.app))&title=$([uri]::EscapeDataString($f.title))&recording=$rid"
          Invoke-RestMethod -Uri "$Server/api/observer/shot?$qs" -Method Post -Headers $headers -ContentType "image/jpeg" -Body $img | Out-Null
        }
      }
    }
    if (($t0 - $lastCfg).TotalSeconds -ge 15) {
      $cfg = Invoke-RestMethod -Uri "$Server/api/observer/helper?device=$q" -Headers $headers; $lastCfg = $t0
    }
    if ((($t0 - $lastSend).TotalSeconds -ge 60) -or ($cfg.recording -and $buf.Count -ge 2)) {
      $body = @{ device = $Device; platform = "windows"; samples = $buf } | ConvertTo-Json -Depth 4 -Compress
      $cfg = Invoke-RestMethod -Uri "$Server/api/observer/samples" -Method Post -Headers $headers -ContentType "application/json" `
        -Body ([Text.Encoding]::UTF8.GetBytes($body))
      $buf.Clear(); $lastSend = $t0
    }
  } catch {
    Write-Host "(skipped a beat: $($_.Exception.Message))"
    Start-Sleep -Seconds 10
  }
  $wait = 10 - ((Get-Date) - $t0).TotalSeconds
  if ($wait -gt 0.5) { Start-Sleep -Milliseconds ([int]($wait * 1000)) }
}
