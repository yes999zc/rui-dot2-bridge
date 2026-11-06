"""A replaced device connection must not contribute stale microphone frames."""

import asyncio
from types import SimpleNamespace

import pytest

from rui_dot2_bridge.device_link import DeviceLink
from rui_dot2_bridge.frames import FRAME_MIC


class BufferedConnection:
    """Model a socket whose reader can yield buffered data after abort()."""

    def __init__(self):
        self.queue = asyncio.Queue()
        self.transport = SimpleNamespace(abort=lambda: None)
        self.remote_address = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.queue.get()
        if frame is None:
            raise StopAsyncIteration
        return frame


def mic(payload):
    return bytes([FRAME_MIC, 0, 1]) + payload


@pytest.mark.parametrize("ref_uplink,size", [(False, 2560), (True, 5120)])
def test_superseded_buffered_socket_and_queued_audio_are_discarded(ref_uplink, size):
    asyncio.run(check_generations(ref_uplink, size))


async def check_generations(ref_uplink, size):
    link = DeviceLink(tls_port=None, device_id="synthetic", ref_uplink=ref_uplink)
    old, new = BufferedConnection(), BufferedConnection()
    old_task = asyncio.create_task(link._serve_data(old))
    new_task = None
    try:
        await asyncio.sleep(0)
        await old.queue.put(mic(b"A" * size))
        await asyncio.sleep(0)
        assert link.audio.qsize() == 1

        new_task = asyncio.create_task(link._serve_data(new))
        await asyncio.sleep(0)
        assert link._data is new
        assert link.audio.empty()
        assert link.stats.stale_audio_dropped == 1

        await old.queue.put(mic(b"B" * size))
        await new.queue.put(mic(b"C" * size))
        await asyncio.sleep(0)
        assert await asyncio.wait_for(link.audio.get(), 1) == b"C" * size
        assert link.audio.empty()
        assert link.stats.mic_frames == 2

        # Closing the superseded handler must not erase the new generation.
        await new.queue.put(mic(b"D" * size))
        await asyncio.sleep(0)
        await old.queue.put(None)
        await old_task
        assert await asyncio.wait_for(link.audio.get(), 1) == b"D" * size

        # EOF alone leaves the last valid audio available to the consumer.
        await new.queue.put(mic(b"E" * size))
        await asyncio.sleep(0)
        await new.queue.put(None)
        await new_task
        assert await asyncio.wait_for(link.audio.get(), 1) == b"E" * size
        assert link.stats.stale_audio_dropped == 1
    finally:
        old_task.cancel()
        if new_task is not None:
            new_task.cancel()
        await asyncio.gather(old_task, *([new_task] if new_task else []), return_exceptions=True)


def test_queued_audio_survives_clear_until_a_new_data_generation():
    async def scenario():
        link = DeviceLink(tls_port=None, device_id="synthetic")
        link._enqueue_audio(b"old")
        link._clear_link_state()
        assert link.audio.qsize() == 1
        assert link.stats.stale_audio_dropped == 0
        link._adopt_data(BufferedConnection())
        assert link.audio.empty()
        assert link.stats.stale_audio_dropped == 1

    asyncio.run(scenario())
