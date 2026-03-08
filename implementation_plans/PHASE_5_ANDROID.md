# PythOwnCloud — Phase 5: Android Client Setup

## Goal

Automatically upload photos and videos from your Android phone to PythOwnCloud, replacing Nextcloud's instant upload. Two key behaviors:

1. **Auto-upload** — new photos/videos are sent to the server automatically.
2. **No delete propagation** — deleting files from the phone to free space does NOT delete them from the server. The server is the permanent archive; the phone is a temporary source.

This matches Nextcloud's instant-upload behavior exactly: take a photo, it goes to the server, then freely delete it from the phone whenever you need space.

**Why not two-way sync?** The phone has less free space than the server. Cloning everything back would fill it up. Instead, browse server files on-demand (see below) and download only what you need.


## Prerequisites

- PythOwnCloud WebDAV server running (see [PHASE_5.1.md](PHASE_5.1.md))
- Tailscale installed on Android
- Phone and Pi on the same tailnet


## Option A: FolderSync + WebDAV (Recommended)

FolderSync is a well-maintained Android app that handles retry, conflict detection, and battery optimization out of the box.

### Step 1: Install FolderSync

Install [FolderSync](https://play.google.com/store/apps/details?id=dk.tacit.android.foldersync.lite) from Google Play (free version is enough).

### Step 2: Add a WebDAV Account

1. Open FolderSync → Accounts → Add Account
2. Account type: **WebDAV**
3. Configure:
   - **Server URL**: `http://100.93.58.13:8000/dav/`
   - **Username**: `admin`
   - **Password**: your PythOwnCloud password
4. Test connection → should succeed

### Step 3: Create Folder Pairs

Each folder pair defines a one-way upload rule.

> **CRITICAL: Sync type must be "To remote folder".** Never use "Two-way" or "Mirror" — those modes propagate local deletions to the server. If you delete a photo from the phone to free space, "Two-way" would delete it from PythOwnCloud too.
>
> | Sync type | Local delete → server delete? | Safe to free phone space? |
> |-----------|-------------------------------|--------------------------|
> | **To remote folder** | No | ✅ Yes |
> | Two-way | Yes | ❌ No — destroys server copy |
> | To local folder | N/A | N/A (wrong direction) |

#### Camera Photos

| Setting | Value |
|---------|-------|
| Local folder | `/DCIM/Camera/` |
| Remote folder | `SubidaInstantánea/Camera/` |
| Sync type | **To remote folder** (one-way upload) |
| Schedule | Every 15 minutes, or instant sync on change |
| Use WiFi only | Optional (disable for mobile data uploads) |
| Overwrite | **If newer** (skip already-uploaded photos) |

#### Screenshots (Optional)

| Setting | Value |
|---------|-------|
| Local folder | `/Pictures/Screenshots/` |
| Remote folder | `SubidaInstantánea/Screenshots/` |
| Sync type | **To remote folder** |
| Schedule | Every 30 minutes |

#### WhatsApp Images (Optional)

| Setting | Value |
|---------|-------|
| Local folder | `/WhatsApp/Media/WhatsApp Images/` |
| Remote folder | `SubidaInstantánea/WhatsApp/` |
| Sync type | **To remote folder** |
| Schedule | Every 60 minutes |

### Step 4: Battery Optimization

Android aggressively kills background apps. To keep FolderSync alive:

1. Settings → Apps → FolderSync → Battery → **Unrestricted**
2. In FolderSync settings → enable **Foreground service** (persistent notification)
3. Samsung/Xiaomi/Huawei devices: check [dontkillmyapp.com](https://dontkillmyapp.com) for device-specific workarounds


## Option B: Tasker + curl (Lightweight Alternative)

No extra app — just Tasker watching for new files and uploading immediately.

### Profile

- **Event**: File → File Modified
- **Path**: `DCIM/Camera/*`

### Task

```bash
FILE="%evtpath"
FILENAME=$(basename "$FILE")
curl -X PUT \
  -u "admin:yourpassword" \
  -T "$FILE" \
  "http://100.93.58.13:8000/dav/SubidaInstantánea/Camera/$FILENAME"
```

### Limitations

- **No retry** — if Tailscale is disconnected or the server is down, the photo is lost
- **No deduplication** — re-saving a photo uploads it again
- Good for testing, not for daily use


## Browsing Server Files from the Phone

Since cloning the full server to the phone doesn't make sense, browse files **on-demand**:

1. **PythOwnCloud web UI** — open `http://100.93.58.13:8000/browse/` in the phone's browser. Thumbnails, search, preview — all work on mobile.
2. **FolderSync file browser** — built-in WebDAV browser, no sync needed. Tap a file to stream/download it individually.
3. **Solid Explorer** or **Total Commander** — Android file managers with WebDAV plugins. Same URL and credentials as FolderSync. Browse, open, or download individual files without syncing everything.

All of these stream files over the network — nothing is stored locally unless you explicitly download it.


## What This Replaces

| Before | After |
|--------|-------|
| Phone → Nextcloud Android app → Nextcloud (PHP) → PostgreSQL → drive | Phone → FolderSync → PythOwnCloud (Python) → SQLite → drive |

The destination path stays the same (`SubidaInstantánea/Camera/`). You can run both in parallel during testing — Nextcloud on port 443, PythOwnCloud on port 8000 — and cut over when confident.


## Testing Checklist

1. FolderSync connects to `http://pi:8000/dav/` and authenticates
2. Take a photo → appears on the Pi within the sync interval
3. Take a short video (100 MB) → uploads without corruption
4. Toggle airplane mode mid-upload → re-enable → FolderSync retries and completes
5. Already-uploaded photos are skipped on next sync (no duplicates)
6. **Delete safety test**: upload a photo → verify it's on the server → delete it from the phone → trigger sync → **confirm the server copy is still there**
7. Battery impact is acceptable (< 2% per day from FolderSync)
