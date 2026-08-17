"""
DX2 ComfyUI S3 Tools – nodes.py

Provides:
  DX2UploadVideoToS3 – uploads a generated video to an S3-compatible bucket.

The node accepts ComfyUI's native VIDEO type, a plain STRING file path, or the
VHS_FILENAMES output of ComfyUI-VideoHelperSuite's VideoCombine node.

Required environment variables:
  S3_BUCKET              – destination bucket name
  AWS_ACCESS_KEY_ID      – access key (or compatible credential)
  AWS_SECRET_ACCESS_KEY  – secret key

Optional environment variables:
  S3_ENDPOINT_URL  – custom endpoint for S3-compatible stores (RunPod, MinIO, …)
  S3_REGION        – region name (default: us-east-1)
"""

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


class DX2UploadVideoToS3:
    """ComfyUI node: upload a generated video to an S3-compatible bucket.

    Wiring options
    --------------
    - Connect the VIDEO output of ComfyUI's CreateVideo to *video*.
    - Connect a plain file-path string to *local_path*.
    - Connect the VHS_FILENAMES output of VHS_VideoCombine to *vhs_filenames*
      (the last file in the list is used).
    - All source inputs are optional individually; at least one must be provided.
      Priority is *video*, then *local_path*, then *vhs_filenames*.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {},
            "optional": {
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
                    {"default": "videos", "multiline": False},
                ),
                "file_name": (
                    "STRING",
                    {"default": "", "multiline": False},
                ),
                "enabled": ("BOOLEAN", {"default": True}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("upload_info",)
    FUNCTION = "upload_video"
    CATEGORY = "DX2/IO"
    OUTPUT_NODE = True

    # ------------------------------------------------------------------
    # Main execution
    # ------------------------------------------------------------------

    def upload_video(
        self,
        local_path: str = "",
        vhs_filenames=None,
        s3_path: str = "videos",
        file_name: str = "",
        enabled: bool = True,
        video=None,
    ):
        """Upload a video to S3 and return the destination URI.

        Parameters
        ----------
        local_path:
            Explicit filesystem path to the video file (STRING input).
        vhs_filenames:
            VHS_FILENAMES payload from VHS_VideoCombine:
            ``(save_output: bool, filepaths: list[str])``.
            The last filepath in the list is used.
        s3_path:
            Destination folder within the bucket (default: ``videos``).
        file_name:
            Optional user-facing filename. A UTC timestamp is appended to keep
            uploads collision-safe. The source extension is used when omitted.
        enabled:
            Set to *False* to skip the upload and return ``"upload_skipped"``.
        video:
            Native ComfyUI VIDEO object. It is serialized to a temporary MP4,
            which is removed after the upload attempt.
        """
        if not enabled:
            logger.info("DX2UploadVideoToS3: upload disabled – skipping.")
            return ("upload_skipped",)

        temporary_path = None
        try:
            # --------------------------------------------------------------
            # 1. Resolve or materialize the local file path
            # --------------------------------------------------------------
            if video is not None:
                file_descriptor, temporary_path = tempfile.mkstemp(suffix=".mp4")
                os.close(file_descriptor)
                video.save_to(temporary_path)
                resolved_path = temporary_path
            else:
                resolved_path = self._resolve_path(local_path, vhs_filenames)

            logger.info("DX2UploadVideoToS3: resolved local path → %s", resolved_path)

            if not os.path.isfile(resolved_path):
                raise FileNotFoundError(
                    f"DX2UploadVideoToS3: file not found: {resolved_path}"
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
                    "DX2UploadVideoToS3: S3_BUCKET environment variable is not set."
                )
            if not access_key or not secret_key:
                raise EnvironmentError(
                    "DX2UploadVideoToS3: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY "
                    "must both be set."
                )

            # --------------------------------------------------------------
            # 3. Build the S3 key independently from the local source name.
            #    Native VIDEO sources use a randomly named temporary file, but
            #    that implementation detail must not leak into the S3 key.
            # --------------------------------------------------------------
            timestamp = datetime.now(timezone.utc).strftime(
                "%Y%m%dT%H%M%S_%fZ"
            )
            normalized_s3_path = self._normalize_s3_path(s3_path)
            filename = self._build_destination_filename(
                source_path=resolved_path,
                requested_name=file_name,
                timestamp=timestamp,
            )
            s3_key = f"{normalized_s3_path}/{filename}"

            logger.info(
                "DX2UploadVideoToS3: uploading to s3://%s/%s (endpoint: %s)",
                bucket,
                s3_key,
                endpoint_url or "default AWS endpoint",
            )

            # --------------------------------------------------------------
            # 4. Upload
            # --------------------------------------------------------------
            s3_client = self._build_s3_client(
                endpoint_url, region, access_key, secret_key
            )
            self._upload(s3_client, resolved_path, bucket, s3_key)

            upload_info = f"s3://{bucket}/{s3_key}"
            logger.info("DX2UploadVideoToS3: upload succeeded → %s", upload_info)
            return (upload_info,)
        finally:
            if temporary_path is not None:
                try:
                    os.remove(temporary_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.warning(
                        "DX2UploadVideoToS3: failed to remove temporary video %s",
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
                    "DX2UploadVideoToS3: vhs_filenames is not a valid VHS_FILENAMES "
                    f"payload (expected a 2-tuple): {exc}"
                ) from exc

            if filepaths:
                return filepaths[-1]

        raise ValueError(
            "DX2UploadVideoToS3: no file path provided. "
            "Connect video (VIDEO), local_path (STRING), or "
            "vhs_filenames (VHS_FILENAMES)."
        )

    @staticmethod
    def _normalize_s3_path(s3_path: str) -> str:
        """Normalize the destination folder and fall back to ``videos``."""
        raw_path = (s3_path or "").strip().strip("/")

        if not raw_path:
            return "videos"

        parts = [
            part
            for part in PurePosixPath(raw_path).parts
            if part not in ("", ".", "..", "/")
        ]
        return "/".join(parts) or "videos"

    @staticmethod
    def _build_destination_filename(
        source_path: str,
        requested_name: str,
        timestamp: str,
    ) -> str:
        """Build ``name-timestamp.ext`` or ``timestamp.ext``."""
        source_suffix = Path(source_path).suffix.lower() or ".mp4"
        requested_name = (requested_name or "").strip()

        if not requested_name:
            return f"{timestamp}{source_suffix}"

        requested_filename = Path(requested_name).name
        requested_path = Path(requested_filename)
        requested_suffix = requested_path.suffix.lower()
        suffix = requested_suffix or source_suffix
        stem = requested_path.stem if requested_suffix else requested_filename
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("._-")

        if not safe_stem:
            return f"{timestamp}{suffix}"

        return f"{safe_stem}-{timestamp}{suffix}"

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
                f"DX2UploadVideoToS3: upload failed for "
                f"s3://{bucket}/{s3_key}: {exc}"
            ) from exc


# ------------------------------------------------------------------
# ComfyUI node registry
# ------------------------------------------------------------------

NODE_CLASS_MAPPINGS = {
    "DX2UploadVideoToS3": DX2UploadVideoToS3,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DX2UploadVideoToS3": "DX2 Upload Video to S3",
}
