# PythOwnCloud — Phase 5.2: S3-Compatible API

## Problem

WebDAV uploads are not resumable. When the phone (S3Drive) or rclone sends a large file via `PUT /dav/{path}`, the entire file travels in a single HTTP request. If the Tailscale tunnel drops, the connection resets, or the phone switches networks mid-transfer — the whole upload restarts from zero. For a 500 MB video over a mobile connection, this can mean never completing the upload.

TUS (Phase 5 Step 3) solves this for the web UI, but no mainstream mobile sync app speaks TUS. The Nextcloud Android app uses a proprietary chunked WebDAV extension that no other client supports either.

The one protocol that gives resumable uploads **and** is natively supported by both S3Drive (Android) and rclone (Windows/Linux/Mac) is **S3 multipart upload**.


## Solution: Minimal S3-Compatible API

Add an S3-compatible HTTP API to PythOwnCloud, alongside the existing REST and WebDAV APIs. All three coexist on the same port, share the same storage, database, cache, and thumbnail logic.

The S3 API only needs to implement the subset that S3Drive and rclone actually use — not the full AWS S3 specification (which has hundreds of endpoints). Specifically:

- Single-object operations: `GET`, `PUT`, `HEAD`, `DELETE`
- Bucket listing: `ListObjectsV2`
- Multipart upload: `CreateMultipartUpload`, `UploadPart`, `CompleteMultipartUpload`, `AbortMultipartUpload`
- Auth: AWS Signature V4 (what every S3 client sends)


## What This Is (and Isn't)

**In scope:**

- S3 single-object operations (GET, PUT, HEAD, DELETE)
- S3 ListObjectsV2 (directory listing mapped to S3 XML)
- S3 multipart upload (resumable chunked uploads)
- AWS Signature V4 request verification
- Cleanup of abandoned multipart uploads
- Configuration for S3 access key and secret key

**Out of scope:**

- Bucket creation/deletion (single bucket "storage" is hardcoded)
- ACLs, policies, versioning, lifecycle rules, replication
- Pre-signed URLs
- S3 Select, inventory, analytics, or any other advanced feature
- Changes to WebDAV or TUS (they continue working unchanged)


## Architecture

```
┌────────────────────────────────────────────────────-┐
│                PythOwnCloud Server                  │
│                                                     │
│  ┌──────────┐  ┌──────────-┐  ┌──────────┐          │
│  │ REST API │  │  WebDAV   │  │   S3 API │          │
│  │ /files/* │  │  /dav/*   │  │   /s3/*  │          │
│  │ /api/*   │  │           │  │          │          │ 
│  └────┬─────┘  └───-─┬─────┘  └────┬─────┘          │
│       │              │             │                │
│       └──────────┬───┴─────────────┘                │
│                  │                                  │
│           ┌──────▼──────┐                           │
│           │   Shared    │                           │
│           │  helpers,   │                           │
│           │  db, cache, │                           │
│           │  thumbnails │                           │
│           └──────┬──────┘                           │
│                  │                                  │
│           ┌──────▼──────┐                           │
│           │   SQLite    │                           │
│           │   + ext4    │                           │
│           └─────────────┘                           │
└──────────────────────────────────────────────────-──┘
```

The S3 router handles the HTTP protocol translation and auth verification. Everything below that — storage paths, database, thumbnails, cache invalidation — is the same code used by WebDAV and REST.


## How S3 Multipart Upload Works

This is the protocol that gives S3Drive and rclone resumable uploads. It has four steps:

### Step 1: Initiate

```
POST /s3/storage/photos/video.mp4?uploads HTTP/1.1
Authorization: AWS4-HMAC-SHA256 ...
```

Server creates a tracking record and returns an `UploadId`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<InitiateMultipartUploadResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Bucket>storage</Bucket>
  <Key>photos/video.mp4</Key>
  <UploadId>abc123def456</UploadId>
