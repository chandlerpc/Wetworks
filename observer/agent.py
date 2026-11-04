#!/usr/bin/env python3
"""The Observer's helper: run it on a computer you want Wetworks to learn from. Stop it any time (Ctrl+C).

Every few seconds it notes the app in front and its window title; about once a minute (if screenshots are on)
it sends a screenshot that Wetworks describes in one sentence with the local model and then throws away.
Apps and titles listed in config/observer.toml (work, money, passwords, private browsing) are never captured.
Pause from the console's Observer tab. No keystrokes are ever recorded.

  macOS:            python3 observer/agent.py --server https://<msi>.ts.net --device mac
  MSI (in Ubuntu):  python3 observer/agent.py --server http://localhost:8700 --device msi
                    (reads the Windows desktop through powershell.exe)

The console access code is read from WETWORKS_TOKEN or ~/.wetworks-observer (first line).
Only run this on your own computers, never on a work-managed one.
"""

import argparse
import base64
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SAMPLE_EVERY = 5
SEND_EVERY = 60


# ---- what's on screen ------------------------------------------------------------------------
def mac_front() -> tuple[str, str, float]:
    script = ('tell application "System Events"\n set p to first application process whose frontmost is true\n'
              ' set a to name of p\n try\n  set t to name of front window of p\n on error\n  set t to ""\n end try\n'
              'end tell\nreturn a & tab & t')
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5).stdout.rstrip("\n")
    app, _, title = out.partition("\t")
    idle = 0.0
    try:
        io = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=5).stdout
        for line in io.splitlines():
            if "HIDIdleTime" in line:
                idle = int(line.split("=")[-1].strip()) / 1e9
                break
    except Exception:
        pass
    return app, title, idle


def mac_shot() -> bytes | None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.jpg"
        subprocess.run(["screencapture", "-x", "-t", "jpg", str(p)], timeout=10)
        if not p.exists():
            return None
        subprocess.run(["sips", "-Z", "1280", str(p)], capture_output=True, timeout=10)
        return p.read_bytes()


PS_FRONT = r"""
Add-Type @"
using System; using System.Runtime.InteropServices; using System.Text;
public class W {
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);
 [StructLayout(LayoutKind.Sequential)] public struct L { public uint cb; public uint t; }
 [DllImport("user32.dll")] public static extern bool GetLastInputInfo(ref L l);
}
"@
$h=[W]::GetForegroundWindow(); $sb=New-Object System.Text.StringBuilder 512; [void][W]::GetWindowText($h,$sb,512)
$p=0; [void][W]::GetWindowThreadProcessId($h,[ref]$p); $n=(Get-Process -Id $p -ErrorAction SilentlyContinue).ProcessName
$l=New-Object W+L; $l.cb=8; [void][W]::GetLastInputInfo([ref]$l); $idle=([Environment]::TickCount-$l.t)/1000
[Console]::OutputEncoding=[Text.Encoding]::UTF8
Write-Output (ConvertTo-Json @{app=$n; title=$sb.ToString(); idle=$idle} -Compress)
"""

PS_SHOT = r"""
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$b=[System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp=New-Object Drawing.Bitmap $b.Width,$b.Height; $g=[Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size)
$w=[Math]::Min(1280,$b.Width); $h=[int]($b.Height*$w/$b.Width)
$s=New-Object Drawing.Bitmap $bmp,$w,$h; $m=New-Object IO.MemoryStream
$s.Save($m,[Drawing.Imaging.ImageFormat]::Jpeg); [Convert]::ToBase64String($m.ToArray())
"""


def powershell() -> str | None:
    for exe in ("powershell.exe", "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"):
        try:
            subprocess.run([exe, "-NoProfile", "-Command", "exit"], capture_output=True, timeout=20)
            return exe
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def win_front(ps: str) -> tuple[str, str, float]:
    out = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", PS_FRONT], capture_output=True, timeout=15).stdout
    d = json.loads(out.decode("utf-8", "replace").strip() or "{}")
    return str(d.get("app") or ""), str(d.get("title") or ""), float(d.get("idle") or 0)


def win_shot(ps: str) -> bytes | None:
    out = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", PS_SHOT], capture_output=True, timeout=30).stdout
    try:
        return base64.b64decode(out.strip())
    except ValueError:
        return None


