# dx2-comfyui-s3-tools

ComfyUI custom node package for uploading generated images, videos, and audio to
S3-compatible storage such as AWS S3, RunPod Network Storage, and MinIO.

## Nodes

### DX2 Upload Media to S3 (`DX2UploadMediaToS3`)

Uploads native ComfyUI images, videos, and audio, existing local files, or
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
| `upload_workflow` | BOOLEAN | No | Upload a JSON workflow-provenance sidecar under the destination's `workflows/` folder (default: `true`). |
| `audio` | AUDIO | No | Decoded ComfyUI audio, including MiniMax Music 3 and YuE2 workflows. Each mono/stereo track in the batch is uploaded as 24-bit FLAC. |
| `embed_metadata` | BOOLEAN | No | Embed prompt/workflow metadata in native PNG, MP4, and FLAC files (default: `true`). Independent of `upload_workflow`; respects ComfyUI's `--disable-metadata`. |

At least one media source must be connected. When multiple sources are
provided, priority is `image`, then `video`, then `audio`, then `local_path`, then
`vhs_filenames`.

Native images are always encoded as PNG. An extension in `file_name` is ignored
for native images. A multi-image batch is uploaded as separate, sequentially
numbered PNG files.

Native audio is encoded with PyAV as 24-bit FLAC, preserving the original sample
rate and mono/stereo layout. An extension in `file_name` is ignored for native
audio. Multi-track batches produce separate, sequentially numbered FLAC files.
Only tracks with samples outside `[-1, 1]` produce two versions:

- `song-<timestamp>-clipped.flac`: samples are hard-clamped to `[-1, 1]`.
- `song-<timestamp>-gain-reduced.flac`: the original waveform is multiplied by
  one constant gain, `10^(-1/20) / original_peak`, across all channels and samples.
  This preserves dynamics and stereo balance while reducing the sample peak to
  -1 dBFS. It is not a true-peak limiter and cannot undo distortion already
  present in the generated waveform.

Both versions are encoded directly from the original waveform and share a
timestamp and source batch number. Each receives its own matching workflow
sidecar. Sidecar `media.audio_processing` records `variant`, `original_peak`,
`original_out_of_range_samples`, `sample_count`, `gain`, `gain_db`,
`target_peak_dbfs`, and `clipped_samples`. Counts include each channel separately;
`batch_index` and `batch_count` describe source tracks, not exported variants.
Warning logs report the original peak, clipped sample count, and applied gain.

In-range tracks, including silence and peaks between -1 dBFS and full scale,
produce one unchanged, normally named file. Audio is never resampled or downmixed.
NaN and infinite samples fail before any S3 upload.
The single-URI output returns the last uploaded media file: the gain-reduced
version if the final source track is over range. Find both versions by their
shared filename prefix in S3.
FLAC compression is lossless, while conversion from floating-point samples to
24-bit PCM introduces quantization. Input tensors are never modified.

#### Output

| Name | Type | Description |
|---|---|---|
| `upload_info` | STRING | S3 URI of the uploaded file, or the final file in an IMAGE/AUDIO batch. Returns `"upload_skipped"` when disabled. |

Examples:

```text
s3://my-bucket/media/test-20260816T193012_123456Z.mp4
```

```text
s3://my-bucket/image/ComfyUI-20260816T193012_123456Z.png
```

#### Embedded workflow metadata

Native IMAGE, VIDEO, and AUDIO uploads embed provenance in the media file by
default, so it travels with the downloaded file:

| Output | Storage |
|---|---|
| PNG | PNG text chunks written with Pillow, using ComfyUI's `prompt` and `workflow` keys. |
| FLAC | Vorbis comment fields written during the existing PyAV encode. |
| MP4 | Container tags written by ComfyUI's native `VIDEO.save_to(..., metadata=...)`. |

