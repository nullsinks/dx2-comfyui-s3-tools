"""Unit tests for DX2UploadVideoToS3 node (nodes.py)."""

import os
import unittest
from unittest.mock import MagicMock, patch

import boto3
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import ClientError

from nodes import DX2UploadVideoToS3


class TestResolvePath(unittest.TestCase):
    """Tests for DX2UploadVideoToS3._resolve_path."""

    def test_uses_explicit_local_path(self):
        result = DX2UploadVideoToS3._resolve_path("/tmp/video.mp4", None)
        self.assertEqual(result, "/tmp/video.mp4")

    def test_strips_whitespace_from_local_path(self):
        result = DX2UploadVideoToS3._resolve_path("  /tmp/video.mp4  ", None)
        self.assertEqual(result, "/tmp/video.mp4")

    def test_uses_vhs_filenames_when_no_local_path(self):
        vhs = (True, ["/tmp/a.mp4", "/tmp/b.mp4"])
        result = DX2UploadVideoToS3._resolve_path("", vhs)
        self.assertEqual(result, "/tmp/b.mp4")  # last item

    def test_local_path_takes_priority_over_vhs_filenames(self):
        vhs = (True, ["/tmp/vhs.mp4"])
        result = DX2UploadVideoToS3._resolve_path("/tmp/explicit.mp4", vhs)
        self.assertEqual(result, "/tmp/explicit.mp4")

    def test_raises_when_neither_provided(self):
        with self.assertRaises(ValueError):
            DX2UploadVideoToS3._resolve_path("", None)

    def test_raises_when_vhs_filenames_empty_list(self):
        with self.assertRaises(ValueError):
            DX2UploadVideoToS3._resolve_path("", (False, []))

    def test_raises_when_vhs_filenames_malformed(self):
        with self.assertRaises(ValueError):
            DX2UploadVideoToS3._resolve_path("", "not-a-tuple")


