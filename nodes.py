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
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

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
                "job_id": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                    },
                ),
                "s3_key_prefix": (
                    "STRING",
                    {
                        "default": "videos",
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
        job_id: str = "",
        s3_key_prefix: str = "videos",
        enabled: bool = True,
    ):
        """
        Upload a video to S3 and return upload metadata as JSON.

        The generated S3 key is collision-safe.

        With a job ID:
            {prefix}/{YYYY}/{MM}/{DD}/{job_id}/{uuid}/{filename}

        Without a job ID:
            {prefix}/{YYYY}/{MM}/{DD}/{uuid}/{filename}
        """
        if not enabled:
            upload_info = {
                "uploaded": False,
                "status": "upload_skipped",
            }

            logger.info(
                "DX2UploadVideoToS3: upload disabled, skipping."
            )

            return (json.dumps(upload_info),)

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

        filename = Path(resolved_path).name
        date_path = datetime.now(
            timezone.utc
        ).strftime("%Y/%m/%d")
        unique_id = str(uuid4())

        parts = [
            s3_key_prefix.strip("/"),
            date_path,
        ]

        normalized_job_id = job_id.strip().strip("/")

        if normalized_job_id:
            parts.append(normalized_job_id)

        parts.extend(
            [
                unique_id,
                filename,
            ]
        )

        s3_key = "/".join(
            part
            for part in parts
            if part
        )

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
