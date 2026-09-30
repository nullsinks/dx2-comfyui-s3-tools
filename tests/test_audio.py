"""Real PyAV encoding, independently decoded with libsndfile, plus upload tests."""

import inspect
import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import soundfile as sf
import torch
from boto3.exceptions import S3UploadFailedError

from nodes import DX2UploadMediaToS3


@pytest.fixture
def upload(monkeypatch):
    for name, value in {
        "S3_BUCKET": "test-bucket",
        "AWS_ACCESS_KEY_ID": "test-key",
        "AWS_SECRET_ACCESS_KEY": "test-secret",
    }.items():
        monkeypatch.setenv(name, value)
    client = MagicMock()
    captured = []

    def capture(path, bucket, key, **kwargs):
        captured.append((path, key, kwargs["ExtraArgs"], Path(path).read_bytes()))

    client.upload_file.side_effect = capture
    monkeypatch.setattr("nodes.boto3.client", lambda *args, **kwargs: client)
    return DX2UploadMediaToS3(), client, captured


@pytest.fixture
def temporary_audio_paths(monkeypatch):
    paths = []
    original = tempfile.mkstemp

    def record(*args, **kwargs):
        descriptor, path = original(*args, **kwargs)
        paths.append(path)
        return descriptor, path

    monkeypatch.setattr("nodes.tempfile.mkstemp", record)
    yield paths
    assert all(not os.path.exists(path) for path in paths)


def audio(waveform=None, sample_rate=48000):
    if waveform is None:
        waveform = torch.zeros((1, 2, 16))
    return {"waveform": waveform, "sample_rate": sample_rate}


@pytest.mark.parametrize("sample_rate,channels", [(44100, 1), (48000, 2)])
def test_real_flac_quality_across_chunks(upload, temporary_audio_paths, sample_rate, channels):
    node, client, captured = upload
    count = 65536 + 123  # exercise chunk timestamps and a short final frame
    time = torch.arange(count, dtype=torch.float64) / sample_rate
    tracks = [0.8 * torch.sin(2 * torch.pi * 437 * time)]
    if channels == 2:
        tracks.append(0.3 * torch.cos(2 * torch.pi * 919 * time))
    waveform = torch.stack(tracks).unsqueeze(0).requires_grad_()
    original = waveform.detach().clone()
    result = node.upload_media(
        audio=audio(waveform, sample_rate), file_name="song.mp3", upload_workflow=False
    )
    assert result[0].endswith(".flac")
    assert len(captured) == 1
    path, key, headers, data = captured[0]
    assert headers == {"ContentType": "audio/flac"}
    assert key.startswith("media/song-")
    info = sf.info(io.BytesIO(data))
    assert info.format == "FLAC"
    assert info.subtype == "PCM_24"
    assert info.samplerate == sample_rate
    assert info.channels == channels
    assert info.frames == count
    decoded, rate = sf.read(io.BytesIO(data), dtype="float64", always_2d=True)
    np.testing.assert_allclose(decoded, original[0].numpy().T, atol=2**-23, rtol=0)
    assert torch.equal(waveform, original)
    assert waveform.requires_grad
    assert client.upload_file.call_count == 1


@pytest.mark.parametrize("dtype", [torch.float16, torch.bfloat16, torch.float32, torch.float64])
def test_endpoints_low_level_samples_and_noncontiguous_tensors(upload, temporary_audio_paths, dtype):
    node, _, captured = upload
    values = torch.tensor([-1.0, 1.0, 0, 2**-20, -(2**-20), 0.5, -0.5], dtype=dtype)
    waveform = values.repeat(2).reshape(1, -1, 2).transpose(1, 2)
    assert not waveform.is_contiguous()
    node.upload_media(audio=audio(waveform), upload_workflow=False)
    decoded, _ = sf.read(io.BytesIO(captured[0][3]), always_2d=True)
    np.testing.assert_allclose(decoded, waveform[0].double().numpy().T, atol=2**-23, rtol=0)


