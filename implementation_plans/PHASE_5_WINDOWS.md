# PythOwnCloud — Phase 5: Windows Desktop Integration

## Goal

Access PythOwnCloud files from Windows without opening a browser — browse, upload, and download files via Explorer or the command line. This is **on-demand access**, not a full clone of the server.

**Why not clone?** Your Windows machine has less free space than the server's total storage. Syncing everything would fill the disk. Instead, files are accessed over the network and only downloaded when you open them.


## Prerequisites

- PythOwnCloud WebDAV server running (see [PHASE_5.1.md](PHASE_5.1.md))
- Tailscale installed on Windows
- Both machines on the same tailnet


## Option A: Map Network Drive in Explorer

Windows Explorer has built-in WebDAV support via "Map Network Drive".

### Setup

1. Open **File Explorer**
2. Right-click **This PC** → **Map network drive...**
3. Choose a drive letter (e.g., `P:` for PythOwnCloud)
4. Folder: `\\100.93.58.13@8000\dav\`
5. Check **Reconnect at sign-in**
6. Check **Connect using different credentials**
7. Click **Finish**
8. Enter credentials: `admin` / your PythOwnCloud password

PythOwnCloud now appears as drive `P:` in Explorer. Browse, drag-and-drop, create folders — all the usual operations.

### Alternative: Command Line

```cmd
net use P: \\100.93.58.13@8000\dav\ /user:admin yourpassword /persistent:yes
```

To disconnect:

```cmd
net use P: /delete
```

### Windows WebDAV Quirks

Windows WebClient (the built-in WebDAV client) has some known issues:

| Issue | Fix |
|-------|-----|
| Refuses non-HTTPS connections | Registry: set `HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Services\WebClient\Parameters\BasicAuthLevel` to `2` (allows Basic Auth over HTTP). Restart WebClient service. |
| 50 MB file size limit | Registry: set `HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Services\WebClient\Parameters\FileSizeLimitInBytes` to `4294967295` (4 GB). Restart WebClient service. |
| WebClient service not running | Run `net start WebClient` or set it to Automatic in Services. |
| Slow first connection | Normal — Windows probes for proxy settings. Subsequent connections are faster. |

#### Apply Registry Fixes (Run as Administrator)

```cmd
reg add HKLM\SYSTEM\CurrentControlSet\Services\WebClient\Parameters /v BasicAuthLevel /t REG_DWORD /d 2 /f
reg add HKLM\SYSTEM\CurrentControlSet\Services\WebClient\Parameters /v FileSizeLimitInBytes /t REG_DWORD /d 4294967295 /f
net stop WebClient && net start WebClient
```

These changes are required once and persist across reboots.


## Option B: rclone (Recommended for Reliability)

Windows Explorer's WebDAV client is functional but quirky. rclone provides a more reliable alternative with better performance.

### Install

Download from [rclone.org/downloads](https://rclone.org/downloads/) or via winget:

```powershell
winget install Rclone.Rclone
```

Also install [WinFsp](https://winfsp.dev/) — required for FUSE mount support on Windows.

### Configure

```powershell
rclone config
```

| Prompt | Value |
|--------|-------|
| name | `poc` |
| type | `webdav` |
| url | `http://100.93.58.13:8000/dav/` |
| vendor | `other` |
| user | `admin` |
| pass | your PythOwnCloud password |

### Common Commands

```powershell
# List top-level directories
rclone lsd poc:

# List files
rclone ls poc:photos/2025/

# Download a file
rclone copy poc:documents/report.pdf .

# Upload a file
rclone copy .\file.zip poc:backups/

# Interactive file browser
rclone ncdu poc:
```

### Mount as a Drive Letter

```powershell
rclone mount poc: P: --vfs-cache-mode full
```

This mounts PythOwnCloud as `P:` — usable from Explorer, cmd, PowerShell, or any Windows application. Files are cached locally on read and uploaded on write.

To run in the background, use `--daemon` or set up a Windows service:

#### Autostart via Task Scheduler

1. Open **Task Scheduler**
2. Create Basic Task → Name: `PythOwnCloud Mount`
3. Trigger: **When I log on**
4. Action: **Start a program**
   - Program: `C:\Users\youruser\rclone\rclone.exe` (or wherever rclone is installed)
   - Arguments: `mount poc: P: --vfs-cache-mode full`
5. Finish

Or via PowerShell:

```powershell
$action = New-ScheduledTaskAction -Execute "rclone.exe" -Argument "mount poc: P: --vfs-cache-mode full"
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "PythOwnCloud Mount" -Action $action -Trigger $trigger -Description "Mount PythOwnCloud via rclone"
```


## Comparison

| Method | Best For | Needs Install | Reliability |
|--------|----------|--------------|-------------|
| Map Network Drive (Explorer) | Quick setup, casual use | No (built-in) | Quirky (registry fixes needed) |
| rclone mount | Daily use, large files | Yes (rclone + WinFsp) | Solid |
| rclone copy | Scripted uploads/downloads | Yes (rclone only) | Solid |


## Browsing Without Mounting

If you don't want a persistent mount, you can always browse via:

1. **PythOwnCloud web UI** — open `http://100.93.58.13:8000/browse/` in any browser
2. **rclone ncdu** — terminal-based interactive browser with file sizes


## Testing Checklist

1. `net use P: \\100.93.58.13@8000\dav\` connects successfully (after registry fixes)
2. Explorer shows PythOwnCloud files under `P:`
3. Drag a file from Desktop to `P:\documents\` → appears in PythOwnCloud
4. Open an image from `P:` → displays in the default viewer
5. `rclone ls poc:` lists files correctly
6. `rclone mount poc: P:` works and survives Tailscale reconnections
7. Files larger than 50 MB upload successfully (after FileSizeLimitInBytes fix)
