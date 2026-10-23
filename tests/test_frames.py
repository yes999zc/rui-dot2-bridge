"""Pure wire-format tests: no hardware, no sockets (see docs/protocol-map.md)."""

from __future__ import annotations

import unittest

from rui_dot2_bridge import frames


class FrameTypeTest(unittest.TestCase):
    def test_parse_splits_type_and_payload(self):
        kind, payload = frames.parse_data_frame(bytes([frames.FRAME_MIC]) + b"\x01\x02")
        self.assertEqual(kind, frames.FRAME_MIC)
        self.assertEqual(payload, b"\x01\x02")

    def test_empty_frame_is_an_error(self):
        with self.assertRaises(ValueError):
            frames.parse_data_frame(b"")


class SpeakerTest(unittest.TestCase):
    def test_chunk_is_4096_bytes_and_prefixed(self):
        pcm = bytes(frames.SPEAKER_CHUNK_BYTES)
        frame = frames.speaker_frame(pcm)
        self.assertEqual(frame[0], frames.FRAME_SPEAKER)
        self.assertEqual(len(frame), frames.SPEAKER_CHUNK_BYTES + 1)

    def test_oversized_chunk_rejected(self):
        with self.assertRaises(ValueError):
            frames.speaker_frame(bytes(frames.SPEAKER_CHUNK_BYTES + 2))

    def test_split_playback_covers_input_once(self):
        pcm = bytes(frames.SPEAKER_CHUNK_BYTES * 2 + 100)
        chunks = frames.split_playback(pcm)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(sum(len(c) - 1 for c in chunks), len(pcm))
        self.assertTrue(all(c[0] == frames.FRAME_SPEAKER for c in chunks))

    def test_eos_is_one_byte(self):
        self.assertEqual(frames.speaker_eos(), bytes([frames.FRAME_SPEAKER_EOS]))


class RechunkTest(unittest.TestCase):
    def test_80ms_becomes_four_20ms_blocks(self):
        pcm = bytes(frames.MIC_CHUNK_BYTES)
        blocks = frames.rechunk_voice(pcm)
        self.assertEqual(len(blocks), 4)
        self.assertTrue(all(len(b) == 640 for b in blocks))  # 20 ms * 16 kHz * 2 B
        self.assertEqual(b"".join(blocks), pcm)


class LevelTest(unittest.TestCase):
    def test_dbfs_of_full_scale_square_is_zero(self):
        pcm = (32767).to_bytes(2, "little", signed=True) * 160
        self.assertAlmostEqual(frames.mic_dbfs(pcm), 0.0, places=3)

    def test_dbfs_of_silence_is_neg_inf(self):
        self.assertEqual(frames.mic_dbfs(bytes(320)), float("-inf"))


if __name__ == "__main__":
    unittest.main()