def test_audio_batch_names_sidecars_and_last_uri(upload, temporary_audio_paths):
    node, _, captured = upload
    waveform = torch.stack((torch.zeros(2, 32), torch.full((2, 32), 0.25)))
    result = node.upload_media(
        audio=audio(waveform), s3_path="music/yue2", file_name="track.wav",
        prompt={"1": {"class_type": "VAEDecodeAudio"}},
        extra_pnginfo={"workflow": {"nodes": []}},
    )
    assert len(captured) == 4
    for index in range(2):
        _, key, headers, data = captured[index * 2]
        _, sidecar_key, sidecar_headers, sidecar = captured[index * 2 + 1]
        assert key.startswith("music/yue2/track-")
        assert key.endswith(f"-{index + 1:04d}.flac")
        assert sidecar_key == f"music/yue2/workflows/{Path(key).stem}.workflow.json"
        assert sidecar_headers == {"ContentType": "application/json"}
        payload = json.loads(sidecar)
        assert payload["media"]["batch_index"] == index + 1
        assert payload["media"]["batch_count"] == 2
        assert payload["media"]["s3_uri"] == f"s3://test-bucket/{key}"
        assert payload["comfyui"]["workflow"] == {"nodes": []}
        decoded, _ = sf.read(io.BytesIO(data), always_2d=True)
        np.testing.assert_array_equal(decoded, waveform[index].numpy().T)
    assert result == (f"s3://test-bucket/{captured[2][1]}",)


@pytest.mark.parametrize("payload,match", [
    ({}, "waveform and sample_rate"),
    (audio(sample_rate=True), "sample_rate"),
    (audio(sample_rate=48000.0), "sample_rate"),
    (audio(sample_rate=0), "sample_rate"),
    (audio(sample_rate=655351), "sample_rate"),
    (audio(torch.zeros((1, 2, 16), dtype=torch.int16)), "floating-point"),
    (audio([[0.1]]), "floating-point"),
    (audio(torch.zeros(2, 16)), "shape"),
    (audio(torch.zeros(0, 2, 16)), "empty"),
    (audio(torch.zeros(1, 0, 16)), "empty"),
    (audio(torch.zeros(1, 2, 0)), "empty"),
    (audio(torch.zeros(1, 3, 16)), "mono or stereo"),
    (audio(torch.full((1, 2, 16), float("nan"))), "nonfinite"),
    (audio(torch.full((1, 2, 16), float("inf"))), "nonfinite"),
    (audio(torch.full((1, 2, 16), 1.01)), "would clip"),
    (audio(torch.full((1, 2, 16), -1.01)), "would clip"),
])
def test_invalid_audio_never_uploads(upload, temporary_audio_paths, payload, match):
    node, client, _ = upload
    with pytest.raises(ValueError, match=match):
        node.upload_media(audio=payload)
    client.upload_file.assert_not_called()


def test_late_invalid_audio_cleans_all_encoded_files(upload, temporary_audio_paths):
    node, client, _ = upload
    waveform = torch.zeros(2, 2, 65537)
    waveform[1, 0, -1] = 1.5
    with pytest.raises(ValueError, match="track 2.*would clip"):
        node.upload_media(audio=audio(waveform))
    assert len(temporary_audio_paths) == 2
    client.upload_file.assert_not_called()


def test_disabled_audio_does_not_import_or_encode(upload):
    node, client, _ = upload
    with patch.dict("sys.modules", {"av": None}):
        assert node.upload_media(audio=audio(), enabled=False) == ("upload_skipped",)
    client.upload_file.assert_not_called()


def test_audio_dependency_is_lazy_with_clear_error(upload, tmp_path):
    node, _, _ = upload
    saved = tmp_path / "already-saved.flac"
    saved.write_bytes(b"existing-file")
    with patch.dict("sys.modules", {"av": None}):
        assert node.upload_media(local_path=str(saved), upload_workflow=False)[0].endswith(".flac")
        with pytest.raises(RuntimeError, match="native audio requires PyAV"):
            node.upload_media(audio=audio())


def test_serialization_failure_cleans_temporary_file(upload, temporary_audio_paths):
    node, client, _ = upload
    with patch("av.open", side_effect=RuntimeError("encode failed")):
        with pytest.raises(RuntimeError, match="encode failed"):
            node.upload_media(audio=audio())
    assert len(temporary_audio_paths) == 1
    client.upload_file.assert_not_called()


