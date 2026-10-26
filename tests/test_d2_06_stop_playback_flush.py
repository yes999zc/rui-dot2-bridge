"""D2-06: an active stop must not swallow the next reply's first PCM.

The device arms ``discarding`` when ``speaker_flush`` arrives while a stream is
active; only an EOS clears it. ``Dot2Adapter.stop_playback()`` therefore sends
``flush -> end_stream`` when a stream is live (adapter.py:804-812). The existing
tests count ``flush``/``eos`` calls; the gap this file closes is the *device-side
consequence*: play a cue, stop it mid-stream, speak again immediately, and assert
the first block of the next reply actually reaches the device instead of being
dropped while ``discarding`` is still set.

The fake link models the firmware state machine from
``docs/stutter-adapter-options.md`` §7.9: flush sets ``eos_pending`` (and
``discarding`` when active), EOS clears both.
"""
from __future__ import annotations

import unittest

from rui_dot2_bridge.adapter import Dot2Adapter
from rui_dot2_bridge.frames import SPEAKER_CHUNK_BYTES


class DiscardingDeviceLink:
    """A fake device that drops PCM while ``discarding`` is armed."""

    def __init__(self) -> None:
        self.volume_level = 38
        self.sent: list[bytes] = []
        self.accepted_bytes = 0
        self.dropped_bytes = 0
        self.flushed = 0
        self.eos = 0
        self.active = False
        self.discarding = False
        self.eos_pending = False
        #: Proof that the flush itself armed EOS (the adapter must clear it).
        self.flush_armed_eos = False

    async def send_pcm(self, pcm: bytes) -> None:
        if self.discarding:
            self.dropped_bytes += len(pcm)
            return
        self.active = True
        self.accepted_bytes += len(pcm)
        self.sent.append(bytes(pcm))

    async def end_stream(self) -> None:
        self.eos += 1
        self.eos_pending = False
        self.discarding = False
        self.active = False

    async def flush(self) -> None:
        self.flushed += 1
        self.eos_pending = True
        self.flush_armed_eos = True
        if self.active:
            self.discarding = True


def _adapter() -> tuple[Dot2Adapter, DiscardingDeviceLink]:
    adapter = Dot2Adapter("dot2", host="<lan-ip>", secrets_dir="secrets")
    # Start every stream immediately; otherwise a 4096-byte cue sits in the
    # prebuffer and `active` is only half-true.
    adapter._prebuffer_bytes = 0
    link = DiscardingDeviceLink()
    adapter._link = link
    return adapter, link


class StopPlaybackFlushTest(unittest.IsolatedAsyncioTestCase):
    async def test_active_stop_clears_discarding_before_the_next_reply(self) -> None:
        adapter, link = _adapter()
        cue = bytes(SPEAKER_CHUNK_BYTES)
        await adapter.play(cue, sample_rate=48000, channels=1)
        self.assertEqual(link.accepted_bytes, SPEAKER_CHUNK_BYTES)
        self.assertTrue(link.active)

        await adapter.stop_playback()

        # The flush armed EOS; the adapter's EOS must clear the device state that
        # would otherwise eat the next stream's head.
        self.assertEqual(link.flushed, 1)
        self.assertTrue(link.flush_armed_eos)

        before = link.accepted_bytes
        await adapter.play(cue, sample_rate=48000, channels=1)

        self.assertEqual(link.dropped_bytes, 0)
        self.assertEqual(link.accepted_bytes, before + SPEAKER_CHUNK_BYTES)
        self.assertEqual(link.sent[-1], cue)
        self.assertFalse(link.eos_pending)
        self.assertFalse(link.discarding)
        self.assertEqual(link.eos, 1)

    async def test_idle_stop_does_not_add_an_eos(self) -> None:
        """No live stream: flush alone (it opens the device's prime bypass)."""
        adapter, link = _adapter()

        await adapter.stop_playback()

        self.assertEqual(link.flushed, 1)
        self.assertEqual(link.eos, 0)
        self.assertFalse(link.discarding)
        # eos_pending is not asserted here: the fake arms it on every flush, but
        # D2-06 only specifies what an active stream must do with it.


if __name__ == "__main__":
    unittest.main()