# ---- talking to Wetworks -------------------------------------------------------------------------
class Wetworks:
    def __init__(self, server: str, token: str):
        self.server, self.token = server.rstrip("/"), token

    def call(self, path: str, data: bytes | None = None, ctype: str = "application/json", method: str | None = None) -> dict:
        req = urllib.request.Request(self.server + path, data=data, method=method or ("POST" if data is not None else "GET"),
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return json.loads(body or b"{}")


def excluded(cfg: dict, app: str, title: str) -> bool:
    a, t = app.lower(), title.lower()
    return any(x.lower() in a for x in cfg.get("exclude_apps", [])) or any(w.lower() in t or w.lower() in a
                                                                             for w in cfg.get("exclude_title_words", []))


def main() -> None:
    ap = argparse.ArgumentParser(description="The Observer's helper for Wetworks")
    ap.add_argument("--server", required=True, help="Wetworks console URL")
    ap.add_argument("--device", default=socket.gethostname().split(".")[0], help="name for this computer")
    args = ap.parse_args()
    token = os.environ.get("WETWORKS_TOKEN", "")
    if not token:
        f = Path.home() / ".wetworks-observer"
        token = f.read_text().strip().splitlines()[0] if f.exists() else ""
    if not token:
        sys.exit("Put the console access code in ~/.wetworks-observer (or WETWORKS_TOKEN) first.")

    ps = None
    if sys.platform == "darwin":
        front, shot, plat = mac_front, mac_shot, "mac"
    elif sys.platform == "win32" or "microsoft" in platform.release().lower():
        ps = powershell() if sys.platform != "win32" else "powershell"
        if not ps:
            sys.exit("Couldn't reach powershell.exe from here.")
        front, shot, plat = (lambda: win_front(ps)), (lambda: win_shot(ps)), "windows"
    else:
        sys.exit("This helper runs on macOS or Windows (including WSL on the MSI).")

    ww = Wetworks(args.server, token)
    try:
        cfg = ww.call(f"/api/observer/helper?device={urllib.parse.quote(args.device)}")
    except urllib.error.HTTPError as e:
        sys.exit("The access code was rejected." if e.code == 401 else f"Wetworks said {e.code}.")
    except urllib.error.URLError as e:
        sys.exit(f"Can't reach Wetworks at {args.server}: {e.reason}")
    print(f"Observer helper running on {args.device} ({plat}). Ctrl+C to stop. Pause any time from the console.")

    every_sample = 10 if plat == "windows" else SAMPLE_EVERY  # PowerShell is slower to ask
    buf, last_send, last_shot, last_cfg = [], time.time(), 0.0, time.time()
    while True:
        t0 = time.time()
        try:
            if cfg.get("enabled"):
                app, title, idle = front()
                if app and idle < cfg.get("idle_after", 300):
                    private = excluded(cfg, app, title)
                    buf.append({"ts": datetime.now(timezone.utc).isoformat(), "app": "Private" if private else app,
                                "title": "" if private else title, "secs": every_sample})
                    rec = cfg.get("recording")
                    every = cfg.get("record_every", 5) if rec else cfg.get("shot_every", 60)
                    if not private and (rec or cfg.get("screenshots")) and t0 - last_shot >= every:
                        img = shot()
                        last_shot = t0
                        if img:
                            q = urllib.parse.urlencode({"device": args.device, "app": app, "title": title,
                                                        "recording": rec["id"] if rec else 0})
                            ww.call(f"/api/observer/shot?{q}", img, "image/jpeg")
            if t0 - last_cfg >= 15:  # notice pause / record from the console quickly
                cfg, last_cfg = ww.call(f"/api/observer/helper?device={urllib.parse.quote(args.device)}"), t0
            if t0 - last_send >= SEND_EVERY or (cfg.get("recording") and len(buf) >= 2):
                cfg = ww.call("/api/observer/samples", json.dumps({"device": args.device, "platform": plat, "samples": buf}).encode())
                buf, last_send = [], t0
        except KeyboardInterrupt:
            raise
        except Exception as e:  # keep going through sleep, Wi-Fi drops, MSI restarts
            print(f"(skipped a beat: {type(e).__name__}: {str(e)[:120]})", file=sys.stderr)
            time.sleep(10)
        time.sleep(max(0.5, every_sample - (time.time() - t0)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nObserver helper stopped.")
