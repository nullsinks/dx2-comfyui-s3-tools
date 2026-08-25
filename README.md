# dx2-comfyui-s3-tools

ComfyUI custom node package for uploading generated images and videos to
S3-compatible storage such as AWS S3, RunPod Network Storage, and MinIO.

## Nodes

### DX2 Upload Media to S3 (`DX2UploadMediaToS3`)

Uploads native ComfyUI images and videos, existing local files, or
VideoHelperSuite output to an S3-compatible bucket.

#### Inputs

| Name | Type | Required | Description |
|---|---|---|---|
| `image` | IMAGE | No | Any native ComfyUI image output. Every image in the batch is uploaded as PNG. |
| `video` | VIDEO | No | Native video output from ComfyUI's built-in `CreateVideo` node. |
| `local_path` | STRING | No | Explicit filesystem path to an existing media file. |
| `vhs_filenames` | VHS_FILENAMES | No | Output of `VHS_VideoCombine`. The last file in the list is used. |
| `s3_path` | STRING | No | Destination folder within the bucket (default: `media`). Use `image` or `videos` for type-specific folders. |
| `file_name` | STRING | No | Optional filename stem. A UTC timestamp is appended to prevent collisions. |
| `enabled` | BOOLEAN | No | Set to `false` to skip uploading and return `"upload_skipped"` (default: `true`). |

At least one media source must be connected. When multiple sources are
provided, priority is `image`, then `video`, then `local_path`, then
`vhs_filenames`.

Native images are always encoded as PNG. An extension in `file_name` is ignored
for native images. A multi-image batch is uploaded as separate, sequentially
numbered PNG files.

#### Output

| Name | Type | Description |
|---|---|---|
| `upload_info` | STRING | S3 URI of the uploaded file, or the final file in an IMAGE batch. Returns `"upload_skipped"` when disabled. |

Examples:

```text
s3://my-bucket/media/test-20260816T193012_123456Z.mp4
```

```text
s3://my-bucket/image/ComfyUI-20260816T193012_123456Z.png
```

## Installation

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/mluciani/dx2-comfyui-s3-tools.git
pip install -r dx2-comfyui-s3-tools/requirements.txt
```

Restart ComfyUI after installation.

## Configuration

All credentials are read from environment variables; nothing is hard-coded in
the workflow JSON.

| Variable | Required | Description |
|---|---|---|
| `S3_BUCKET` | Yes | Destination bucket name. |
| `AWS_ACCESS_KEY_ID` | Yes | Access key ID or compatible credential. |
| `AWS_SECRET_ACCESS_KEY` | Yes | Secret access key. |
| `S3_ENDPOINT_URL` | No | Custom endpoint for S3-compatible stores. Omit for standard AWS S3. |
| `S3_REGION` | No | AWS region (default: `us-east-1`). |

Example for RunPod:

```bash
export S3_BUCKET=my-runpod-bucket
export S3_ENDPOINT_URL=https://s3.runpod.io
export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
```

## Wiring examples

### Native image

Connect the final IMAGE-producing node directly to the uploader. `SaveImage` is
not required:

```text
VAEDecode/Image Processor -> image -> DX2UploadMediaToS3
```

To save locally as well, connect both output nodes to the same IMAGE source:

```text
                         +-> SaveImage
IMAGE-producing node ----+
                         +-> DX2UploadMediaToS3
```

API workflow example:

```json
{
  "class_type": "DX2UploadMediaToS3",
  "inputs": {
    "image": ["16", 0],
    "s3_path": "image",
    "file_name": "ComfyUI",
    "enabled": true
  }
}
```

### Native video

```text
CreateVideo --+-> SaveVideo
              +-> video -> DX2UploadMediaToS3
```

The uploader writes native media to temporary files for transfer and removes
them after the upload attempt. Temporary filenames never leak into S3 keys.

### VideoHelperSuite

```text
VHS_VideoCombine -> vhs_filenames -> DX2UploadMediaToS3
```

### Existing local file

```text
(any STRING node) -> local_path -> DX2UploadMediaToS3
```

## Upgrading from v0.2.0

Version 0.3.0 intentionally replaces `DX2UploadVideoToS3` with
`DX2UploadMediaToS3`; the legacy node ID is not registered in the latest
release.

- API workflows must change `class_type` from `DX2UploadVideoToS3` to
  `DX2UploadMediaToS3`.
- UI workflows must remove the missing legacy node, add `DX2 Upload Media to
  S3`, and reconnect its inputs and output.
- The default `s3_path` changed from `videos` to `media`. Set it to `videos` to
  retain the old destination.
- Workflows that are not migrated should remain pinned to release tag `v0.2.0`.

## Error handling

| Situation | Behavior |
|---|---|
| No media source | Raises `ValueError` describing the supported inputs. |
| Invalid or empty IMAGE batch | Raises `ValueError` before contacting S3. |
| File not found | Raises `FileNotFoundError` with the resolved path. |
| Missing credentials | Raises `EnvironmentError` naming the missing configuration. |
| Upload failure | Raises `RuntimeError` with `s3://bucket/key` context. |
| `enabled` is `false` | Returns `"upload_skipped"` without side effects. |

If an image batch fails partway through, already-uploaded objects remain in S3;
all local temporary files are still removed.
