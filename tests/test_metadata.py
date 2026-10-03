"""Read embedded provenance back from uploaded bytes and exercise VIDEO delegation."""

import copy
import io
import json
import sys
from fractions import Fraction
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import av
import numpy as np
from PIL import Image
import pytest
import soundfile as sf
import torch

from nodes import DX2UploadMediaToS3
from test_audio import upload, temporary_audio_paths  # shared upload/cleanup fixtures


PROMPT = {"1": {"class_type": "Generator", "inputs": {"text": "café / 音楽 🎵", "seed": 42}}}
EXTRA = {"workflow": {"nodes": [{"id": 1}], "links": []}, "caption": "line 1\nline 2"}
EXPECTED = {**EXTRA, "prompt": PROMPT}


def source(kind, batch=1):
    if kind == "image":
        return torch.linspace(0, 1, batch * 4 * 6 * 3).reshape(batch, 4, 6, 3)
    return {
        "waveform": torch.linspace(-0.9, 0.9, batch * 2 * 1000).reshape(batch, 2, 1000),
        "sample_rate": 48000,
    }


def read_tags(data, kind):
    if kind == "image":
        with Image.open(io.BytesIO(data)) as image:
            return dict(image.info)
    with av.open(io.BytesIO(data)) as container:
        return dict(container.metadata)


@pytest.mark.parametrize("kind", ["image", "audio"])
@pytest.mark.parametrize("sidecars", [False, True])
def test_metadata_round_trip_in_every_batch_item(upload, temporary_audio_paths, kind, sidecars):
    node, _, captured = upload
    prompt, extra = copy.deepcopy(PROMPT), copy.deepcopy(EXTRA)
    node.upload_media(
        **{kind: source(kind, batch=2)}, prompt=prompt, extra_pnginfo=extra,
        upload_workflow=sidecars,
    )
    assert len(captured) == (4 if sidecars else 2)
    for _, key, headers, data in captured:
        if key.endswith(".workflow.json"):
            envelope = json.loads(data)["comfyui"]
            assert envelope["prompt"] == PROMPT
            assert envelope["workflow"] == EXTRA["workflow"]
        else:
            tags = read_tags(data, kind)
            assert {key: json.loads(tags[key]) for key in EXPECTED} == EXPECTED
            assert headers["ContentType"] == ("image/png" if kind == "image" else "audio/flac")
    assert prompt == PROMPT and extra == EXTRA


@pytest.mark.parametrize("kind", ["image", "audio"])
def test_embedding_does_not_change_decoded_media(upload, temporary_audio_paths, kind):
    node, _, captured = upload
    native = source(kind)
    for enabled in (False, True):
        node.upload_media(
            **{kind: native}, prompt=PROMPT, extra_pnginfo=EXTRA,
            embed_metadata=enabled, upload_workflow=False,
        )
    data = [entry[3] for entry in captured]
    if kind == "image":
        decoded = []
        for content in data:
            with Image.open(io.BytesIO(content)) as image:
                decoded.append(np.array(image))
    else:
        decoded = [sf.read(io.BytesIO(content), dtype="int32")[0] for content in data]
        for content in data:
            info = sf.info(io.BytesIO(content))
            assert (info.subtype, info.samplerate, info.channels, info.frames) == (
                "PCM_24", 48000, 2, 1000
            )
    np.testing.assert_array_equal(*decoded)
    assert "prompt" not in read_tags(data[0], kind)
    assert json.loads(read_tags(data[1], kind)["prompt"]) == PROMPT


@pytest.mark.parametrize("kind", ["image", "audio", "video"])
@pytest.mark.parametrize("global_disable,embed,sidecars", [
    (False, False, True), (True, True, True), (False, True, False), (True, True, False),
])
def test_embedding_controls_are_independent_of_sidecars(
    upload, temporary_audio_paths, monkeypatch, kind, global_disable, embed, sidecars
):
    cli_args = ModuleType("comfy.cli_args")
    cli_args.args = SimpleNamespace(disable_metadata=global_disable)
    monkeypatch.setitem(sys.modules, "comfy", ModuleType("comfy"))
    monkeypatch.setitem(sys.modules, "comfy.cli_args", cli_args)
    node, _, captured = upload
    if kind == "video":
        native = MagicMock()
        native.save_to.side_effect = lambda path, **kwargs: Path(path).write_bytes(b"video")
    else:
        native = source(kind)
    node.upload_media(
        **{kind: native}, prompt=PROMPT, extra_pnginfo=EXTRA,
        embed_metadata=embed, upload_workflow=sidecars,
    )
    expected = embed and not global_disable
    if kind == "video":
        assert native.save_to.call_count == 1
        assert native.save_to.call_args.kwargs == ({"metadata": EXPECTED} if expected else {})
    else:
        assert ("prompt" in read_tags(captured[0][3], kind)) == expected
    assert len(captured) == (2 if sidecars else 1)
    if sidecars:
        assert json.loads(captured[1][3])["comfyui"]["workflow"] == EXTRA["workflow"]