class TestUploadVideo(unittest.TestCase):
    """Integration-style tests for DX2UploadVideoToS3.upload_video."""

    BASE_ENV = {
        "S3_BUCKET": "test-bucket",
        "AWS_ACCESS_KEY_ID": "test-key-id",
        "AWS_SECRET_ACCESS_KEY": "test-secret",
        "S3_ENDPOINT_URL": "https://s3.example.com",
    }

    def _node(self):
        return DX2UploadVideoToS3()

    # ------------------------------------------------------------------
    # disabled
    # ------------------------------------------------------------------

    def test_disabled_returns_skipped(self):
        result = self._node().upload_video(enabled=False)
        self.assertEqual(result, ("upload_skipped",))

    # ------------------------------------------------------------------
    # missing path
    # ------------------------------------------------------------------

    def test_raises_when_no_path(self):
        with patch.dict(os.environ, self.BASE_ENV):
            with self.assertRaises(ValueError):
                self._node().upload_video()

    # ------------------------------------------------------------------
    # missing credentials / bucket
    # ------------------------------------------------------------------

    def test_raises_when_bucket_missing(self):
        env = {**self.BASE_ENV, "S3_BUCKET": ""}
        with patch.dict(os.environ, env, clear=True):
            with patch("os.path.isfile", return_value=True):
                with self.assertRaises(EnvironmentError):
                    self._node().upload_video(local_path="/tmp/video.mp4")

    def test_raises_when_credentials_missing(self):
        env = {**self.BASE_ENV, "AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": ""}
        with patch.dict(os.environ, env):
            with patch("os.path.isfile", return_value=True):
                with self.assertRaises(EnvironmentError):
                    self._node().upload_video(local_path="/tmp/video.mp4")

    # ------------------------------------------------------------------
    # file not found
    # ------------------------------------------------------------------

    def test_raises_when_file_not_found(self):
        with patch.dict(os.environ, self.BASE_ENV):
            with self.assertRaises(FileNotFoundError):
                self._node().upload_video(local_path="/nonexistent/video.mp4")

    # ------------------------------------------------------------------
    # successful upload
    # ------------------------------------------------------------------

    def _run_successful_upload(self, **kwargs):
        with patch.dict(os.environ, self.BASE_ENV):
            with patch("os.path.isfile", return_value=True):
                with patch("boto3.client") as mock_boto:
                    mock_s3 = MagicMock()
                    mock_boto.return_value = mock_s3
                    result = self._node().upload_video(**kwargs)
        return result, mock_s3

    def test_successful_upload_returns_s3_uri(self):
        result, _ = self._run_successful_upload(local_path="/tmp/video.mp4")
        self.assertEqual(result, ("s3://test-bucket/videos/video.mp4",))

    def test_s3_key_includes_job_id(self):
        result, _ = self._run_successful_upload(
            local_path="/tmp/video.mp4", job_id="job-123"
        )
        self.assertEqual(result, ("s3://test-bucket/videos/job-123/video.mp4",))

    def test_custom_key_prefix(self):
        result, _ = self._run_successful_upload(
            local_path="/tmp/video.mp4", s3_key_prefix="outputs/videos"
        )
        self.assertEqual(result, ("s3://test-bucket/outputs/videos/video.mp4",))

    def test_upload_file_called_with_correct_args(self):
        _, mock_s3 = self._run_successful_upload(
            local_path="/tmp/my.mp4", job_id="run-1"
        )
        mock_s3.upload_file.assert_called_once_with(
            "/tmp/my.mp4", "test-bucket", "videos/run-1/my.mp4"
        )

    # ------------------------------------------------------------------
    # VHS_FILENAMES input
    # ------------------------------------------------------------------

    def test_accepts_vhs_filenames(self):
        vhs = (True, ["/tmp/out_00001.mp4"])
        result, _ = self._run_successful_upload(vhs_filenames=vhs)
        self.assertEqual(result, ("s3://test-bucket/videos/out_00001.mp4",))

    def test_vhs_filenames_uses_last_file(self):
        vhs = (True, ["/tmp/first.mp4", "/tmp/last.mp4"])
        result, _ = self._run_successful_upload(vhs_filenames=vhs)
        self.assertEqual(result, ("s3://test-bucket/videos/last.mp4",))

    def test_local_path_overrides_vhs_filenames(self):
        vhs = (True, ["/tmp/vhs.mp4"])
        result, _ = self._run_successful_upload(
            local_path="/tmp/explicit.mp4", vhs_filenames=vhs
        )
        self.assertEqual(result, ("s3://test-bucket/videos/explicit.mp4",))

    # ------------------------------------------------------------------
    # upload failures — P2: both ClientError and S3UploadFailedError
    # ------------------------------------------------------------------

    def _run_upload_with_side_effect(self, side_effect, **kwargs):
        with patch.dict(os.environ, self.BASE_ENV):
            with patch("os.path.isfile", return_value=True):
                with patch("boto3.client") as mock_boto:
                    mock_s3 = MagicMock()
                    mock_s3.upload_file.side_effect = side_effect
                    mock_boto.return_value = mock_s3
                    self._node().upload_video(**kwargs)

    def test_client_error_raises_runtime_error(self):
        local_path = "/tmp/video.mp4"
        expected_key = f"videos/{os.path.basename(local_path)}"
        error = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            "UploadFile",
        )
        with self.assertRaises(RuntimeError) as ctx:
            self._run_upload_with_side_effect(error, local_path=local_path)
        self.assertIn("test-bucket", str(ctx.exception))
        self.assertIn(expected_key, str(ctx.exception))

    def test_s3_upload_failed_error_raises_runtime_error(self):
        error = S3UploadFailedError(
            "Failed to upload videos/video.mp4: Connection reset"
        )
        with self.assertRaises(RuntimeError) as ctx:
            self._run_upload_with_side_effect(error, local_path="/tmp/video.mp4")
        self.assertIn("test-bucket", str(ctx.exception))

    def test_runtime_error_wraps_original_exception(self):
        error = ClientError(
            {"Error": {"Code": "NoSuchBucket", "Message": "No such bucket"}},
            "UploadFile",
        )
        with self.assertRaises(RuntimeError) as ctx:
            self._run_upload_with_side_effect(error, local_path="/tmp/video.mp4")
        self.assertIsInstance(ctx.exception.__cause__, ClientError)


if __name__ == "__main__":
    unittest.main()