</InitiateMultipartUploadResult>
```

### Step 2: Upload Parts

The client splits the file into parts (typically 5–100 MB each) and uploads them individually:

```
PUT /s3/storage/photos/video.mp4?partNumber=1&uploadId=abc123def456 HTTP/1.1
Content-Length: 5242880
Authorization: AWS4-HMAC-SHA256 ...

[5 MB of binary data]
```

Server stores the part and returns its ETag (MD5 hash):

```
HTTP/1.1 200 OK
ETag: "d41d8cd98f00b204e9800998ecf8427e"
```

**If the connection drops here**, the client can retry just this one part. All previously uploaded parts are safe on disk.

### Step 3: Complete

After all parts are uploaded, the client sends a completion request listing all parts and their ETags:

```
POST /s3/storage/photos/video.mp4?uploadId=abc123def456 HTTP/1.1
Authorization: AWS4-HMAC-SHA256 ...

<?xml version="1.0" encoding="UTF-8"?>
<CompleteMultipartUpload>
  <Part><PartNumber>1</PartNumber><ETag>"etag1"</ETag></Part>
  <Part><PartNumber>2</PartNumber><ETag>"etag2"</ETag></Part>
  <Part><PartNumber>3</PartNumber><ETag>"etag3"</ETag></Part>
</CompleteMultipartUpload>
```

Server concatenates all parts in order, moves the result to the final storage path, upserts to SQLite, generates a thumbnail if applicable, and responds:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<CompleteMultipartUploadResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Location>/s3/storage/photos/video.mp4</Location>
  <Bucket>storage</Bucket>
  <Key>photos/video.mp4</Key>
  <ETag>"combined-etag"</ETag>
</CompleteMultipartUploadResult>
```

### Step 4: Abort (if needed)

```
DELETE /s3/storage/photos/video.mp4?uploadId=abc123def456 HTTP/1.1
```

Server deletes all stored parts and the metadata file.

### Resume After Disconnect

When S3Drive loses connection mid-upload, it reconnects and:

1. Knows which parts it already sent (tracked client-side by ETag)
2. Optionally calls `ListParts` to verify what the server received
3. Resumes uploading from the first missing part
4. Sends `CompleteMultipartUpload` once all parts are done

The key insight: each part is independently stored and verified. A 500 MB video in 5 MB parts means at most 5 MB of wasted transfer on disconnect, not 500 MB.


## AWS Signature V4 Authentication

Every S3 client signs requests with HMAC-SHA256. The `Authorization` header looks like:

```
AWS4-HMAC-SHA256
Credential=ACCESS_KEY/20260311/us-east-1/s3/aws4_request,
SignedHeaders=host;x-amz-content-sha256;x-amz-date,
Signature=fe5f80f77d5fa3beca038a248ff027d0445342fe2855ddc963176630326f1024
```

### How Verification Works

The server must reconstruct the same signature from the request and compare:

1. **Canonical Request** — normalize the HTTP method, path, query string, headers, and payload hash into a deterministic string.
2. **String to Sign** — combine the algorithm name, timestamp, credential scope, and hash of the canonical request.
3. **Signing Key** — derive a key by HMAC-chaining: `secret_key → date → region → service → "aws4_request"`.
4. **Signature** — HMAC-SHA256 the string-to-sign with the signing key. Compare against the header.

This is ~80–100 lines of Python using only `hmac` and `hashlib` from the standard library. No external dependencies.

### Configuration

Two new settings in `config.py`:

```python
s3_access_key: str = "pythowncloud"         # POC_S3_ACCESS_KEY
s3_secret_key: str = ""                     # POC_S3_SECRET_KEY
s3_region: str = "us-east-1"               # POC_S3_REGION (arbitrary, must match client)
```

The access key is like a username. The secret key is like a password — it must be set and match between server and client. The region is arbitrary but both sides must agree (S3Drive and rclone will default to `us-east-1`).


## S3 Endpoints

All endpoints live under the `/s3/` prefix. The single bucket is called `storage`.

### Single-Object Operations