@pytest.mark.parametrize("kind", ["image", "audio"])
def test_api_prompt_without_editable_workflow(upload, temporary_audio_paths, kind):
    node, _, captured = upload
    node.upload_media(**{kind: source(kind)}, prompt=PROMPT, upload_workflow=False)
    tags = read_tags(captured[0][3], kind)
    assert json.loads(tags["prompt"]) == PROMPT
    assert "workflow" not in tags


def test_builder_preserves_executed_prompt_and_snapshots_inputs():
    extra = {**copy.deepcopy(EXTRA), "prompt": {"wrong": "graph"}}
    metadata = DX2UploadMediaToS3._build_embedded_metadata(PROMPT, extra)
    assert metadata == EXPECTED
    metadata["workflow"]["nodes"].clear()
    assert extra["workflow"] == EXTRA["workflow"]
    assert DX2UploadMediaToS3._build_embedded_metadata(None, None) == {}


@pytest.mark.parametrize("bad_key", [None, 1, "", "=tag", "x" * 80, "音楽", "bad\nkey", " leading"])
def test_invalid_metadata_keys_are_skipped(bad_key, caplog):
    assert DX2UploadMediaToS3._build_embedded_metadata(PROMPT, {bad_key: "value"}) == {
        "prompt": PROMPT
    }
    assert "invalid embedded metadata key" in caplog.text


@pytest.mark.parametrize("bad_value", [{1, 2}, float("nan"), float("inf"), object()])
def test_non_json_extra_field_does_not_discard_valid_metadata(bad_value, caplog):
    metadata = DX2UploadMediaToS3._build_embedded_metadata(PROMPT, {**EXTRA, "bad": bad_value})
    assert metadata == EXPECTED
    assert "skipping non-JSON embedded metadata field bad" in caplog.text


@pytest.mark.parametrize("input_name", ["local_path", "vhs_filenames"])
def test_existing_files_are_not_rewritten(upload, tmp_path, input_name):
    node, _, captured = upload
    path = tmp_path / "existing.flac"
    paths = []
    try:
        saved, _ = node._materialize_audio(source("audio"), paths, {"prompt": {"original": True}})
        data = Path(saved[0]).read_bytes()
        path.write_bytes(data)
    finally:
        for temporary_path in paths:
            Path(temporary_path).unlink()
    value = str(path) if input_name == "local_path" else (True, [str(path)])
    node.upload_media(**{input_name: value}, prompt=PROMPT, extra_pnginfo=EXTRA, upload_workflow=False)
    assert captured[0][3] == path.read_bytes() == data
    assert json.loads(read_tags(data, "audio")["prompt"]) == {"original": True}


@pytest.mark.parametrize("bit_depth", [8, 10])
@pytest.mark.parametrize("from_file", [False, True])
def test_comfy_video_metadata_and_encoded_streams(upload, temporary_audio_paths, tmp_path, bit_depth, from_file):
    """Runs when ComfyUI is on PYTHONPATH; exercises its actual MP4 saver/remuxer."""
    video_types = pytest.importorskip("comfy_api.latest._input_impl.video_types")
    native = video_types.VideoFromComponents(SimpleNamespace(
        images=torch.linspace(0, 1, 4 * 16 * 16 * 3).reshape(4, 16, 16, 3),
        audio={"waveform": torch.zeros(1, 2, 8000), "sample_rate": 48000},
        frame_rate=Fraction(24),
    ), bit_depth=bit_depth)
    if from_file:
        original = tmp_path / "original.mp4"
        native.save_to(str(original))
        native = video_types.VideoFromFile(str(original))
    node, _, captured = upload
    for embed in (False, True):
        node.upload_media(video=native, prompt=PROMPT, extra_pnginfo=EXTRA,
                          embed_metadata=embed, upload_workflow=False)
    encoded = []
    for _, _, headers, data in captured:
        assert headers == {"ContentType": "video/mp4"}
        with av.open(io.BytesIO(data)) as container:
            assert container.streams.video[0].pix_fmt == ("yuv420p10le" if bit_depth == 10 else "yuv420p")
            assert container.streams.video[0].average_rate == Fraction(24)
            assert len(container.streams.audio) == 1
            encoded.append([(packet.stream.type, packet.pts, packet.dts, bytes(packet))
                            for packet in container.demux() if packet.size])
    assert encoded[0] == encoded[1]
    assert "prompt" not in read_tags(captured[0][3], "video")
    tags = read_tags(captured[1][3], "video")
    # Both VIDEO implementations must expose structured prompt/workflow after one decode.
    for key in ("prompt", "workflow"):
        assert json.loads(tags[key]) == EXPECTED[key]
