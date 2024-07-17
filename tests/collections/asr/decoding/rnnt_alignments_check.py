# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


# NOTE: the file name does not contain "test" on purpose to avoid executing
#       these tests outside of the CI machines environment, where test data is
#       stored

import pytest
from examples.asr.transcribe_speech import TranscriptionConfig
from omegaconf import OmegaConf
from pathlib import Path
from nemo.collections.asr.parts.utils.manifest_utils import read_manifest, write_manifest
from nemo.collections.asr.parts.utils.transcribe_utils import prepare_audio_data, setup_model

PRETRAINED_MODEL_NAME = "stt_en_conformer_transducer_small"


@pytest.fixture
def an4_val_manifest_corrected(tmp_path, test_data_dir):
    """
    Correct an4_val manifest audio filepaths, e.g.,
    "tests/data/asr/test/an4/wav/an440-mjgm-b.wav" -> test_data_dir / "test/an4/wav/an440-mjgm-b.wav"
    """
    an4_val_manifest_orig_path = Path(test_data_dir) / "asr/an4_val.json"
    an4_val_manifest_corrected_path = tmp_path / "an4_val_corrected.json"
    an4_val_records = read_manifest(an4_val_manifest_orig_path)
    for record in an4_val_records:
        record["audio_filepath"] = record["audio_filepath"].replace(
            "tests/data/asr", str(an4_val_manifest_orig_path.resolve().parent)
        )
    write_manifest(an4_val_manifest_corrected_path, an4_val_records)
    return an4_val_manifest_corrected_path


def get_rnnt_alignments(
    strategy: str, manifest_path: Path, loop_labels: bool = True, use_cuda_graph_decoder=False, location="cuda"
):
    cfg = OmegaConf.structured(TranscriptionConfig(pretrained_name=PRETRAINED_MODEL_NAME))
    cfg.rnnt_decoding.confidence_cfg.preserve_frame_confidence = True
    cfg.rnnt_decoding.preserve_alignments = True
    cfg.rnnt_decoding.strategy = strategy
    if cfg.rnnt_decoding.strategy == "greedy_batch":
        cfg.rnnt_decoding.greedy.loop_labels = loop_labels
        cfg.rnnt_decoding.greedy.use_cuda_graph_decoder = use_cuda_graph_decoder
    cfg.dataset_manifest = str(manifest_path)
    filepaths = prepare_audio_data(cfg)[0][:10]  # selecting 10 files only

    model = setup_model(cfg, map_location=location)[0]
    model.change_decoding_strategy(cfg.rnnt_decoding)

    transcriptions = model.transcribe(
        audio=filepaths,
        batch_size=cfg.batch_size,
        num_workers=cfg.num_workers,
        return_hypotheses=True,
        channel_selector=cfg.channel_selector,
    )[0]

    for transcription in transcriptions:
        for align_elem, frame_confidence in zip(transcription.alignments, transcription.frame_confidence):
            assert len(align_elem) == len(frame_confidence)  # frame confidences have to match alignments
            assert len(align_elem) > 0  # no empty alignments
            for idx, pred in enumerate(align_elem):
                if idx < len(align_elem) - 1:
                    assert pred[1].item() != model.decoder.blank_idx  # all except last have to be non-blank
                else:
                    assert pred[1].item() == model.decoder.blank_idx  # last one has to be blank
    return transcriptions


@pytest.fixture(autouse=True)
def cleanup_local_folder():
    """Overriding global fixture to make sure it's not applied for this test.

    Otherwise, there will be errors in the CI in github.
    """
    return


# TODO: add the same tests for multi-blank RNNT decoding
@pytest.mark.parametrize("loop_labels", [True, False])
@pytest.mark.parametrize("use_cuda_graph_decoder", [True, False])
@pytest.mark.with_downloads
def test_rnnt_alignments(loop_labels: bool, use_cuda_graph_decoder: bool, an4_val_manifest_corrected):
    if not loop_labels and use_cuda_graph_decoder:
        pytest.skip("Frame-Looping algorithm with CUDA graphs does not yet support alignments")
    # using greedy as baseline and comparing all other configurations to it
    ref_transcriptions = get_rnnt_alignments("greedy", manifest_path=an4_val_manifest_corrected)
    transcriptions = get_rnnt_alignments(
        "greedy_batch",
        loop_labels=loop_labels,
        use_cuda_graph_decoder=use_cuda_graph_decoder,
        manifest_path=an4_val_manifest_corrected,
    )
    # comparing that label sequence in alignments is exactly the same
    # we can't compare logits as well, because they are expected to be
    # slightly different in batched and single-sample mode
    assert len(ref_transcriptions) == len(transcriptions)
    for ref_transcription, transcription in zip(ref_transcriptions, transcriptions):
        for ref_align_elem, align_elem in zip(ref_transcription.alignments, transcription.alignments):
            assert len(ref_align_elem) == len(align_elem)
            for ref_pred, pred in zip(ref_align_elem, align_elem):
                assert ref_pred[1].item() == pred[1].item()
