# Changelog

## Unreleased

- Added an optional native `AUDIO` input for mono/stereo batches, encoded as
  24-bit FLAC with PyAV at the source sample rate. Existing input positions and
  the single-URI return contract are preserved.
- Reused media naming, per-track workflow sidecars, and temporary-file cleanup
  for audio. Invalid audio and out-of-range samples fail before uploading.
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