| Method | Path | Description | Maps To |
|--------|------|-------------|---------|
| `PUT` | `/s3/storage/{key}` | Upload file (single request) | `safe_path()` → write → `db.upsert_file()` |
| `GET` | `/s3/storage/{key}` | Download file | `safe_path()` → `FileResponse` |
| `HEAD` | `/s3/storage/{key}` | File metadata | `safe_path()` → stat + headers |
| `DELETE` | `/s3/storage/{key}` | Delete file | `safe_path()` → unlink → `db.delete_file_row()` |

These reuse the same logic as WebDAV `PUT`/`GET`/`HEAD`/`DELETE`, just with S3 XML error responses and Signature V4 auth.

### Bucket Operations

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/s3/` | ListBuckets — returns single bucket "storage" |
| `GET` | `/s3/storage?list-type=2` | ListObjectsV2 — directory listing as S3 XML |
| `HEAD` | `/s3/storage` | HeadBucket — return 200 (bucket exists) |

`ListObjectsV2` supports `prefix` and `delimiter` query parameters to simulate directory browsing:

```
GET /s3/storage?list-type=2&prefix=photos/2025/&delimiter=/
```

This returns objects with prefix `photos/2025/` and common prefixes (subdirectories) — mapping directly to `db.list_directory("photos/2025")`.

### Multipart Upload

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/s3/storage/{key}?uploads` | Initiate multipart upload |
| `PUT` | `/s3/storage/{key}?partNumber=N&uploadId=X` | Upload a part |
| `POST` | `/s3/storage/{key}?uploadId=X` | Complete multipart upload |
| `DELETE` | `/s3/storage/{key}?uploadId=X` | Abort multipart upload |
| `GET` | `/s3/storage/{key}?uploadId=X` | ListParts (for resume verification) |


## Multipart Upload Storage

Reuses the existing `.uploads/` directory alongside TUS uploads, with a different naming convention:

```
/data/.uploads/
├── tus-{id}.part          ← TUS partial file (single file, appended to)
├── tus-{id}.meta          ← TUS metadata
├── s3-{upload_id}.meta    ← S3 multipart metadata (JSON)
├── s3-{upload_id}.part.1  ← S3 part 1
├── s3-{upload_id}.part.2  ← S3 part 2
└── s3-{upload_id}.part.3  ← S3 part 3
```

The S3 metadata file stores:

```json
{
  "upload_id": "abc123def456",
  "bucket": "storage",
  "key": "photos/video.mp4",
  "created_at": "2026-03-11T10:00:00+00:00",
  "parts": {
    "1": {"size": 5242880, "etag": "\"d41d8cd98f00b204e9800998ecf8427e\""},
    "2": {"size": 5242880, "etag": "\"e99a18c428cb38d5f260853678922e03\""}
  }
}
```

On `CompleteMultipartUpload`:

1. Read the parts list from the XML body
2. Verify all listed parts exist on disk and ETags match
3. Concatenate parts in order → write to a temp file
4. Move temp file to final `safe_path()` destination
5. Compute SHA256 checksum of final file
6. `db.upsert_file()` with path, size, checksum
7. `thumbnails.record_upload()` + thumbnail generation (same burst-aware pattern)
8. `invalidate_listing_cache()`
9. Delete all `.part.N` files and `.meta`

Cleanup of abandoned multipart uploads uses the same logic as TUS: delete any `s3-*.meta` files with `created_at` older than `tus_max_age_hours` (default 24h).


## S3 XML Response Format

S3 uses its own XML namespace and format, different from WebDAV. A new `s3_xml.py` module builds these responses.

### ListBuckets

```xml
<?xml version="1.0" encoding="UTF-8"?>
<ListAllMyBucketsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Owner>
    <ID>pythowncloud</ID>
    <DisplayName>pythowncloud</DisplayName>
  </Owner>
  <Buckets>
    <Bucket>
      <Name>storage</Name>
      <CreationDate>2025-01-01T00:00:00.000Z</CreationDate>
    </Bucket>
  </Buckets>
</ListAllMyBucketsResult>
```