`prompt` contains the executed API graph; `workflow` contains the editable UI
graph when supplied in `EXTRA_PNGINFO`. API callers that only send an execution
graph get `prompt` without an invented `workflow`. Other JSON-compatible extra
fields are included. The executed prompt takes precedence over an extra field
named `prompt`. Invalid fields are skipped with a warning, retaining valid fields.
Portable tag names must be 1–79 printable ASCII characters, excluding `=`, with
no leading, trailing, or consecutive spaces.

Embedding uses the existing encoding step and does not change image pixels,
audio precision, sample rate, or video encoding settings. Each audio variant is
encoded once from the source waveform. Existing `local_path` and VHS files are uploaded byte-for-byte,
including whatever metadata their original saver wrote.

Set `embed_metadata=false` or start ComfyUI with `--disable-metadata` to stop
adding embedded fields. These settings do not strip pre-existing metadata or
disable sidecars: `upload_workflow` controls sidecars separately. To disable both
forms of new provenance, set both node options to `false`.

Embedded metadata and S3 object headers are separate. The S3 console's metadata
panel will show `ContentType`, not these in-file fields. After downloading a
newly generated file, read the fields back, for example:

```bash
ffprobe -v error -show_entries format_tags=prompt,workflow -of json downloaded.flac
ffprobe -v error -show_entries format_tags=prompt,workflow -of json downloaded.mp4
python -c "from PIL import Image; import json; im = Image.open('downloaded.png'); print({k: json.loads(im.info[k]) for k in ('prompt', 'workflow') if k in im.info})"
```

Confirm the graph and generation parameters match the submitted workflow.
Editors can remove metadata when exporting, and automatic workflow import
depends on the ComfyUI frontend's support for the file type. Keep the sidecar
as a separate provenance copy.

#### Workflow provenance sidecars

When `upload_workflow` is `true`, each media object receives a same-stem
`.workflow.json` companion in a `workflows/` child folder:

```text
s3://my-bucket/media/test-20260816T193012_123456Z.mp4
s3://my-bucket/media/workflows/test-20260816T193012_123456Z.workflow.json
```

The separate folder prevents media filename-prefix searches from also
returning workflow sidecars.

The versioned JSON envelope contains the media URI and batch position, the
complete prompt executed by ComfyUI, the editable UI workflow when the client
provides one, and any other `EXTRA_PNGINFO` supplied by extensions. API callers
that do not submit an editable workflow still receive a sidecar with
`workflow: null`. Image/audio batches receive one sidecar per PNG/FLAC.

Sidecars are best-effort: serialization or sidecar-upload failures are logged,
but a successful media upload still returns its media URI. Set
`upload_workflow` to `false` to upload only media.

> [!WARNING]
> Workflow metadata can contain prompts, local paths, model names, and values
> entered into third-party node widgets. Review workflows for credentials or
> other sensitive values before sharing media with embedded metadata or sidecars.

#### S3 content types

Every upload explicitly sets the object's `ContentType`, including workflow
sidecars. Native PNG, MP4, and FLAC uploads use `image/png`, `video/mp4`, and
`audio/flac`; sidecars use `application/json`.

Existing local files and VHS output use their source filename extension, with
a fixed mapping for common media formats, MIME inference for other extensions,
and `application/octet-stream` for unknown types. This is extension-based
inference, not inspection of file contents. A renamed S3 destination does not
change the inferred type or transcode the source file, so use a matching extension
in `file_name`. Compressed files with an inferred content encoding fall back to
`application/octet-stream` rather than advertising the uncompressed inner type.

This applies to new uploads only. Previously uploaded objects are not updated.
`ContentType` is independent of embedded provenance. The uploader does not add
custom S3 metadata or download headers.

