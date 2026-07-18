"""
DX2 ComfyUI S3 Tools - nodes.py

Provides:
    DX2UploadVideoToS3 - uploads a generated video file to an
    S3-compatible bucket.

The node accepts a file path either as a plain STRING or directly from the
VHS_FILENAMES output of ComfyUI-VideoHelperSuite's VHS_VideoCombine node.

Required environment variables:
    S3_BUCKET
    AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY

Optional environment variables:
    S3_ENDPOINT_URL
    S3_REGION
"""

import json
import logging
import mimetypes
import os
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import boto3
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class DX2UploadVideoToS3:
    """Upload a generated video file to an S3-compatible bucket."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {},
            "optional": {
                "local_path": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                    },
                ),
                "vhs_filenames": ("VHS_FILENAMES",),
                "s3_path": (
                    "STRING",
                    {
                        "default": "videos",
                        "multiline": False,
                    },
                ),
                "file_name": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                    },
                ),
                "enabled": (
                    "BOOLEAN",
                    {
                        "default": True,
                    },
                ),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("upload_info",)
    FUNCTION = "upload_video"
    CATEGORY = "DX2/IO"
    OUTPUT_NODE = True

    def upload_video(
        self,
        local_path: str = "",
        vhs_filenames=None,
        s3_path: str = "videos",
        file_name: str = "",
        enabled: bool = True,
    ):
        """
        Upload a video to S3 and return upload metadata as JSON.

        Destination format with a supplied filename:

            {s3_path}/{file_name}-{timestamp}.{extension}

        Destination format without a supplied filename:

            {s3_path}/{timestamp}.{extension}

        An empty s3_path falls back to:

            videos
        """
        if not enabled:
            upload_info = {
                "uploaded": False,
                "status": "upload_skipped",
            }

            logger.info(
                "DX2UploadVideoToS3: upload disabled, skipping."
            )

            return (
                json.dumps(
                    upload_info,
                    separators=(",", ":"),
                ),
            )

        resolved_path = self._resolve_path(
            local_path=local_path,
            vhs_filenames=vhs_filenames,
        )

        logger.info(
            "DX2UploadVideoToS3: resolved local path: %s",
            resolved_path,
        )

        if not os.path.isfile(resolved_path):
            raise FileNotFoundError(
                "DX2UploadVideoToS3: file not found: "
                f"{resolved_path}"
            )

        bucket = os.environ.get(
            "S3_BUCKET",
            "",
        ).strip()

        endpoint_url = os.environ.get(
            "S3_ENDPOINT_URL",
            "",
        ).strip() or None

        region = os.environ.get(
            "S3_REGION",
            "us-east-1",
        ).strip()

        access_key = os.environ.get(
            "AWS_ACCESS_KEY_ID",
            "",
        ).strip()

        secret_key = os.environ.get(
            "AWS_SECRET_ACCESS_KEY",
            "",
        ).strip()

        if not bucket:
            raise EnvironmentError(
                "DX2UploadVideoToS3: S3_BUCKET environment variable "
                "is not set."
            )

        if not access_key or not secret_key:
            raise EnvironmentError(
                "DX2UploadVideoToS3: AWS_ACCESS_KEY_ID and "
                "AWS_SECRET_ACCESS_KEY must both be set."
            )

        # ------------------------------------------------------------------
        # Build a readable, collision-safe S3 key
        # ------------------------------------------------------------------
        timestamp = datetime.now(
            timezone.utc
        ).strftime("%Y%m%dT%H%M%S_%fZ")

        normalized_s3_path = self._normalize_s3_path(
            s3_path
        )

        filename = self._build_destination_filename(
            source_path=resolved_path,
            requested_name=file_name,
            timestamp=timestamp,
        )

        s3_key = f"{normalized_s3_path}/{filename}"

        logger.info(
            "DX2UploadVideoToS3: uploading to "
            "s3://%s/%s using endpoint %s",
            bucket,
            s3_key,
            endpoint_url or "default AWS endpoint",
        )

        s3_client = self._build_s3_client(
            endpoint_url=endpoint_url,
            region=region,
            access_key=access_key,
            secret_key=secret_key,
        )

        self._upload(
            s3_client=s3_client,
            local_path=resolved_path,
            bucket=bucket,
            s3_key=s3_key,
        )

        upload_info = {
            "uploaded": True,
            "bucket": bucket,
            "key": s3_key,
            "uri": f"s3://{bucket}/{s3_key}",
            "filename": filename,
            "size_bytes": os.path.getsize(resolved_path),
            "s3_path": normalized_s3_path,
            "timestamp": timestamp,
        }

        logger.info(
            "DX2UploadVideoToS3: upload succeeded: %s",
            upload_info["uri"],
        )

        return (
            json.dumps(
                upload_info,
                separators=(",", ":"),
            ),
        )

    @staticmethod
    def _resolve_path(
        local_path: str,
        vhs_filenames,
    ) -> str:
        """
        Resolve the video file path.

        Priority:
            1. Explicit local_path
            2. Last path from VHS_FILENAMES
        """
        if local_path and local_path.strip():
            return local_path.strip()

        if vhs_filenames is not None:
            try:
                _, filepaths = vhs_filenames
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "DX2UploadVideoToS3: invalid VHS_FILENAMES payload. "
                    "Expected a two-item tuple containing "
                    "(save_output, filepaths)."
                ) from exc

            if filepaths:
                return str(filepaths[-1])

        raise ValueError(
            "DX2UploadVideoToS3: no file path provided. "
            "Connect local_path or vhs_filenames."
        )

    @staticmethod
    def _normalize_s3_path(
        s3_path: str,
    ) -> str:
        """Normalize the destination folder and fall back to videos."""
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
        """Build name-timestamp.ext or timestamp.ext."""
        source_suffix = Path(source_path).suffix.lower() or ".mp4"
        requested_name = (requested_name or "").strip()

        if not requested_name:
            return f"{timestamp}{source_suffix}"

        requested_filename = Path(requested_name).name
        requested_path = Path(requested_filename)

        requested_suffix = requested_path.suffix.lower()
        suffix = requested_suffix or source_suffix

        stem = (
            requested_path.stem
            if requested_suffix
            else requested_filename
        )

        safe_stem = re.sub(
            r"[^A-Za-z0-9._-]+",
            "-",
            stem,
        ).strip("._-")

        if not safe_stem:
            return f"{timestamp}{suffix}"

        return f"{safe_stem}-{timestamp}{suffix}"

    @staticmethod
    def _build_s3_client(
        endpoint_url,
        region: str,
        access_key: str,
        secret_key: str,
    ):
        """Create and return a boto3 S3 client."""
        client_kwargs = {
            "region_name": region,
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
        }

        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url

        return boto3.client(
            "s3",
            **client_kwargs,
        )

    @staticmethod
    def _upload(
        s3_client,
        local_path: str,
        bucket: str,
        s3_key: str,
    ) -> None:
        """Upload the video file and preserve its MIME type."""
        content_type, _ = mimetypes.guess_type(
            local_path
        )

        try:
            s3_client.upload_file(
                local_path,
                bucket,
                s3_key,
                ExtraArgs={
                    "ContentType": (
                        content_type
                        or "application/octet-stream"
                    ),
                    "ContentDisposition": "inline",
                },
            )
        except (
            ClientError,
            S3UploadFailedError,
        ) as exc:
            raise RuntimeError(
                "DX2UploadVideoToS3: upload failed for "
                f"s3://{bucket}/{s3_key}: {exc}"
            ) from exc


NODE_CLASS_MAPPINGS = {
    "DX2UploadVideoToS3": DX2UploadVideoToS3,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DX2UploadVideoToS3": "DX2 Upload Video to S3",
}
