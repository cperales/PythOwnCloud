# PythOwnCloud — Phase 5: Linux Desktop Integration

## Goal

Access PythOwnCloud files from a Linux desktop file manager or terminal — browse, upload, download, and manage files without opening a browser. No full clone of the server; everything is accessed on-demand over the network.


## Prerequisites

- PythOwnCloud WebDAV server running (see [PHASE_5.1.md](PHASE_5.1.md))
- Tailscale installed on the Linux machine
- Both machines on the same tailnet


## Option A: File Manager (GNOME, KDE, XFCE)

All major Linux file managers support WebDAV natively via the `dav://` URL scheme.

### GNOME Files (Nautilus)

1. Open Files
2. **Other Locations** (bottom of the sidebar) → **Connect to Server**
3. Enter: `dav://100.93.58.13:8000/dav/`
4. Authenticate with `admin` / your password
5. PythOwnCloud appears as a network location in the sidebar

Or from the command line:

```bash
# Open in Nautilus directly
gio mount dav://100.93.58.13:8000/dav/
nautilus dav://100.93.58.13:8000/dav/
```

### KDE Dolphin

1. Open Dolphin
2. Address bar → type: `webdav://100.93.58.13:8000/dav/`
3. Authenticate when prompted

### Thunar (XFCE)

1. Open Thunar
2. Address bar → type: `davs://100.93.58.13:8000/dav/` (or `dav://` without TLS)
3. Requires `gvfs-backends` package: `sudo apt install gvfs-backends`

### What Works

- Browse directories, open files, drag-and-drop upload
- Create folders, rename, delete
- Copy files to/from local disk
- Open files directly in applications (streams over network)

### What Doesn't Work

- Real-time notifications (no push — must refresh to see new files)
- Thumbnail previews (depends on file manager; may need to download the file first)


## Option B: rclone (Terminal & Scripted Access)

rclone is the Swiss Army knife for remote storage. Works on every Linux distro.

### Install

```bash
# Debian/Ubuntu
sudo apt install rclone

# Or latest version
curl https://rclone.org/install.sh | sudo bash
```

### Configure

```bash
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

```bash
# List top-level directories
rclone lsd poc:

# List all files in a directory
rclone ls poc:photos/2025/

# Download a file
rclone copy poc:photos/2025/sunset.jpg ./

# Upload a file
rclone copy ./report.pdf poc:documents/

# Upload a directory (one-way, non-destructive)
rclone copy ~/Documents/project/ poc:documents/project/

# Interactive file browser (ncurses)
rclone ncdu poc:
```

### Mount as FUSE Filesystem

Mount PythOwnCloud as a local directory — all reads/writes go through WebDAV transparently:

```bash
mkdir -p ~/mnt/poc
rclone mount poc: ~/mnt/poc --vfs-cache-mode full --daemon
```

Then use it like any local folder:

```bash
ls ~/mnt/poc/photos/
cp ~/Downloads/file.zip ~/mnt/poc/backups/
xdg-open ~/mnt/poc/documents/report.pdf
```

To unmount:

```bash
fusermount -u ~/mnt/poc
```

#### Autostart on Login

Add to `~/.config/autostart/rclone-poc.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=PythOwnCloud Mount
Exec=rclone mount poc: /home/youruser/mnt/poc --vfs-cache-mode full
Terminal=false
```

Or via systemd user service:

```ini
# ~/.config/systemd/user/rclone-poc.service
[Unit]
Description=Mount PythOwnCloud via rclone
After=network-online.target

[Service]
ExecStart=/usr/bin/rclone mount poc: /home/youruser/mnt/poc --vfs-cache-mode full
ExecStop=/bin/fusermount -u /home/youruser/mnt/poc
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now rclone-poc.service
```


## Option C: rsync over SSH (Bulk Transfers)

For migrating large amounts of data, rsync is faster than WebDAV because it avoids HTTP overhead and does delta transfers.

```bash
# Upload a large folder to the Pi
rsync -avz ~/Photos/ pi@100.93.58.13:/mnt/external-disk/poc-data/photos/

# Download from the Pi
rsync -avz pi@100.93.58.13:/mnt/external-disk/poc-data/documents/ ~/Documents/from-pi/
```

**Important**: rsync bypasses PythOwnCloud — files won't appear in the web UI or database until you trigger a scan:

```bash
curl -X POST -H "X-API-Key: your-key" http://100.93.58.13:8000/api/scan
```

Use rsync for initial migration of large datasets, then switch to WebDAV (rclone or file manager) for day-to-day use.


## Comparison

| Method | Best For | Needs Install | Offline Access |
|--------|----------|--------------|---------------|
| File manager (Nautilus/Dolphin) | Casual browsing, drag-and-drop | No (built-in) | No |
| rclone mount | Transparent local access | Yes (`apt install rclone`) | Cached files only |
| rclone copy | Scripted uploads/downloads | Yes | Downloaded files |
| rsync | Bulk migration | No (built-in) | Downloaded files |


## Testing Checklist

1. File manager connects to `dav://pi:8000/dav/` and lists directories
2. Drag a file from local disk to the WebDAV mount → appears in PythOwnCloud
3. Open an image directly from the mount → streams without full download
4. `rclone ls poc:` lists files correctly
5. `rclone mount` allows `ls`, `cp`, `cat` on mounted directory
6. Create/delete a folder via file manager → reflected in PythOwnCloud web UI