### ListObjectsV2

```xml
<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>storage</Name>
  <Prefix>photos/2025/</Prefix>
  <Delimiter>/</Delimiter>
  <KeyCount>1</KeyCount>
  <MaxKeys>1000</MaxKeys>
  <IsTruncated>false</IsTruncated>
  <Contents>
    <Key>photos/2025/sunset.jpg</Key>
    <LastModified>2025-03-04T18:30:00.000Z</LastModified>
    <ETag>"a1b2c3d4"</ETag>
    <Size>3452918</Size>
    <StorageClass>STANDARD</StorageClass>
  </Contents>
  <CommonPrefixes>
    <Prefix>photos/2025/vacation/</Prefix>
  </CommonPrefixes>
</ListBucketResult>
```

**Critical fields:**

- `<KeyCount>` — number of items returned (total of `<Contents>` + `<CommonPrefixes>` entries). Required by rclone to avoid retries.
- `<MaxKeys>` — the max keys parameter (default 1000 if not specified). Required for pagination support.
- `<IsTruncated>` — whether more results are available (set to true if KeyCount == MaxKeys, false otherwise). Always include this field.

### Error Response

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Error>
  <Code>NoSuchKey</Code>
  <Message>The specified key does not exist.</Message>
  <Key>photos/missing.jpg</Key>
  <RequestId>req-001</RequestId>
</Error>
```

S3 error codes used: `NoSuchKey` (404), `NoSuchBucket` (404), `NoSuchUpload` (404), `AccessDenied` (403), `InvalidArgument` (400), `InternalError` (500), `SignatureDoesNotMatch` (403), `InvalidPartOrder` (400).


## Files to Create / Modify

| File | Action |
|---|---|
| `pythowncloud/s3_auth.py` | **NEW** — AWS Signature V4 verification |
| `pythowncloud/s3_xml.py` | **NEW** — S3 XML response builders |
| `pythowncloud/routers/s3.py` | **NEW** — All S3 endpoints |
| `pythowncloud/db.py` | Add `list_all_under(prefix: str)` — recursive file listing for flat S3 queries |
| `pythowncloud/config.py` | Add `s3_access_key`, `s3_secret_key`, `s3_region` |
| `pythowncloud/uploads.py` | **NEW** — Unified cleanup for both TUS and S3 multipart uploads |
| `pythowncloud/routers/tus.py` | Remove `cleanup_abandoned_uploads()` (moved to `uploads.py`) |
| `pythowncloud/main.py` | Mount S3 router, update cleanup imports to use unified function from `uploads.py` |


## Critical Implementation Notes

### Cleanup Function Generalization

The existing `cleanup_abandoned_uploads()` in `tus.py` globs `*.meta` — which will match both `tus-*.meta` and `s3-*.meta` files. However, they have different file structures:

- **TUS:** `.meta` + single `.part` file (one file, appended to)
- **S3:** `.meta` + multiple `.part.1`, `.part.2`, `.part.N` files

Create a new shared module `pythowncloud/uploads.py` with a unified cleanup function:

```python
async def cleanup_abandoned_uploads():
    """Clean up abandoned TUS and S3 uploads older than tus_max_age_hours."""
    uploads_dir = settings.tus_upload_path
    if not uploads_dir.exists():
        return

    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=settings.tus_max_age_hours)

    for meta_file in uploads_dir.glob("*.meta"):
        try:
            with open(meta_file) as f:
                meta = json.load(f)
            created_at = datetime.fromisoformat(meta["created_at"].replace("Z", "+00:00"))
            if created_at < cutoff:
                # TUS cleanup: delete .part file
                if meta_file.name.startswith("tus-"):
                    meta_file.with_suffix(".part").unlink(missing_ok=True)
                # S3 cleanup: delete all .part.N files
                elif meta_file.name.startswith("s3-"):
                    for part_file in uploads_dir.glob(meta_file.stem + ".part.*"):
                        part_file.unlink(missing_ok=True)
                # Clean up the metadata file itself
                meta_file.unlink(missing_ok=True)
                logger.info(f"Cleaned up abandoned upload {meta.get('upload_id', 'unknown')}")
        except Exception:
            logger.warning(f"Failed to process {meta_file} during cleanup", exc_info=True)