## Installation

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/mluciani/dx2-comfyui-s3-tools.git
pip install -r dx2-comfyui-s3-tools/requirements.txt
```

Restart ComfyUI after installation.

Native audio requires PyAV (`av`), PyTorch, and NumPy from the ComfyUI runtime.
They are imported only when audio is selected. There is no ComfyUI save-helper
dependency or external FFmpeg executable requirement.

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
    "enabled": true,
    "upload_workflow": true
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

### Native audio (MiniMax Music 3, YuE2, and other AUDIO workflows)

Connect the final decoded or processed `AUDIO` output directly to the uploader;
do not connect a latent or conditioning output. A separate Save Audio node is
optional and is not used by the uploader:

```text
VAE Decode Audio / audio processor -> audio -> DX2UploadMediaToS3
```

```json
{
  "class_type": "DX2UploadMediaToS3",
  "inputs": {
    "audio": ["16", 0],
    "s3_path": "audio/yue2",
    "file_name": "song",
    "enabled": true,
    "upload_workflow": true
  }
}
```

PyAV writes temporary FLAC files, which follow the same S3 naming, upload,
workflow-sidecar, and cleanup path as other media. Files are encoded in chunks
to bound additional conversion memory. For a batch, all tracks are materialized
before uploading, so temporary disk usage grows with batch size and doubles
for tracks needing both versions. Keep the
default `s3_path="media"` or choose any folder such as `audio/yue2`.

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

## Upgrading from v0.3.0

Version 0.4.0 enables workflow sidecars by default. Existing API and UI
workflows may omit `upload_workflow` and inherit `true`; set it explicitly to
`false` to retain media-only uploads.

## Error handling

| Situation | Behavior |
|---|---|
| No media source | Raises `ValueError` describing the supported inputs. |
| Invalid or empty IMAGE batch | Raises `ValueError` before contacting S3. |
| Invalid AUDIO, unsupported channels, or nonfinite samples | Raises `ValueError` before contacting S3. All generated temporary files are removed. |
| Finite AUDIO samples outside `[-1, 1]` | Uploads both `*-clipped.flac` and `*-gain-reduced.flac` from the original waveform, with matching sidecars and processing details. The reduced version targets a -1 dBFS sample peak. |
| Missing native-audio dependencies | Raises an audio-specific `RuntimeError`; other source types remain usable. |
| File not found | Raises `FileNotFoundError` with the resolved path. |
| Missing credentials | Raises `EnvironmentError` naming the missing configuration. |
| Media upload failure | Raises `RuntimeError` with `s3://bucket/key` context. |
| Workflow sidecar failure | Logs a warning and returns the successful media URI. |
| Non-JSON embedded field or unsupported tag name | Logs a warning and skips that field, retaining other valid fields. |
| `enabled` is `false` | Returns `"upload_skipped"` without side effects. |

If an image/audio batch fails partway through, already-uploaded objects remain in S3;
all local temporary files are still removed.

## Development validation

Use an isolated Python environment. If it does not already have PyTorch, install
the appropriate build first (a CPU build is sufficient for tests), then run:

```bash
python -m pip install -r tests/requirements.txt
python -m pytest -q
```

Audio tests use real PyTorch tensors and PyAV encoding, with independent
libsndfile decoding through SoundFile. They verify 24-bit precision, sample rate,
channel order, frame count, quantization error, and input immutability. S3 calls
are mocked; content-type arguments, naming, sidecars, and failure cleanup are tested.

Metadata tests read prompt/workflow fields back from real PNG and FLAC upload
bytes and compare decoded media with embedding on/off. MP4 integration tests
exercise ComfyUI's actual tensor-backed saver and file-backed remuxer, including
8-bit/10-bit video with audio and identical encoded packets with embedding
on/off. Those four tests run when ComfyUI is available on `PYTHONPATH`; otherwise
they are explicitly skipped. Unit tests still verify the VIDEO metadata call.

Before declaring live compatibility, load an existing UI workflow and check its
connections, run a short MiniMax Music 3 and YuE2 generation through the uploader,
inspect the uploaded objects' `ContentType`, then download and import a FLAC into
Audacity. These checks require the target ComfyUI environment and S3 access.
Also download newly uploaded PNG/MP4/FLAC files and inspect their embedded
`prompt` and supplied `workflow` fields using the commands above. Test workflow
import in the deployed frontend separately from metadata readback.
