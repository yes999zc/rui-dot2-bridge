"""Socket-write receipts use a bounded, isolated delivery scope, not audio replay."""
import asyncio
from functools import wraps

import pytest
from test_adapter import make_adapter

from rui_dot2_bridge.device_link import DeviceLink
from rui_dot2_bridge.frames import SPEAKER_CHUNK_BYTES


def run_async(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


@run_async
async def test_direct_send_failure_is_sticky_without_replay():
    adapter = make_adapter()
    adapter._prebuffer_bytes = 0
    attempted = []

    async def partial_then_fail(pcm):
        attempted.append(pcm)
        return False

    adapter._link.send_pcm = partial_then_fail
    token = adapter.begin_delivery()
    await adapter.play_delivery(token, bytes(SPEAKER_CHUNK_BYTES * 2), sample_rate=48000, channels=1)
    assert await adapter.finish_delivery(token, timeout_s=1) == 'unknown'
    assert len(attempted) == 1
    assert not adapter._frame_buffer


@run_async
async def test_prebuffer_failure_marks_original_delivery():
    adapter = make_adapter()
    adapter._prebuffer_start_s = .01
    async def fail(pcm):
        return False
    adapter._link.send_pcm = fail
    token = adapter.begin_delivery()
    await adapter.play_delivery(token, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    assert await adapter.finish_delivery(token, timeout_s=1) == 'unknown'


@pytest.mark.parametrize('failure', ['tail', 'eos'])
@run_async
async def test_tail_and_eos_failures_do_not_report_sent(failure):
    adapter = make_adapter()
    adapter._prebuffer_start_s = .01
    adapter._eos_delay = lambda: .02
    async def send(pcm):
        return failure != 'tail'
    async def eos():
        return failure != 'eos'
    adapter._link.send_pcm = send
    adapter._link.end_stream = eos
    token = adapter.begin_delivery()
    await adapter.play_delivery(token, bytes(1000), sample_rate=48000, channels=1)
    assert await adapter.finish_delivery(token, timeout_s=1) == ('unknown' if failure == 'tail' else 'partial')


@run_async
async def test_old_delayed_failure_does_not_poison_new_delivery():
    adapter = make_adapter()
    adapter._prebuffer_bytes = 0
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def send(pcm):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            return False
        return True
    adapter._link.send_pcm = send
    adapter._link.end_stream = lambda: asyncio.sleep(0, result=True)
    adapter._eos_delay = lambda: .02
    old = adapter.begin_delivery()
    old_play = asyncio.create_task(adapter.play_delivery(old, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1))
    await entered.wait()
    adapter.cancel_delivery(old)
    new = adapter.begin_delivery()
    new_play = asyncio.create_task(adapter.play_delivery(
        new, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1))
    await asyncio.sleep(0)
    assert not new_play.done()
    release.set()
    await old_play
    await asyncio.wait_for(new_play, 1)
    assert await adapter.finish_delivery(old, timeout_s=1) == 'unknown'
    assert await adapter.finish_delivery(new, timeout_s=1) == 'sent'


@run_async
async def test_timeout_and_storage_are_bounded():
    adapter = make_adapter()
    adapter._prebuffer_start_s = 10
    adapter._link.send_pcm = lambda pcm: asyncio.Event().wait()
    token = adapter.begin_delivery()
    await adapter.play_delivery(token, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    assert await adapter.finish_delivery(token, timeout_s=.01) == 'unknown'
    for _ in range(100):
        adapter.begin_delivery()
    assert len(adapter._delivery_scopes) <= 32


@run_async
async def test_finish_waits_for_eos_and_then_reports_socket_only():
    adapter = make_adapter()
    adapter._prebuffer_bytes = 0
    adapter._eos_delay = lambda: .01
    entered, release = asyncio.Event(), asyncio.Event()

    async def eos():
        entered.set()
        await release.wait()
        return True

    adapter._link.end_stream = eos
    token = adapter.begin_delivery()
    await adapter.play_delivery(token, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    result = asyncio.create_task(adapter.finish_delivery(token, timeout_s=1))
    await asyncio.wait_for(entered.wait(), 1)
    assert not result.done()
    release.set()
    assert await result == 'sent'


@run_async
async def test_cancelled_or_failed_scope_drops_late_pcm_until_new_scope():
    adapter = make_adapter()
    adapter._prebuffer_bytes = 0
    token = adapter.begin_delivery()
    adapter.cancel_delivery(token)
    await adapter.play_delivery(token, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    assert not adapter._link.sent
    new = adapter.begin_delivery()
    await adapter.play_delivery(new, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    assert len(adapter._link.sent) == 1
    adapter.cancel_delivery(new)


@run_async
async def test_one_delivery_can_cover_multiple_finished_streams():
    adapter = make_adapter()
    adapter._prebuffer_bytes = 0
    adapter._eos_delay = lambda: .01
    token = adapter.begin_delivery()
    await adapter.play_delivery(token, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    await asyncio.sleep(.05)
    assert adapter._link.eos == 1
    await adapter.play_delivery(token, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    assert await adapter.finish_delivery(token, timeout_s=1) == 'sent'
    assert adapter._link.eos == 2


@run_async
async def test_stop_cancels_scope_and_never_reports_old_delivery():
    adapter = make_adapter()
    adapter._prebuffer_start_s = 10
    token = adapter.begin_delivery()
    await adapter.play_delivery(token, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    await adapter.stop_playback()
    assert await adapter.finish_delivery(token, timeout_s=1) == 'cancelled'
    await adapter.play_delivery(token, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    assert not adapter._link.sent


@run_async
async def test_cancelled_scope_does_not_block_legacy_music():
    adapter = make_adapter()
    adapter._prebuffer_bytes = 0
    token = adapter.begin_delivery()
    adapter.cancel_delivery(token)
    await adapter.play(bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    assert len(adapter._link.sent) == 1
    await adapter.play_delivery(token, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    assert len(adapter._link.sent) == 1


@run_async
async def test_failure_stays_failed_even_after_reconnect():
    adapter = make_adapter()
    adapter._prebuffer_bytes = 0
    calls = []

    async def fail(pcm):
        calls.append(pcm)
        return False

    adapter._link.send_pcm = fail
    token = adapter.begin_delivery()
    await adapter.play_delivery(token, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    adapter._link.send_pcm = lambda pcm: asyncio.sleep(0, result=True)
    await adapter.play_delivery(token, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    assert await adapter.finish_delivery(token, timeout_s=1) == 'unknown'
    assert len(calls) == 1


@run_async
async def test_late_old_eos_does_not_complete_new_scope():
    adapter = make_adapter()
    adapter._prebuffer_bytes = 0
    adapter._eos_delay = lambda: .01
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0
    new_release = asyncio.Event()

    async def eos():
        nonlocal count
        count += 1
        if count == 1:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # Model a lower layer that completes a write after cancellation.
                await release.wait()
        else:
            await new_release.wait()
        return True

    adapter._link.end_stream = eos
    old = adapter.begin_delivery()
    await adapter.play_delivery(old, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    await asyncio.wait_for(entered.wait(), 1)
    adapter.cancel_delivery(old)
    new = adapter.begin_delivery()
    adapter._eos_delay = lambda: .2
    new_play = asyncio.create_task(adapter.play_delivery(
        new, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1))
    await asyncio.sleep(0)
    assert not new_play.done()
    release.set()
    await asyncio.wait_for(new_play, 1)
    new_result = asyncio.create_task(adapter.finish_delivery(new, timeout_s=1))
    await asyncio.sleep(.05)
    assert not new_result.done()
    new_release.set()
    assert await new_result == 'sent'
    assert await adapter.finish_delivery(old, timeout_s=1) == 'partial'


@run_async
async def test_link_reports_missing_data_connection_for_eos_and_pcm():
    link = DeviceLink(tls_port=None, plain_port=0, device_id='test')
    assert await link.send_pcm(bytes(SPEAKER_CHUNK_BYTES)) is False
    assert await link.end_stream() is False


@run_async
async def test_partial_long_tail_is_written_for_scoped_receipt():
    adapter = make_adapter()
    adapter._prebuffer_bytes = 0
    adapter._eos_delay = lambda: .01
    token = adapter.begin_delivery()
    await adapter.play_delivery(token, bytes(SPEAKER_CHUNK_BYTES * 36 + 100),
                                sample_rate=48000, channels=1)
    assert await adapter.finish_delivery(token, timeout_s=1) == 'sent'
    assert len(adapter._link.sent) == 37


@run_async
async def test_timeout_is_terminal_and_stale_token_cannot_play():
    adapter = make_adapter()
    adapter._prebuffer_start_s = 10
    adapter._link.send_pcm = lambda pcm: asyncio.Event().wait()
    token = adapter.begin_delivery()
    await adapter.play_delivery(token, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    assert await adapter.finish_delivery(token, timeout_s=.01) == 'unknown'
    await adapter.play_delivery(token, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    assert not adapter._link.sent
    assert await adapter.finish_delivery(token, timeout_s=1) == 'unknown'


@run_async
async def test_cancelling_receipt_wait_aborts_only_that_scope():
    adapter = make_adapter()
    adapter._prebuffer_start_s = 10
    entered = asyncio.Event()
    async def block(pcm):
        entered.set()
        await asyncio.Event().wait()
    adapter._link.send_pcm = block
    token = adapter.begin_delivery()
    await adapter.play_delivery(token, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    task = asyncio.create_task(adapter.finish_delivery(token, timeout_s=1))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await adapter.finish_delivery(token, timeout_s=1) == 'unknown'
    assert not adapter._frame_buffer


@run_async
async def test_link_reports_partial_multiframe_failure_without_retry():
    link = DeviceLink(tls_port=None, plain_port=0, device_id='test')
    conn = object()
    link._data = conn
    calls = []

    async def write(seen_conn, frame):
        assert seen_conn is conn
        calls.append(frame)
        return len(calls) == 1

    link._write_data_frame = write
    result = await link.send_pcm(bytes(SPEAKER_CHUNK_BYTES * 3))
    assert result is False
    assert len(calls) == 2
    assert link.stats.speaker_frames == 1


@run_async
async def test_old_eos_cannot_finish_new_sentence_in_same_scope():
    adapter = make_adapter()
    adapter._prebuffer_bytes = 0
    adapter._eos_delay = lambda: .01
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0
    new_release = asyncio.Event()

    async def eos():
        nonlocal count
        count += 1
        if count == 1:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
        else:
            await new_release.wait()
        return True

    adapter._link.end_stream = eos
    token = adapter.begin_delivery()
    await adapter.play_delivery(token, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    await asyncio.wait_for(entered.wait(), 1)
    adapter._eos_delay = lambda: .2
    new_play = asyncio.create_task(adapter.play_delivery(
        token, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1))
    await asyncio.sleep(0)
    assert not new_play.done()
    release.set()
    await asyncio.wait_for(new_play, 1)
    result = asyncio.create_task(adapter.finish_delivery(token, timeout_s=1))
    await asyncio.sleep(.05)
    assert not result.done()
    new_release.set()
    assert await result == 'sent'


@run_async
async def test_eos_waits_for_a_blocked_prebuffer_send():
    adapter = make_adapter()
    adapter._prebuffer_start_s = .01
    adapter._eos_delay = lambda: .04
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(pcm):
        entered.set()
        await release.wait()
        return True

    adapter._link.send_pcm = send
    token = adapter.begin_delivery()
    await adapter.play_delivery(token, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    await asyncio.wait_for(entered.wait(), 1)
    result = asyncio.create_task(adapter.finish_delivery(token, timeout_s=1))
    await asyncio.sleep(.1)
    assert not result.done()
    release.set()
    assert await result == 'sent'


@run_async
async def test_old_tail_and_eos_do_not_interleave_new_sentence_on_real_link():
    from rui_dot2_bridge.frames import FRAME_SPEAKER, FRAME_SPEAKER_EOS

    adapter = make_adapter()
    link = DeviceLink(tls_port=None, plain_port=0, device_id='test')
    adapter._link = link
    adapter._prebuffer_start_s = .01
    adapter._eos_delay = lambda: .03
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []

    class Connection:
        async def send(self, frame):
            sent.append(frame[0])
            if len(sent) == 1:
                entered.set()
                await release.wait()

    link._data = Connection()
    token = adapter.begin_delivery()
    await adapter.play_delivery(token, bytes(1000), sample_rate=48000, channels=1)
    await asyncio.wait_for(entered.wait(), 1)
    adapter._prebuffer_bytes = 0
    new_play = asyncio.create_task(adapter.play_delivery(
        token, bytes(SPEAKER_CHUNK_BYTES * 2), sample_rate=48000, channels=1))
    await asyncio.sleep(.02)
    release.set()
    await asyncio.wait_for(new_play, 1)
    assert await adapter.finish_delivery(token, timeout_s=1) == 'sent'
    assert sent == [FRAME_SPEAKER, FRAME_SPEAKER_EOS,
                    FRAME_SPEAKER, FRAME_SPEAKER, FRAME_SPEAKER_EOS]


@run_async
async def test_finish_actively_drains_short_buffer_without_waiting_for_idle():
    adapter = make_adapter()
    adapter._prebuffer_start_s = 5
    adapter._eos_delay = lambda: 5
    token = adapter.begin_delivery()
    await adapter.play_delivery(token, bytes(1000), sample_rate=48000, channels=1)
    assert await adapter.finish_delivery(token, timeout_s=.5) == 'sent'
    assert len(adapter._link.sent) == 1
    assert adapter._link.eos == 1


@run_async
async def test_first_pcm_failure_is_unknown_not_safe_to_retry():
    adapter = make_adapter()
    adapter._prebuffer_bytes = 0
    adapter._link.send_pcm = lambda pcm: asyncio.sleep(0, result=False)
    token = adapter.begin_delivery()
    await adapter.play_delivery(token, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    assert await adapter.finish_delivery(token, timeout_s=1) == 'unknown'


@run_async
async def test_eos_failure_after_pcm_is_partial_not_safe_to_retry():
    adapter = make_adapter()
    adapter._prebuffer_bytes = 0
    adapter._link.end_stream = lambda: asyncio.sleep(0, result=False)
    token = adapter.begin_delivery()
    await adapter.play_delivery(token, bytes(SPEAKER_CHUNK_BYTES), sample_rate=48000, channels=1)
    assert await adapter.finish_delivery(token, timeout_s=1) == 'partial'


@run_async
async def test_link_loss_before_any_pcm_attempt_is_not_sent():
    adapter = make_adapter()
    adapter._prebuffer_start_s = 10
    token = adapter.begin_delivery()
    await adapter.play_delivery(token, bytes(1000), sample_rate=48000, channels=1)
    adapter._on_link_lost('synthetic-disconnect')
    assert await adapter.finish_delivery(token, timeout_s=1) == 'not_sent'
    assert not adapter._link.sent


@run_async
async def test_finish_does_not_release_partial_buffer_until_active_write_finishes():
    from rui_dot2_bridge.frames import FRAME_SPEAKER, FRAME_SPEAKER_EOS

    adapter = make_adapter()
    link = DeviceLink(tls_port=None, plain_port=0, device_id='test')
    adapter._link = link
    adapter._prebuffer_bytes = 0
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []

    class Connection:
        async def send(self, frame):
            sent.append(frame[0])
            if len(sent) == 1:
                entered.set()
                await release.wait()

    link._data = Connection()
    token = adapter.begin_delivery()
    producer = asyncio.create_task(adapter.play_delivery(
        token, bytes(SPEAKER_CHUNK_BYTES + 1000), sample_rate=48000, channels=1))
    await asyncio.wait_for(entered.wait(), 1)
    finish = asyncio.create_task(adapter.finish_delivery(token, timeout_s=1))
    await asyncio.sleep(.02)
    assert not finish.done()
    release.set()
    await producer
    assert await finish == 'sent'
    assert sent == [FRAME_SPEAKER, FRAME_SPEAKER, FRAME_SPEAKER_EOS]


@run_async
async def test_evicted_receipt_has_unknown_history_not_safe_to_retry():
    adapter = make_adapter()
    token = adapter.begin_delivery()
    await adapter.play_delivery(token, bytes(1000), sample_rate=48000, channels=1)
    assert await adapter.finish_delivery(token, timeout_s=1) == 'sent'
    for _ in range(33):
        adapter.begin_delivery()
    assert await adapter.finish_delivery(token, timeout_s=1) == 'unknown'


@run_async
async def test_finish_empty_scope_is_definitely_not_sent():
    adapter = make_adapter()
    token = adapter.begin_delivery()
    assert await adapter.finish_delivery(token, timeout_s=1) == 'not_sent'