```

Then in `main.py`, import from the new module instead of from `tus.py`.

### CompleteMultipartUpload Atomicity

Step 5 (multipart completion) concatenates parts → moves to storage → upserts DB. If the server crashes or power-fails between concatenation and final placement, the file exists but is invisible (not in DB).

Use temp-file-then-rename for atomicity:

1. Concatenate parts to a **temporary** file with a `.tmp` suffix in the storage directory
2. Move (rename) the `.tmp` file to the final destination — this is atomic at the OS level
3. Then upsert to DB, invalidate cache, generate thumbnails

This ensures that either the file is fully in place (and DB will catch it on next scan) or it doesn't exist at all. A power loss during the rename itself is safe — the filesystem is journaled.

### ListObjectsV2 Prefix and Delimiter Edge Cases

The `prefix` parameter in S3 doesn't always match a full directory path. Handle these cases explicitly:

- `prefix=""` + `delimiter="/"` → list root-level items (calls `db.list_directory("")`)
- `prefix="photos/"` + `delimiter="/"` → list direct children of `photos/` (calls `db.list_directory("photos")`)
- `prefix="photos"` (no trailing slash) + `delimiter="/"` → list items with that prefix, but may not exist as a directory; return empty if no matches
- `prefix="photos/"` + no delimiter → list **all** items recursively under `photos/` (flat listing; requires `db.list_all_under(prefix)`)

Before calling `db.list_directory()`, normalize the prefix:
- Strip leading `/` if present
- Strip trailing `/` for the prefix parameter (but remember to re-add it in the `<Prefix>` response field)
- If the prefix doesn't exist as a directory and no files match it, return an empty listing (not an error)

**Critical:** The recursive flat listing case (no delimiter) is needed for `rclone sync` to reconcile the full remote state. You must implement `db.list_all_under(prefix: str) -> list[File]` which returns all files recursively under a prefix, **excluding directories** (`is_dir = 0`). In S3, flat listings include only real objects in `<Contents>` — directory markers never appear (they only show in `<CommonPrefixes>` of delimited listings). Similar to `SELECT * FROM files WHERE path LIKE 'photos/%' AND is_dir = 0`. This is different from `db.list_directory(prefix)` which only returns direct children (both files and directories).

### UNSIGNED-PAYLOAD and Content-Encoding: aws-chunked

S3 Signature V4 can use two payload modes:

1. **`UNSIGNED-PAYLOAD`** (common for HTTP, used by S3Drive and rclone defaults) — body hash is skipped, signature only verifies headers/method/path
2. **`aws-chunked`** (used by rclone with large files and HTTPS) — body is encoded with chunk size markers

For initial implementation, support `UNSIGNED-PAYLOAD` only. In the Signature V4 verifier, if `x-amz-content-sha256: UNSIGNED-PAYLOAD` is present, skip body hash validation.

For rclone compatibility during testing, advise users to disable chunked encoding:
```ini
[poc]
disable_checksum = true
upload_cutoff = 0
```

This forces rclone into simple PUT behavior initially.

### S3 Router Mounting Pattern

The existing `webdav.py` is mounted twice in `main.py` (once with prefix, once at root). **Do not follow this pattern for S3.** Mount the S3 router only once:

```python
app.include_router(s3.router, prefix="/s3")
```

S3 is always accessed at `/s3/...`, never at root.


## Implementation Order

### Step 1: AWS Signature V4 (`s3_auth.py`)

The hardest piece, but also self-contained and testable in isolation.

Implement `verify_s3_auth(request: Request) -> str` as a FastAPI dependency:

1. Parse the `Authorization` header to extract access key, signed headers, and signature.
2. Also handle query-string auth (`X-Amz-Algorithm` etc.) for pre-signed URL compatibility (optional).
3. Build the canonical request string from the request method, path, query, and signed headers.
4. Build the string-to-sign from the algorithm, timestamp, credential scope, and canonical request hash.
5. Derive the signing key: `HMAC(HMAC(HMAC(HMAC("AWS4" + secret, date), region), "s3"), "aws4_request")`.
6. Compute HMAC-SHA256 of the string-to-sign with the signing key.
7. Compare with the provided signature. Raise 403 `SignatureDoesNotMatch` if they differ.

**Dependencies:** `hmac`, `hashlib`, `urllib.parse` — all stdlib. No external packages.

**Test strategy:** Use `aws s3 ls --endpoint-url http://localhost:8000/s3 s3://storage/` from the AWS CLI. If auth passes and you get a valid ListObjectsV2 response, it works.

