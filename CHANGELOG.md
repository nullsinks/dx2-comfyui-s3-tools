# Changelog

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