def test_partial_upload_failure_cleans_all_audio(upload, temporary_audio_paths):
    node, client, _ = upload
    client.upload_file.side_effect = [None, S3UploadFailedError("connection lost")]
    with pytest.raises(RuntimeError, match="upload failed for s3://test-bucket/"):
        node.upload_media(audio=audio(torch.zeros(2, 1, 16)), upload_workflow=False)
    assert client.upload_file.call_count == 2
    assert len(temporary_audio_paths) == 2


def test_audio_sidecar_failure_is_best_effort(upload, temporary_audio_paths, caplog):
    node, client, _ = upload
    client.upload_file.side_effect = [None, S3UploadFailedError("sidecar failed")]
    result = node.upload_media(audio=audio())
    assert result[0].endswith(".flac")
    assert "workflow sidecar failed" in caplog.text


def test_audio_precedes_paths(upload, temporary_audio_paths):
    node, _, captured = upload
    node.upload_media(
        audio=audio(), local_path="does-not-exist.mp4",
        vhs_filenames=(True, ["also-missing.mp4"]), upload_workflow=False,
    )
    assert captured[0][1].endswith(".flac")


@pytest.mark.parametrize("source", ["image", "video"])
def test_existing_native_media_precedes_audio(upload, temporary_audio_paths, source):
    node, _, captured = upload
    if source == "image":
        native = torch.zeros(1, 2, 3, 3)
        expected = "image/png"
    else:
        native = MagicMock()
        native.save_to.side_effect = lambda path: Path(path).write_bytes(b"video")
        expected = "video/mp4"
    node.upload_media(**{source: native}, audio={}, upload_workflow=False)
    assert captured[0][2] == {"ContentType": expected}


def test_audio_does_not_shift_existing_interface():
    optional = DX2UploadMediaToS3.INPUT_TYPES()["optional"]
    assert list(optional) == [
        "image", "video", "local_path", "vhs_filenames", "s3_path", "file_name",
        "enabled", "upload_workflow", "audio",
    ]
    assert optional["audio"] == ("AUDIO",)
    assert list(inspect.signature(DX2UploadMediaToS3.upload_media).parameters) == [
        "self", "local_path", "vhs_filenames", "s3_path", "file_name", "enabled",
        "upload_workflow", "video", "image", "prompt", "extra_pnginfo", "audio",
    ]
    assert DX2UploadMediaToS3.RETURN_TYPES == ("STRING",)


@pytest.mark.parametrize("suffix,expected", [
    (".PNG", "image/png"), (".jpeg", "image/jpeg"), (".webp", "image/webp"),
    (".gif", "image/gif"), (".mp4", "video/mp4"), (".webm", "video/webm"),
    (".mov", "video/quicktime"), (".mkv", "video/x-matroska"),
    (".flac", "audio/flac"), (".wav", "audio/wav"), (".mp3", "audio/mpeg"),
    (".m4a", "audio/mp4"), (".ogg", "audio/ogg"), (".opus", "audio/ogg"),
    (".workflow.json", "application/json"),
    (".dx2unknown", "application/octet-stream"), ("", "application/octet-stream"),
])
def test_upload_content_type_uses_source_not_destination(upload, tmp_path, suffix, expected):
    node, _, captured = upload
    source = tmp_path / f"source{suffix}"
    source.write_bytes(b"unchanged media bytes")
    node.upload_media(local_path=str(source), file_name="renamed.txt", upload_workflow=False)
    assert captured[0][1].endswith(".txt")
    assert captured[0][2] == {"ContentType": expected}
    assert captured[0][3] == source.read_bytes()
    assert source.exists()


def test_mime_fallback_and_compressed_data():
    with patch("nodes.mimetypes.guess_type", return_value=("application/pdf", None)):
        assert DX2UploadMediaToS3._content_type("document.pdf") == "application/pdf"
    with patch("nodes.mimetypes.guess_type", return_value=("text/plain", "gzip")):
        assert DX2UploadMediaToS3._content_type("document.txt.gz") == "application/octet-stream"


def test_native_video_content_type_survives_destination_rename(upload, temporary_audio_paths):
    node, _, captured = upload
    video = MagicMock()
    video.save_to.side_effect = lambda path: Path(path).write_bytes(b"video")
    node.upload_media(video=video, file_name="renamed.webm", upload_workflow=False)
    assert captured[0][2] == {"ContentType": "video/mp4"}