### Step 2: S3 XML Builders (`s3_xml.py`)

Build response XML for each endpoint. Use `xml.etree.ElementTree` (same as `webdav_xml.py`).

Functions needed:

```python
def build_list_buckets(owner: str) -> str
def build_list_objects_v2(bucket: str, prefix: str, delimiter: str,
                          objects: list, common_prefixes: list,
                          key_count: int, max_keys: int = 1000,
                          is_truncated: bool) -> str
def build_error(code: str, message: str, **extra) -> str
def build_initiate_multipart(bucket: str, key: str, upload_id: str) -> str
def build_complete_multipart(bucket: str, key: str, etag: str) -> str
def build_list_parts(bucket: str, key: str, upload_id: str, parts: list) -> str
```

### Step 3: Single-Object Operations (`routers/s3.py`)

Start with `PUT`, `GET`, `HEAD`, `DELETE` for individual objects, plus `ListBuckets` and `HeadBucket`.

These are near-identical to the WebDAV equivalents. The upload handler follows the same pattern:

```python
@router.put("/storage/{key:path}")
async def put_object(key: str, request: Request, _auth = Depends(verify_s3_auth)):
    target = safe_path(key)
    target.parent.mkdir(parents=True, exist_ok=True)

    size = 0
    h_sha256 = hashlib.sha256()
    h_md5 = hashlib.md5()
    try:
        with open(target, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
                h_sha256.update(chunk)
                h_md5.update(chunk)
                size += len(chunk)
    except ClientDisconnect:
        target.unlink(missing_ok=True)
        return s3_error_response(400, "RequestTimeout", "Client disconnected")

    # DB upsert, thumbnail, cache invalidation — same as webdav.py PUT
    ...

    return Response(status_code=200, headers={"ETag": f'"{h_md5.hexdigest()}"'})
```

Note: S3 uses MD5 for ETags (not SHA256), so we compute both — MD5 for the S3 ETag response, SHA256 for the database checksum.

### Step 4: ListObjectsV2

Map `db.list_directory()` to S3 XML. The tricky part is handling `prefix` and `delimiter` correctly:

- `prefix=photos/2025/` + `delimiter=/` → return files in `photos/2025/` as `<Contents>`, subdirectories as `<CommonPrefixes>`
- `prefix=photos/` + no delimiter → return **all** files recursively under `photos/` (flat listing)

For the common case (prefix + delimiter), `db.list_directory(prefix)` already returns direct children — directories become `<CommonPrefixes>`, files become `<Contents>`.

### Step 5: Multipart Upload

Implement the four multipart endpoints. Storage follows the `.uploads/` pattern described above.

**Initiate:** Generate upload ID, create `s3-{id}.meta`, return XML with upload ID.

**UploadPart:** Stream the part body to `s3-{id}.part.{N}`, compute MD5, return ETag.

**Complete:** Parse the XML body, verify parts, concatenate parts to temp file, move to storage, upsert DB, cleanup.

**Important:** Compute the SHA256 checksum **incrementally during concatenation**, not in a second read. As each part is read and written to the temp file, update the hash. This avoids an extra full-file read on the Pi, which saves ~17 seconds per 500 MB file over USB 2.0. Use the same pattern as `files.py` and `webdav.py` streaming uploads.

