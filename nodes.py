"""
DX2 ComfyUI S3 Tools – nodes.py

Provides:
  DX2UploadMediaToS3 – uploads generated media to an S3-compatible bucket.

The node accepts ComfyUI's native IMAGE or VIDEO type, a plain STRING file
path, or the VHS_FILENAMES output of ComfyUI-VideoHelperSuite's VideoCombine
node.

Required environment variables:
  S3_BUCKET              – destination bucket name
  AWS_ACCESS_KEY_ID      – access key (or compatible credential)
  AWS_SECRET_ACCESS_KEY  – secret key

Optional environment variables:
  S3_ENDPOINT_URL  – custom endpoint for S3-compatible stores (RunPod, MinIO, …)
  S3_REGION        – region name (default: us-east-1)
"""

import json
import os
import logging
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import boto3
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class DX2UploadMediaToS3:
    """ComfyUI node: upload generated media to an S3-compatible bucket.

    Wiring options
    --------------
    - Connect any ComfyUI IMAGE output to *image*.
    - Connect the VIDEO output of ComfyUI's CreateVideo to *video*.
    - Connect a plain file-path string to *local_path*.
    - Connect the VHS_FILENAMES output of VHS_VideoCombine to *vhs_filenames*
      (the last file in the list is used).
    - All source inputs are optional individually; at least one must be provided.
      Priority is *image*, then *video*, then *local_path*, then
      *vhs_filenames*.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {},
            "optional": {
                "image": ("IMAGE",),
                "video": ("VIDEO",),
                "local_path": (
                    "STRING",
                    {"default": "", "multiline": False},
                ),
                # VHS_FILENAMES is a (save_output: bool, filepaths: list[str]) tuple
                # emitted by ComfyUI-VideoHelperSuite's VHS_VideoCombine node.
                "vhs_filenames": ("VHS_FILENAMES",),
                "s3_path": (
                    "STRING",
                    {"default": "media", "multiline": False},
                ),
                "file_name": (
                    "STRING",
                    {"default": "", "multiline": False},
                ),
                "enabled": ("BOOLEAN", {"default": True}),
                "upload_workflow": ("BOOLEAN", {"default": True}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("upload_info",)
    FUNCTION = "upload_media"
    CATEGORY = "DX2/IO"
    OUTPUT_NODE = True

    # ------------------------------------------------------------------
    # Main execution
    # ------------------------------------------------------------------

    def upload_media(
        self,
        local_path: str = "",
        vhs_filenames=None,
        s3_path: str = "media",
        file_name: str = "",
        enabled: bool = True,
        upload_workflow: bool = True,
        video=None,
        image=None,
        prompt=None,
        extra_pnginfo=None,
    ):
        """Upload media to S3 and return destination information.

        Parameters
        ----------
        local_path:
            Explicit filesystem path to an existing media file (STRING input).
        vhs_filenames:
            VHS_FILENAMES payload from VHS_VideoCombine:
            ``(save_output: bool, filepaths: list[str])``.
            The last filepath in the list is used.
        s3_path:
            Destination folder within the bucket (default: ``media``).
        file_name:
            Optional user-facing filename. A UTC timestamp is appended to keep
            uploads collision-safe. The source extension is used when omitted.
        enabled:
            Set to *False* to skip the upload and return ``"upload_skipped"``.
        upload_workflow:
            Upload a ``.workflow.json`` provenance sidecar for every media
            object. Sidecar failures are logged without failing the media
            upload.
        video:
            Native ComfyUI VIDEO object. It is serialized to a temporary MP4,
            which is removed after the upload attempt.
        image:
            Native ComfyUI IMAGE tensor. Each batch item is serialized to a
            temporary PNG, which is removed after the upload attempt.
        """
        if not enabled:
            logger.info("DX2UploadMediaToS3: upload disabled – skipping.")
            return ("upload_skipped",)

        temporary_paths = []
        try:
            # --------------------------------------------------------------
            # 1. Resolve or materialize the local file path
            # --------------------------------------------------------------
            is_image_upload = image is not None
            if is_image_upload:
                resolved_paths = self._materialize_images(image, temporary_paths)
                resolved_path = resolved_paths[0]
            elif video is not None:
                file_descriptor, temporary_path = tempfile.mkstemp(suffix=".mp4")
                os.close(file_descriptor)
                temporary_paths.append(temporary_path)
                video.save_to(temporary_path)
                resolved_path = temporary_path
                resolved_paths = [resolved_path]
            else:
                resolved_path = self._resolve_path(local_path, vhs_filenames)
                resolved_paths = [resolved_path]

            logger.info("DX2UploadMediaToS3: resolved local paths → %s", resolved_paths)

            for resolved_path in resolved_paths:
                if not os.path.isfile(resolved_path):
                    raise FileNotFoundError(
                        f"DX2UploadMediaToS3: file not found: {resolved_path}"
                    )

            # --------------------------------------------------------------
            # 2. Read S3 configuration from environment
            # --------------------------------------------------------------
            bucket = os.environ.get("S3_BUCKET", "").strip()
            endpoint_url = os.environ.get("S3_ENDPOINT_URL", "").strip() or None
            region = os.environ.get("S3_REGION", "us-east-1").strip()
            access_key = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
            secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()

            if not bucket:
                raise EnvironmentError(
                    "DX2UploadMediaToS3: S3_BUCKET environment variable is not set."
                )
            if not access_key or not secret_key:
                raise EnvironmentError(
                    "DX2UploadMediaToS3: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY "
                    "must both be set."
                )

            # --------------------------------------------------------------
            # 3. Build S3 keys independently from local source names. Native
            #    media uses random temporary files, but that implementation
            #    detail must not leak into S3 keys.
            # --------------------------------------------------------------
            timestamp = datetime.now(timezone.utc).strftime(
                "%Y%m%dT%H%M%S_%fZ"
            )
            normalized_s3_path = self._normalize_s3_path(s3_path)
            upload_jobs = []
            is_batch = is_image_upload and len(resolved_paths) > 1
            for index, resolved_path in enumerate(resolved_paths):
                filename = self._build_destination_filename(
                    source_path=resolved_path,
                    requested_name=file_name,
                    timestamp=timestamp,
                    forced_suffix=".png" if is_image_upload else None,
                    batch_index=index if is_batch else None,
                )
                s3_key = f"{normalized_s3_path}/{filename}"
                upload_jobs.append((resolved_path, s3_key, index))

            # --------------------------------------------------------------
            # 4. Upload
            # --------------------------------------------------------------
            s3_client = self._build_s3_client(
                endpoint_url, region, access_key, secret_key
            )
            upload_info = ""
            for resolved_path, s3_key, index in upload_jobs:
                logger.info(
                    "DX2UploadMediaToS3: uploading to "
                    "s3://%s/%s (endpoint: %s)",
                    bucket,
                    s3_key,
                    endpoint_url or "default AWS endpoint",
                )
                self._upload(s3_client, resolved_path, bucket, s3_key)
                upload_info = f"s3://{bucket}/{s3_key}"

                if upload_workflow:
                    sidecar_key = self._build_sidecar_key(s3_key)
                    try:
                        sidecar_path = self._materialize_workflow_sidecar(
                            media_s3_uri=upload_info,
                            media_filename=PurePosixPath(s3_key).name,
                            batch_index=index + 1,
                            batch_count=len(upload_jobs),
                            captured_at=timestamp,
                            prompt=prompt,
                            extra_pnginfo=extra_pnginfo,
                            temporary_paths=temporary_paths,
                        )
                        logger.info(
                            "DX2UploadMediaToS3: uploading workflow sidecar to "
                            "s3://%s/%s",
                            bucket,
                            sidecar_key,
                        )
                        self._upload(s3_client, sidecar_path, bucket, sidecar_key)
                    except Exception:
                        logger.warning(
                            "DX2UploadMediaToS3: media uploaded, but workflow "
                            "sidecar failed for s3://%s/%s",
                            bucket,
                            sidecar_key,
                            exc_info=True,
                        )

            logger.info("DX2UploadMediaToS3: upload succeeded → %s", upload_info)
            return (upload_info,)
        finally:
            for temporary_path in temporary_paths:
                try:
                    os.remove(temporary_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.warning(
                        "DX2UploadMediaToS3: failed to remove temporary file %s",
                        temporary_path,
                        exc_info=True,
                    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_path(local_path: str, vhs_filenames) -> str:
        """Return the filesystem path to upload.

        Priority: explicit *local_path* string > last path in *vhs_filenames*.
        """
        if local_path and local_path.strip():
            return local_path.strip()

        if vhs_filenames is not None:
            # VHS_FILENAMES shape: (save_output: bool, filepaths: list[str])
            try:
                _, filepaths = vhs_filenames
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "DX2UploadMediaToS3: vhs_filenames is not a valid VHS_FILENAMES "
                    f"payload (expected a 2-tuple): {exc}"
                ) from exc

            if filepaths:
                return filepaths[-1]

        raise ValueError(
            "DX2UploadMediaToS3: no media provided. "
            "Connect image (IMAGE), video (VIDEO), local_path (STRING), or "
            "vhs_filenames (VHS_FILENAMES)."
        )

    @staticmethod
    def _materialize_images(image, temporary_paths) -> list[str]:
        """Serialize every IMAGE batch item to a temporary PNG file."""
        try:
            shape = tuple(image.shape)
        except (AttributeError, TypeError) as exc:
            raise ValueError(
                "DX2UploadMediaToS3: image must be a ComfyUI IMAGE tensor."
            ) from exc

        if len(shape) != 4:
            raise ValueError(
                "DX2UploadMediaToS3: image must have shape "
                "[batch, height, width, channels]."
            )
        if shape[0] < 1:
            raise ValueError("DX2UploadMediaToS3: image batch is empty.")
        if shape[-1] not in (1, 3, 4):
            raise ValueError(
                "DX2UploadMediaToS3: image must have 1, 3, or 4 channels."
            )

        # NumPy and Pillow are provided by the ComfyUI runtime. Import them
        # lazily so path and video uploads do not add an import-time dependency.
        import numpy as np
        from PIL import Image

        resolved_paths = []
        for image_tensor in image:
            file_descriptor, temporary_path = tempfile.mkstemp(suffix=".png")
            os.close(file_descriptor)
            temporary_paths.append(temporary_path)

            if hasattr(image_tensor, "detach"):
                image_tensor = image_tensor.detach()
            image_array = image_tensor.cpu().numpy()
            image_array = np.clip(255.0 * image_array, 0, 255).astype(np.uint8)
            if shape[-1] == 1:
                image_array = image_array[..., 0]
            Image.fromarray(image_array).save(temporary_path, format="PNG")
            resolved_paths.append(temporary_path)

        return resolved_paths

    @staticmethod
    def _build_sidecar_key(media_s3_key: str) -> str:
        """Return the same S3 key stem with a ``.workflow.json`` suffix."""
        return str(PurePosixPath(media_s3_key).with_suffix(".workflow.json"))

    @staticmethod
    def _materialize_workflow_sidecar(
        *,
        media_s3_uri: str,
        media_filename: str,
        batch_index: int,
        batch_count: int,
        captured_at: str,
        prompt,
        extra_pnginfo,
        temporary_paths,
    ) -> str:
        """Write a versioned workflow-provenance envelope to a temp file."""
        metadata = extra_pnginfo if isinstance(extra_pnginfo, dict) else {}
        workflow = metadata.get("workflow")
        remaining_metadata = {
            key: value for key, value in metadata.items() if key != "workflow"
        }
        envelope = {
            "schema_version": 1,
            "captured_at": captured_at,
            "media": {
                "s3_uri": media_s3_uri,
                "filename": media_filename,
                "batch_index": batch_index,
                "batch_count": batch_count,
            },
            "comfyui": {
                "prompt": prompt,
                "workflow": workflow,
                "extra_pnginfo": remaining_metadata,
            },
        }

        file_descriptor, temporary_path = tempfile.mkstemp(
            suffix=".workflow.json"
        )
        os.close(file_descriptor)
        temporary_paths.append(temporary_path)
        with open(temporary_path, "w", encoding="utf-8", newline="\n") as output:
            json.dump(envelope, output, ensure_ascii=False, indent=2)
            output.write("\n")
        return temporary_path

    @staticmethod
    def _normalize_s3_path(s3_path: str) -> str:
        """Normalize the destination folder and fall back to ``media``."""
        raw_path = (s3_path or "").strip().strip("/")

        if not raw_path:
            return "media"

        parts = [
            part
            for part in PurePosixPath(raw_path).parts
            if part not in ("", ".", "..", "/")
        ]
        return "/".join(parts) or "media"

    @staticmethod
    def _build_destination_filename(
        source_path: str,
        requested_name: str,
        timestamp: str,
        forced_suffix=None,
        batch_index=None,
    ) -> str:
        """Build ``name-timestamp.ext`` or ``timestamp.ext``."""
        source_suffix = forced_suffix or Path(source_path).suffix.lower() or ".mp4"
        requested_name = (requested_name or "").strip()

        safe_stem = ""
        suffix = source_suffix
        if requested_name:
            requested_filename = Path(requested_name).name
            requested_path = Path(requested_filename)
            requested_suffix = requested_path.suffix.lower()
            if forced_suffix is None:
                suffix = requested_suffix or source_suffix
            stem = requested_path.stem if requested_suffix else requested_filename
            safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("._-")

        base_name = f"{safe_stem}-{timestamp}" if safe_stem else timestamp
        if batch_index is not None:
            base_name = f"{base_name}-{batch_index + 1:04d}"
        return f"{base_name}{suffix}"

    @staticmethod
    def _build_s3_client(endpoint_url, region: str, access_key: str, secret_key: str):
        """Instantiate and return a boto3 S3 client.

        Credentials are read from environment variables by the caller and
        forwarded here so that S3-compatible endpoints (RunPod, MinIO, …)
        that do not support IAM role chains work correctly.
        """
        client_kwargs: dict = {
            "region_name": region,
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
        }
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
        return boto3.client("s3", **client_kwargs)

    @staticmethod
    def _upload(
        s3_client,
        local_path: str,
        bucket: str,
        s3_key: str,
    ) -> None:
        """Upload *local_path* to *bucket*/*s3_key* using the provided S3 client.

        Catches both ``ClientError`` (low-level API errors such as AccessDenied /
        NoSuchBucket) and ``S3UploadFailedError`` (raised by boto3's managed
        transfer when the underlying request fails mid-transfer) so that every
        S3 failure surfaces as a ``RuntimeError`` with bucket/key context.
        """
        try:
            s3_client.upload_file(local_path, bucket, s3_key)
        except (ClientError, S3UploadFailedError) as exc:
            raise RuntimeError(
                f"DX2UploadMediaToS3: upload failed for "
                f"s3://{bucket}/{s3_key}: {exc}"
            ) from exc


# ------------------------------------------------------------------
# ComfyUI node registry
# ------------------------------------------------------------------

NODE_CLASS_MAPPINGS = {
    "DX2UploadMediaToS3": DX2UploadMediaToS3,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DX2UploadMediaToS3": "DX2 Upload Media to S3",
}
