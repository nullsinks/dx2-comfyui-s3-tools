"""Unit tests for DX2UploadMediaToS3 node (nodes.py)."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import boto3
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import ClientError

from nodes import (
    DX2UploadMediaToS3,
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
)


class TestResolvePath(unittest.TestCase):
    """Tests for DX2UploadMediaToS3._resolve_path."""

    def test_uses_explicit_local_path(self):
        result = DX2UploadMediaToS3._resolve_path("/tmp/video.mp4", None)
        self.assertEqual(result, "/tmp/video.mp4")

    def test_strips_whitespace_from_local_path(self):
        result = DX2UploadMediaToS3._resolve_path("  /tmp/video.mp4  ", None)
        self.assertEqual(result, "/tmp/video.mp4")

    def test_uses_vhs_filenames_when_no_local_path(self):
        vhs = (True, ["/tmp/a.mp4", "/tmp/b.mp4"])
        result = DX2UploadMediaToS3._resolve_path("", vhs)
        self.assertEqual(result, "/tmp/b.mp4")  # last item

    def test_local_path_takes_priority_over_vhs_filenames(self):
        vhs = (True, ["/tmp/vhs.mp4"])
        result = DX2UploadMediaToS3._resolve_path("/tmp/explicit.mp4", vhs)
        self.assertEqual(result, "/tmp/explicit.mp4")

    def test_raises_when_neither_provided(self):
        with self.assertRaises(ValueError):
            DX2UploadMediaToS3._resolve_path("", None)

    def test_raises_when_vhs_filenames_empty_list(self):
        with self.assertRaises(ValueError):
            DX2UploadMediaToS3._resolve_path("", (False, []))

    def test_raises_when_vhs_filenames_malformed(self):
        with self.assertRaises(ValueError):
            DX2UploadMediaToS3._resolve_path("", "not-a-tuple")


class TestS3Naming(unittest.TestCase):
    """Tests for destination path and filename construction."""

    TIMESTAMP = "20260816T193012_123456Z"

    def test_normalizes_s3_path(self):
        result = DX2UploadMediaToS3._normalize_s3_path(
            " /videos//minimax-h3/ "
        )
        self.assertEqual(result, "videos/minimax-h3")

    def test_empty_s3_path_defaults_to_media(self):
        self.assertEqual(DX2UploadMediaToS3._normalize_s3_path(""), "media")

    def test_builds_user_provided_mp4_filename(self):
        result = DX2UploadMediaToS3._build_destination_filename(
            source_path="/tmp/random-native-name.mp4",
            requested_name="test",
            timestamp=self.TIMESTAMP,
        )
        self.assertEqual(result, f"test-{self.TIMESTAMP}.mp4")

    def test_requested_extension_overrides_source_extension(self):
        result = DX2UploadMediaToS3._build_destination_filename(
            source_path="/tmp/video.mp4",
            requested_name="final.WEBM",
            timestamp=self.TIMESTAMP,
        )
        self.assertEqual(result, f"final-{self.TIMESTAMP}.webm")

    def test_missing_file_name_uses_timestamp_not_source_basename(self):
        result = DX2UploadMediaToS3._build_destination_filename(
            source_path="/tmp/random-native-name.mp4",
            requested_name="",
            timestamp=self.TIMESTAMP,
        )
        self.assertEqual(result, f"{self.TIMESTAMP}.mp4")

    def test_forced_suffix_and_batch_index_for_image(self):
        result = DX2UploadMediaToS3._build_destination_filename(
            source_path="/tmp/random.png",
            requested_name="photo.jpg",
            timestamp=self.TIMESTAMP,
            forced_suffix=".png",
            batch_index=1,
        )
        self.assertEqual(result, f"photo-{self.TIMESTAMP}-0002.png")


class TestNodeRegistration(unittest.TestCase):
    def test_registers_only_generalized_media_node(self):
        self.assertEqual(
            NODE_CLASS_MAPPINGS,
            {"DX2UploadMediaToS3": DX2UploadMediaToS3},
        )
        self.assertEqual(
            NODE_DISPLAY_NAME_MAPPINGS,
            {"DX2UploadMediaToS3": "DX2 Upload Media to S3"},
        )

    def test_function_and_output_node_contract(self):
        self.assertEqual(DX2UploadMediaToS3.FUNCTION, "upload_media")
        self.assertTrue(DX2UploadMediaToS3.OUTPUT_NODE)


class TestUploadMedia(unittest.TestCase):
    """Integration-style tests for DX2UploadMediaToS3.upload_media."""

    BASE_ENV = {
        "S3_BUCKET": "test-bucket",
        "AWS_ACCESS_KEY_ID": "test-key-id",
        "AWS_SECRET_ACCESS_KEY": "test-secret",
        "S3_ENDPOINT_URL": "https://s3.example.com",
    }
    FIXED_TIMESTAMP = "20260816T193012_123456Z"

    def _node(self):
        return DX2UploadMediaToS3()

    # ------------------------------------------------------------------
    # disabled
    # ------------------------------------------------------------------

    def test_disabled_returns_skipped(self):
        result = self._node().upload_media(enabled=False)
        self.assertEqual(result, ("upload_skipped",))

    def test_disabled_does_not_serialize_native_video(self):
        video = MagicMock()
        result = self._node().upload_media(video=video, enabled=False)
        self.assertEqual(result, ("upload_skipped",))
        video.save_to.assert_not_called()

    # ------------------------------------------------------------------
    # missing path
    # ------------------------------------------------------------------

    def test_raises_when_no_path(self):
        with patch.dict(os.environ, self.BASE_ENV):
            with self.assertRaises(ValueError):
                self._node().upload_media()

    # ------------------------------------------------------------------
    # missing credentials / bucket
    # ------------------------------------------------------------------

    def test_raises_when_bucket_missing(self):
        env = {**self.BASE_ENV, "S3_BUCKET": ""}
        with patch.dict(os.environ, env, clear=True):
            with patch("os.path.isfile", return_value=True):
                with self.assertRaises(EnvironmentError):
                    self._node().upload_media(local_path="/tmp/video.mp4")

    def test_raises_when_credentials_missing(self):
        env = {**self.BASE_ENV, "AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": ""}
        with patch.dict(os.environ, env):
            with patch("os.path.isfile", return_value=True):
                with self.assertRaises(EnvironmentError):
                    self._node().upload_media(local_path="/tmp/video.mp4")

    # ------------------------------------------------------------------
    # file not found
    # ------------------------------------------------------------------

    def test_raises_when_file_not_found(self):
        with patch.dict(os.environ, self.BASE_ENV):
            with self.assertRaises(FileNotFoundError):
                self._node().upload_media(local_path="/nonexistent/video.mp4")

    # ------------------------------------------------------------------
    # successful upload
    # ------------------------------------------------------------------

    def _run_successful_upload(self, **kwargs):
        with patch.dict(os.environ, self.BASE_ENV):
            with patch("os.path.isfile", return_value=True):
                with patch("nodes.datetime") as mock_datetime, patch(
                    "boto3.client"
                ) as mock_boto:
                    mock_datetime.now.return_value.strftime.return_value = (
                        self.FIXED_TIMESTAMP
                    )
                    mock_s3 = MagicMock()
                    mock_boto.return_value = mock_s3
                    result = self._node().upload_media(**kwargs)
        return result, mock_s3

    def test_successful_upload_returns_s3_uri(self):
        result, _ = self._run_successful_upload(local_path="/tmp/video.mp4")
        self.assertEqual(
            result,
            (f"s3://test-bucket/media/{self.FIXED_TIMESTAMP}.mp4",),
        )

    def test_s3_key_uses_user_path_and_file_name(self):
        result, _ = self._run_successful_upload(
            local_path="/tmp/video.mp4",
            s3_path="videos/minimax-h3",
            file_name="test",
        )
        self.assertEqual(
            result,
            (
                "s3://test-bucket/videos/minimax-h3/"
                f"test-{self.FIXED_TIMESTAMP}.mp4"
            ),
        )

    def test_custom_s3_path_is_normalized(self):
        result, _ = self._run_successful_upload(
            local_path="/tmp/video.mp4", s3_path="/outputs/videos/"
        )
        self.assertEqual(
            result,
            (f"s3://test-bucket/outputs/videos/{self.FIXED_TIMESTAMP}.mp4",),
        )

    def test_upload_file_called_with_correct_args(self):
        _, mock_s3 = self._run_successful_upload(
            local_path="/tmp/my.mp4",
            s3_path="videos/wan2.2",
            file_name="test",
        )
        mock_s3.upload_file.assert_called_once_with(
            "/tmp/my.mp4",
            "test-bucket",
            f"videos/wan2.2/test-{self.FIXED_TIMESTAMP}.mp4",
        )

    # ------------------------------------------------------------------
    # VHS_FILENAMES input
    # ------------------------------------------------------------------

    def test_accepts_vhs_filenames(self):
        vhs = (True, ["/tmp/out_00001.mp4"])
        result, _ = self._run_successful_upload(
            vhs_filenames=vhs,
            s3_path="videos/wan2.2",
            file_name="test",
        )
        self.assertEqual(
            result,
            (
                "s3://test-bucket/videos/wan2.2/"
                f"test-{self.FIXED_TIMESTAMP}.mp4"
            ),
        )

    def test_vhs_filenames_uses_last_file(self):
        vhs = (True, ["/tmp/first.mp4", "/tmp/last.mp4"])
        _, mock_s3 = self._run_successful_upload(vhs_filenames=vhs)
        self.assertEqual(mock_s3.upload_file.call_args.args[0], "/tmp/last.mp4")

    def test_local_path_overrides_vhs_filenames(self):
        vhs = (True, ["/tmp/vhs.mp4"])
        _, mock_s3 = self._run_successful_upload(
            local_path="/tmp/explicit.mp4", vhs_filenames=vhs
        )
        self.assertEqual(
            mock_s3.upload_file.call_args.args[0], "/tmp/explicit.mp4"
        )

    # ------------------------------------------------------------------
    # Native VIDEO input
    # ------------------------------------------------------------------

    def test_input_types_exposes_native_media_and_shared_path(self):
        optional_inputs = DX2UploadMediaToS3.INPUT_TYPES()["optional"]
        self.assertEqual(optional_inputs["image"], ("IMAGE",))
        self.assertEqual(optional_inputs["video"], ("VIDEO",))
        self.assertEqual(
            optional_inputs["s3_path"],
            ("STRING", {"default": "media", "multiline": False}),
        )
        self.assertIn("file_name", optional_inputs)
        self.assertNotIn("image_s3_path", optional_inputs)
        self.assertNotIn("s3_key_prefix", optional_inputs)
        self.assertNotIn("job_id", optional_inputs)

    def test_native_video_is_serialized_uploaded_and_cleaned_up(self):
        video = MagicMock()

        def write_video(path):
            with open(path, "wb") as output:
                output.write(b"native-video")

        video.save_to.side_effect = write_video

        with patch.dict(os.environ, self.BASE_ENV):
            with patch("nodes.datetime") as mock_datetime, patch(
                "boto3.client"
            ) as mock_boto:
                mock_datetime.now.return_value.strftime.return_value = (
                    self.FIXED_TIMESTAMP
                )
                mock_s3 = MagicMock()
                mock_boto.return_value = mock_s3
                result = self._node().upload_media(
                    video=video,
                    s3_path="videos/minimax-h3",
                    file_name="test",
                )

        temporary_path = video.save_to.call_args.args[0]
        self.assertTrue(temporary_path.endswith(".mp4"))
        mock_s3.upload_file.assert_called_once_with(
            temporary_path,
            "test-bucket",
            f"videos/minimax-h3/test-{self.FIXED_TIMESTAMP}.mp4",
        )
        self.assertEqual(
            result,
            (
                "s3://test-bucket/videos/minimax-h3/"
                f"test-{self.FIXED_TIMESTAMP}.mp4"
            ),
        )
        self.assertFalse(os.path.exists(temporary_path))

    def test_native_video_takes_priority_over_other_sources(self):
        video = MagicMock()

        def write_video(path):
            with open(path, "wb") as output:
                output.write(b"native-video")

        video.save_to.side_effect = write_video
        vhs = (True, ["/tmp/vhs.mp4"])

        with patch.dict(os.environ, self.BASE_ENV):
            with patch("boto3.client") as mock_boto:
                mock_s3 = MagicMock()
                mock_boto.return_value = mock_s3
                self._node().upload_media(
                    video=video,
                    local_path="/tmp/explicit.mp4",
                    vhs_filenames=vhs,
                )

        uploaded_path = mock_s3.upload_file.call_args.args[0]
        self.assertEqual(uploaded_path, video.save_to.call_args.args[0])
        self.assertNotEqual(uploaded_path, "/tmp/explicit.mp4")

    def test_native_video_temp_file_is_cleaned_up_when_upload_fails(self):
        video = MagicMock()

        def write_video(path):
            with open(path, "wb") as output:
                output.write(b"native-video")

        video.save_to.side_effect = write_video
        error = S3UploadFailedError("Connection reset")

        with patch.dict(os.environ, self.BASE_ENV):
            with patch("boto3.client") as mock_boto:
                mock_s3 = MagicMock()
                mock_s3.upload_file.side_effect = error
                mock_boto.return_value = mock_s3
                with self.assertRaises(RuntimeError):
                    self._node().upload_media(video=video)

        temporary_path = video.save_to.call_args.args[0]
        self.assertFalse(os.path.exists(temporary_path))

    def test_native_video_temp_file_is_cleaned_up_when_serialization_fails(self):
        video = MagicMock()
        video.save_to.side_effect = RuntimeError("encode failed")

        with patch.dict(os.environ, self.BASE_ENV):
            with self.assertRaisesRegex(RuntimeError, "encode failed"):
                self._node().upload_media(video=video)

        temporary_path = video.save_to.call_args.args[0]
        self.assertFalse(os.path.exists(temporary_path))

    # ------------------------------------------------------------------
    # Native IMAGE input
    # ------------------------------------------------------------------

    @staticmethod
    def _image_batch(batch_size=1, channels=3):
        import numpy as np

        tensors = []
        for index in range(batch_size):
            array = np.full((2, 3, channels), index / max(batch_size, 1))
            tensor = MagicMock()
            tensor.detach.return_value = tensor
            tensor.cpu.return_value = tensor
            tensor.numpy.return_value = array
            tensors.append(tensor)

        image = MagicMock()
        image.shape = (batch_size, 2, 3, channels)
        image.__iter__.return_value = iter(tensors)
        return image

    def _run_image_upload(self, image, **kwargs):
        with patch.dict(os.environ, self.BASE_ENV):
            with patch("nodes.datetime") as mock_datetime, patch(
                "boto3.client"
            ) as mock_boto:
                mock_datetime.now.return_value.strftime.return_value = (
                    self.FIXED_TIMESTAMP
                )
                mock_s3 = MagicMock()
                mock_boto.return_value = mock_s3
                result = self._node().upload_media(image=image, **kwargs)
        return result, mock_s3

    def test_single_image_is_uploaded_as_png_and_returns_s3_uri(self):
        result, mock_s3 = self._run_image_upload(self._image_batch())

        uploaded_path, bucket, key = mock_s3.upload_file.call_args.args
        self.assertTrue(uploaded_path.endswith(".png"))
        self.assertEqual(bucket, "test-bucket")
        self.assertEqual(key, f"media/{self.FIXED_TIMESTAMP}.png")
        self.assertEqual(
            result,
            (f"s3://test-bucket/media/{self.FIXED_TIMESTAMP}.png",),
        )
        self.assertFalse(os.path.exists(uploaded_path))

    def test_image_uses_shared_custom_s3_path_and_forces_png_extension(self):
        result, mock_s3 = self._run_image_upload(
            self._image_batch(channels=4),
            s3_path="image",
            file_name="ComfyUI.jpg",
        )

        key = f"image/ComfyUI-{self.FIXED_TIMESTAMP}.png"
        self.assertEqual(mock_s3.upload_file.call_args.args[2], key)
        self.assertEqual(result, (f"s3://test-bucket/{key}",))

    def test_image_batch_uploads_every_item_with_ordered_indices(self):
        result, mock_s3 = self._run_image_upload(
            self._image_batch(batch_size=2), file_name="batch"
        )

        calls = mock_s3.upload_file.call_args_list
        self.assertEqual(len(calls), 2)
        expected_keys = [
            f"media/batch-{self.FIXED_TIMESTAMP}-0001.png",
            f"media/batch-{self.FIXED_TIMESTAMP}-0002.png",
        ]
        self.assertEqual([call.args[2] for call in calls], expected_keys)
        self.assertEqual(result, (f"s3://test-bucket/{expected_keys[-1]}",))
        self.assertTrue(all(not os.path.exists(call.args[0]) for call in calls))

    def test_native_image_takes_priority_over_video_and_paths(self):
        video = MagicMock()
        result, mock_s3 = self._run_image_upload(
            self._image_batch(),
            video=video,
            local_path="/tmp/explicit.mp4",
            vhs_filenames=(True, ["/tmp/vhs.mp4"]),
        )

        video.save_to.assert_not_called()
        self.assertTrue(mock_s3.upload_file.call_args.args[0].endswith(".png"))
        self.assertTrue(result[0].endswith(".png"))

    def test_rejects_empty_image_batch(self):
        image = MagicMock()
        image.shape = (0, 2, 3, 3)
        with self.assertRaisesRegex(ValueError, "batch is empty"):
            self._node().upload_media(image=image)

    def test_rejects_malformed_image_shape(self):
        image = MagicMock()
        image.shape = (2, 3, 3)
        with self.assertRaisesRegex(
            ValueError, r"\[batch, height, width, channels\]"
        ):
            self._node().upload_media(image=image)

    def test_rejects_unsupported_channel_count(self):
        image = MagicMock()
        image.shape = (1, 2, 3, 2)
        with self.assertRaisesRegex(ValueError, "1, 3, or 4 channels"):
            self._node().upload_media(image=image)

    def test_image_temp_file_is_cleaned_up_when_serialization_fails(self):
        image = MagicMock()
        image.shape = (1, 2, 3, 3)
        tensor = MagicMock()
        tensor.detach.return_value = tensor
        tensor.cpu.side_effect = RuntimeError("tensor transfer failed")
        image.__iter__.return_value = iter([tensor])

        created_paths = []
        real_mkstemp = tempfile.mkstemp

        def recording_mkstemp(*args, **kwargs):
            descriptor, path = real_mkstemp(*args, **kwargs)
            created_paths.append(path)
            return descriptor, path

        with patch("nodes.tempfile.mkstemp", side_effect=recording_mkstemp):
            with self.assertRaisesRegex(RuntimeError, "tensor transfer failed"):
                self._node().upload_media(image=image)

        self.assertEqual(len(created_paths), 1)
        self.assertFalse(os.path.exists(created_paths[0]))

    def test_all_image_temp_files_are_cleaned_up_on_partial_upload_failure(self):
        image = self._image_batch(batch_size=2)
        error = S3UploadFailedError("Connection reset")

        with patch.dict(os.environ, self.BASE_ENV):
            with patch("boto3.client") as mock_boto:
                mock_s3 = MagicMock()
                mock_s3.upload_file.side_effect = [None, error]
                mock_boto.return_value = mock_s3
                with self.assertRaises(RuntimeError):
                    self._node().upload_media(image=image)

        uploaded_paths = [call.args[0] for call in mock_s3.upload_file.call_args_list]
        self.assertEqual(len(uploaded_paths), 2)
        self.assertTrue(all(not os.path.exists(path) for path in uploaded_paths))

    # ------------------------------------------------------------------
    # upload failures — P2: both ClientError and S3UploadFailedError
    # ------------------------------------------------------------------

    def _run_upload_with_side_effect(self, side_effect, **kwargs):
        with patch.dict(os.environ, self.BASE_ENV):
            with patch("os.path.isfile", return_value=True):
                with patch("nodes.datetime") as mock_datetime, patch(
                    "boto3.client"
                ) as mock_boto:
                    mock_datetime.now.return_value.strftime.return_value = (
                        self.FIXED_TIMESTAMP
                    )
                    mock_s3 = MagicMock()
                    mock_s3.upload_file.side_effect = side_effect
                    mock_boto.return_value = mock_s3
                    self._node().upload_media(**kwargs)

    def test_client_error_raises_runtime_error(self):
        local_path = "/tmp/video.mp4"
        expected_key = f"media/{self.FIXED_TIMESTAMP}.mp4"
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