**Abort:** Delete all `s3-{id}.part.*` and `s3-{id}.meta`.

**ListParts:** Read the `.meta` file and return parts list as XML (needed for S3Drive resume).

### Step 6: Config + Wiring

Add settings to `config.py`, mount the router in `main.py`, and integrate the unified `cleanup_abandoned_uploads()` from `uploads.py` into the lifespan. Remove the old cleanup function from `tus.py` and import from the new shared module instead.

### Step 7: Client Testing

Configure and test with each client:

**rclone (all platforms):**

```bash
rclone config
# name: poc
# type: s3
# provider: Other
# access_key_id: pythowncloud
# secret_access_key: your-secret
# endpoint: http://100.93.58.13:8000/s3
# acl: private
```

```bash
rclone ls poc:storage/
rclone copy ~/large-video.mp4 poc:storage/videos/
# Kill rclone mid-transfer, restart → resumes from last part
```

**S3Drive (Android):**

```
Connection type: S3 Compatible
Endpoint: http://100.93.58.13:8000/s3
Bucket: storage
Access Key: pythowncloud
Secret Key: your-secret
Path Style: ON
Region: us-east-1
```

Set up auto media backup → take a photo → verify it appears in PythOwnCloud.

**AWS CLI (debugging):**

```bash
aws configure
# access key: pythowncloud
# secret key: your-secret
# region: us-east-1

aws --endpoint-url http://100.93.58.13:8000/s3 s3 ls s3://storage/
aws --endpoint-url http://100.93.58.13:8000/s3 s3 cp large.mp4 s3://storage/videos/
```


## Edge Cases and Gotchas

### Path-Style vs Virtual-Hosted-Style

S3 has two URL styles:
- **Path-style:** `http://server/bucket/key` — what we implement
- **Virtual-hosted:** `http://bucket.server/key` — requires DNS subdomains

S3Drive and rclone both support path-style. Configure both clients with `Path Style: ON` (rclone: `force_path_style = true`). PythOwnCloud does not need to support virtual-hosted style.

### ETag Format

S3 ETags for single-part uploads are the MD5 hex digest wrapped in double quotes: `"d41d8cd98f00b204e9800998ecf8427e"`. For multipart uploads, the ETag is `"md5-of-part-md5s-partcount"` (e.g., `"a1b2c3-3"`). Both rclone and S3Drive expect this format for resume verification.

### Content-SHA256 Header

S3 requests include `x-amz-content-sha256` header. For unsigned payloads (common with rclone and S3Drive), the value is the literal string `UNSIGNED-PAYLOAD`. The Signature V4 verifier must accept this and skip body hash verification when it appears.

### Empty Directories

S3 doesn't have real directories — they're simulated by keys ending in `/` (e.g., `photos/2025/`). When `MKCOL` or `mkdir` is needed, some clients `PUT` a zero-byte object with a trailing-slash key. The S3 router should detect this and call `mkdir` instead of creating a file. Additionally, the directory must be recorded in the database via `db.upsert_file(..., is_dir=True, size=0, checksum="")` to match WebDAV's MKCOL pattern — without the DB record, the directory exists on disk but is invisible to `db.list_directory()` and subsequent S3 ListObjectsV2 queries.

### Request Routing

FastAPI needs to distinguish between:
- `POST /s3/storage/key?uploads` → initiate multipart
- `POST /s3/storage/key?uploadId=X` → complete multipart
- `PUT /s3/storage/key` → simple upload
- `PUT /s3/storage/key?partNumber=N&uploadId=X` → upload part

This is handled by checking query parameters inside the handler, not by separate route definitions. A single `PUT` handler inspects the presence of `partNumber`/`uploadId` to decide the code path.


## New Dependencies

None. AWS Signature V4 uses only `hmac`, `hashlib`, and `urllib.parse` from the standard library. XML is built with `xml.etree.ElementTree` (same as WebDAV).


## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| Signature V4 is complex and easy to get wrong | Test with `aws s3` CLI first — it gives clear error messages. Log the canonical request and string-to-sign at debug level for troubleshooting. |
| S3Drive sends requests with quirks not in the spec | Test incrementally. Start with rclone (well-behaved), then S3Drive. Log raw requests during development. |
| Multipart parts fill up the Pi's disk | Same 24h cleanup as TUS. Also enforce a max parts count (10,000 per AWS spec, but lower is fine). |
| MD5 computation adds CPU overhead | MD5 is fast even on ARM. The Pi can hash at ~200 MB/s, well above the USB 2.0 write speed (~30 MB/s). Computing both MD5 and SHA256 in the streaming loop is fine. |
| ListObjectsV2 pagination for large directories | Implement `MaxKeys` (default 1000) and `ContinuationToken`. For a home media server with hundreds of files per directory (not millions), pagination is unlikely to be needed but simple to add. |
| `UNSIGNED-PAYLOAD` weakens auth | Acceptable over Tailscale. The signature still verifies the request headers, method, and path — it just skips body verification. This is standard S3 behavior for HTTP (non-HTTPS) connections. |


## What This Replaces

After Phase 5.2, the upload matrix looks like this:

| Scenario | Before | After |
|----------|--------|-------|
| Phone video (500 MB, flaky connection) | WebDAV PUT → full restart on drop | S3 multipart → lose at most one 5 MB part |
| Phone photo (3 MB) | WebDAV PUT → works fine | S3 PUT → works fine (no multipart needed) |
| rclone bulk sync (Windows) | WebDAV PUT → full restart per file | S3 multipart → resume per file |
| rclone bulk sync (Linux) | WebDAV PUT → full restart per file | S3 multipart → resume per file |
| Browser upload (web UI) | TUS → resumable | TUS → resumable (unchanged) |
| File manager (Finder/Nautilus) | WebDAV → works | WebDAV → works (unchanged) |

WebDAV is not replaced — it's still needed for native file manager integration. S3 runs alongside it for clients that benefit from resumable uploads.


## Client Configuration Reference

### S3Drive (Android)

| Setting | Value |
|---------|-------|
| Connection Type | S3 Compatible |
| Endpoint | `http://<tailscale-ip>:8000/s3` |
| Bucket | `storage` |
| Access Key | Value of `POC_S3_ACCESS_KEY` |
| Secret Key | Value of `POC_S3_SECRET_KEY` |
| Region | `us-east-1` |
| Path Style | ON |

### rclone (all platforms)

```ini
[poc]
type = s3
provider = Other
access_key_id = pythowncloud
secret_access_key = your-secret
endpoint = http://<tailscale-ip>:8000/s3
force_path_style = true
```

### AWS CLI (debugging)

```bash
export AWS_ACCESS_KEY_ID=pythowncloud
export AWS_SECRET_ACCESS_KEY=your-secret
export AWS_DEFAULT_REGION=us-east-1
alias pocs3='aws --endpoint-url http://<tailscale-ip>:8000/s3 s3'

pocs3 ls s3://storage/
pocs3 cp large.mp4 s3://storage/videos/
```


## Success Criteria

Phase 5.2 is complete when:

1. `aws s3 ls --endpoint-url ... s3://storage/` returns a valid object listing
2. `aws s3 cp` uploads a file and it appears in the web UI and database
3. rclone configured as S3 can `ls`, `copy`, `sync`, and `mount`
4. Interrupting an rclone upload of a 500 MB file and restarting completes without re-uploading finished parts
5. S3Drive on Android connects, lists files, and auto-uploads photos
6. S3Drive uploading a large video survives a network switch (WiFi → mobile) without restarting
7. Abandoned multipart uploads are cleaned up after 24 hours
8. WebDAV and TUS endpoints continue working unchanged
9. Memory usage remains under 64 MB during multipart upload assembly
10. All three APIs (REST, WebDAV, S3) coexist on the same port
