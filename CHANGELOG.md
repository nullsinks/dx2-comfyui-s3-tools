# Changelog

## Unreleased

- Embed ComfyUI prompt, supplied editable workflow, and compatible extra fields
  in native PNG, MP4, and FLAC outputs during their existing serialization step.
  Added a default-on `embed_metadata` input without shifting existing inputs;
  respects ComfyUI's `--disable-metadata` independently of sidecar uploads.
  Existing local/VHS files retain their original bytes. Invalid embedded fields
  are logged and skipped. Added metadata readback and media-preservation tests.
- Added an optional native `AUDIO` input for mono/stereo batches, encoded as
  24-bit FLAC with PyAV at the source sample rate. Existing input positions and
  the single-URI return contract are preserved.
- Reused media naming, per-track workflow sidecars, and temporary-file cleanup
  for audio. Invalid audio and nonfinite samples fail before uploading.
- Finite out-of-range audio produces both `-clipped.flac` and
  `-gain-reduced.flac` directly from the original waveform. The reduced version
  uses one constant gain across channels to target a -1 dBFS sample peak.
  Variants share their timestamp and source batch index; matching sidecars
  record peaks, sample counts, gain, and processing variant. Warning logs
  report the original peak, clipped count, and reduced gain. Clean tracks and
  input tensors are unchanged. Real FLAC decoding tests cover mixed batches,
  stereo balance, chunk boundaries, metadata, and failure cleanup.
- Set explicit S3 `ContentType` values for all media and workflow sidecars,
  using source extensions for existing files and a binary fallback for unknown
  formats. Previously uploaded objects are unchanged.
- Added real audio encoding/independent decoding tests and corrected three
  existing tuple-return assertions in the regression suite.

## 0.4.1

- Moved workflow provenance sidecars into a dedicated `workflows/` child
  folder so media filename-prefix searches do not also return sidecars.

## 0.4.0

- Added default-on `.workflow.json` provenance sidecars for every uploaded
  image and video.
- Captured ComfyUI's executed prompt, editable workflow when available, and
  extension-provided metadata in a versioned JSON envelope.
- Added the `upload_workflow` boolean input for explicit opt-out.
- Kept sidecar failures best-effort so successful media uploads still return
  their media URI.

## 0.3.0

- Replaced `DX2UploadVideoToS3` with the generalized
  `DX2UploadMediaToS3` node.
- Added native ComfyUI IMAGE input and PNG batch uploads.
- Changed the default `s3_path` from `videos` to `media`.
- Kept `SaveImage` optional; connect the uploader directly to the upstream
  IMAGE output.

This release intentionally does not register the old node ID. Update API
workflow `class_type` values and recreate/reconnect the node in UI workflows,
or remain pinned to `v0.2.0`.
