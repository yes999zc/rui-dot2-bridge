"""Opt-in array AEC requires validated owner, layout and current stream evidence."""
import asyncio
import json
from functools import wraps

import pytest
import websockets.asyncio.client as ws_client
from rui.config import Config, DeviceConfig
from websockets.exceptions import ConnectionClosed

from rui_dot2_bridge.adapter import build
from rui_dot2_bridge.device_link import DeviceLink


def run_async(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return wrapped


def config(*, params=None, aec=None, capture=None):
    params = {'array_aec_v1': True, 'aec_enabled': True, **(params or {})}
    dev = DeviceConfig(id='dot2', host='127.0.0.1', adapter='dot2', params=params, audio={
        'aec': aec or {'enabled': False, 'who': 'device'},
        'capture': capture or {'rate': 16000, 'channels': 2, 'mic_channels': 1,
                               'ref_channels': 1, 'bits': 16},
    })
    return dev, Config(devices=[dev])


def test_opt_in_validates_real_effective_configuration():
    dev, cfg = config()
    adapter = build(dev, cfg)
    assert adapter.channels == 2 and adapter.ref_channels == 1
    assert adapter._link_kwargs['array_aec_v1'] is True
    with pytest.raises(ValueError):
        build(dev, None)
    for owner in ({'enabled': True, 'who': 'controller'},
                  {'enabled': True, 'who': 'both'},
                  {'enabled': False, 'who': 'controller'}):
        dev, cfg = config(aec=owner)
        with pytest.raises(ValueError):
            build(dev, cfg)
    for params in ({'aec_enabled': False}, {'push_config': False}, {'lock_mic': True}):
        dev, cfg = config(params=params)
        with pytest.raises(ValueError):
            build(dev, cfg)
    for capture in ({'rate': 16000, 'channels': 1, 'mic_channels': 1, 'ref_channels': 0, 'bits': 16},
                    {'rate': 48000, 'channels': 2, 'mic_channels': 1, 'ref_channels': 1, 'bits': 16},
                    {'rate': 16000, 'channels': 2, 'mic_channels': 1, 'ref_channels': 1, 'bits': 32}):
        dev, cfg = config(capture=capture)
        with pytest.raises(ValueError):
            build(dev, cfg)


@run_async
async def test_handshake_needs_both_capabilities_and_gates_current_bound_pcm():
    link = DeviceLink(tls_port=None, plain_port=0, token='fixture', array_aec_v1=True,
                      aec_enabled=True, ref_uplink=True, push_config=True)
    await link.start()
    port = link.bound_ports['plain']
    headers = {'X-EM-Token': 'fixture'}
    try:
        for capabilities in (['ref_uplink'], ['array_aec_v1'], []):
            async with ws_client.connect(f'ws://127.0.0.1:{port}/control', additional_headers=headers) as ctl:
                await ctl.send(json.dumps({'type':'register','capabilities':capabilities}))
                with pytest.raises(ConnectionClosed):
                    await asyncio.wait_for(ctl.recv(), 1)
        async with ws_client.connect(f'ws://127.0.0.1:{port}/control', additional_headers=headers) as ctl:
            await ctl.send(json.dumps({'type':'register','capabilities':['ref_uplink','array_aec_v1']}))
            msgs = [json.loads(await asyncio.wait_for(ctl.recv(), 1)) for _ in range(4)]
            assert [m['type'] for m in msgs] == ['ack','volume_set','config','mic_start']
            assert {'dot2_hw_ref','dot2_array_aec_v1'} <= set(msgs[0]['features'])
            epoch = msgs[2]['audioPathEpoch']
            async with ws_client.connect(f'ws://127.0.0.1:{port}/data', additional_headers=headers) as data:
                pcm = bytes([1,0,1]) + bytes(5120)
                await data.send(pcm)
                await asyncio.sleep(.03)
                assert link.audio.empty()
                await data.send(json.dumps({'type':'audio_path_bind','version':1,'epoch':epoch,'stream_epoch':1}))
                await ctl.send(json.dumps({'type':'audio_path','version':1,'epoch':epoch,
                    'stream_epoch':1,'profile':'array_aec_v1','aec_owner':'device','aec_enabled':True,
                    'fusion':'seven_mic_crossover','channels':2,'mic_channels':1,'ref_channels':1,
                    'sample_rate':16000,'format':'s16le','tail_ms':256,'reference':'unknown',
                    'aec_state':'bypass'}))
                await asyncio.sleep(.03)
                await data.send(pcm)
                assert len(await asyncio.wait_for(link.audio.get(), 1)) == 5120
                await data.send(bytes([1,0,2]) + bytes(2560))
                await asyncio.sleep(.03)
                assert not link.audio_path_ready and link.audio.empty()
    finally:
        await link.stop()


@run_async
async def test_epoch_stream_reconnect_and_degraded_status_are_fail_closed():
    link = DeviceLink(tls_port=None, plain_port=0, token='fixture', array_aec_v1=True,
                      aec_enabled=True, ref_uplink=True, push_config=True)
    await link.start()
    port, headers = link.bound_ports['plain'], {'X-EM-Token': 'fixture'}

    async def settle():
        await asyncio.sleep(.025)

    def status(epoch, stream, **changes):
        return {'type': 'audio_path', 'version': 1, 'epoch': epoch, 'stream_epoch': stream,
                'profile': 'array_aec_v1', 'aec_owner': 'device', 'aec_enabled': True,
                'fusion': 'seven_mic_crossover', 'channels': 2, 'mic_channels': 1,
                'ref_channels': 1, 'sample_rate': 16000, 'format': 's16le', 'tail_ms': 256,
                'reference': 'unknown', 'aec_state': 'bypass', **changes}

    def bind(epoch, stream):
        return json.dumps({'type':'audio_path_bind','version':1,'epoch':epoch,'stream_epoch':stream})

    try:
        async with ws_client.connect(f'ws://127.0.0.1:{port}/control', additional_headers=headers) as ctl:
            await ctl.send(json.dumps({'type':'register','capabilities':['ref_uplink','array_aec_v1']}))
            messages = [json.loads(await ctl.recv()) for _ in range(4)]
            epoch = messages[2]['audioPathEpoch']
            # Status can arrive before the data socket and is not enough alone.
            await ctl.send(json.dumps(status(epoch, 1)))
            await settle()
            assert not link.audio_path_ready
            async with ws_client.connect(f'ws://127.0.0.1:{port}/data', additional_headers=headers) as data:
                await data.send(json.dumps({'type':'identify','device_id':'dot2'}))
                await data.send(bind(epoch, 1))
                await settle()
                assert link.audio_path_ready and link._audio_path_status['aec_state'] == 'bypass'
                for invalid in ({'aec_owner':'controller'}, {'aec_enabled':False},
                                {'fusion':'mono'}, {'channels':1}, {'format':'s32le'},
                                {'sample_rate':48000}, {'tail_ms':64}, {'version':True}):
                    await ctl.send(json.dumps(status(epoch, 1, **invalid)))
                    await settle()
                    assert not link.audio_path_ready
                    await ctl.send(json.dumps(status(epoch, 1)))
                    await settle()
                    assert link.audio_path_ready
                await ctl.send(json.dumps(status(epoch, 1, reference='absent', aec_state='active')))
                await settle()
                assert not link.audio_path_ready
                await ctl.send(json.dumps(status(epoch, 1, reference='absent', aec_state='degraded')))
                await settle()
                assert link.audio_path_ready and link._audio_path_status['aec_state'] == 'degraded'
                await ctl.send(json.dumps(status(epoch, 1, aec_state='stopped')))
                await ctl.send(json.dumps(status(epoch, 1)))
                await settle()
                assert not link.audio_path_ready
                await data.send(bind(epoch, 2))
                await ctl.send(json.dumps(status('old-nonce', 2)))
                await settle()
                assert not link.audio_path_ready
                await ctl.send(json.dumps(status(epoch, 2, reference='present', aec_state='active')))
                await settle()
                assert link.audio_path_ready
                await data.send(bytes([1,0,1]) + bytes(5120))
                await settle()
                assert not link.audio.empty()
            await settle()
            assert not link.audio_path_ready and link.audio.empty()
            # An old report and old data bind after reconnect cannot reopen the gate.
            async with ws_client.connect(f'ws://127.0.0.1:{port}/data', additional_headers=headers) as data:
                await data.send(bind(epoch, 2))
                await ctl.send(json.dumps(status(epoch, 2)))
                await settle()
                assert not link.audio_path_ready
                await ctl.send(json.dumps(status(epoch, 3)))
                await data.send(bind(epoch, 3))
                await settle()
                assert link.audio_path_ready
    finally:
        await link.stop()


def test_candidate_off_ignores_missing_global_config():
    dev = DeviceConfig(id='dot2', host='127.0.0.1', adapter='dot2', params={})
    adapter = build(dev, None)
    assert adapter._link_kwargs['array_aec_v1'] is False


def test_direct_constructor_cannot_bypass_rui_owner_validation():
    from rui_dot2_bridge.adapter import Dot2Adapter
    with pytest.raises(ValueError):
        Dot2Adapter('dot2', host='127.0.0.1', aec_enabled=True, mic_channels=1,
                    ref_channels=1, array_aec_v1=True)


def test_capture_shift_and_unverified_optin_values_are_rejected():
    dev, cfg = config(capture={'rate': 16000, 'channels': 2, 'mic_channels': 1,
                               'ref_channels': 1, 'bits': 16, 'mic_shift': 3})
    with pytest.raises(ValueError):
        build(dev, cfg)
    dev, cfg = config(params={'array_aec_v1': 'true'})
    with pytest.raises(ValueError):
        build(dev, cfg)
    dev, cfg = config(params={'aec_tail_ms': 128})
    with pytest.raises(ValueError):
        build(dev, cfg)


@run_async
async def test_status_and_data_from_previous_control_cannot_open_new_control_epoch():
    link = DeviceLink(tls_port=None, plain_port=0, token='fixture', array_aec_v1=True,
                      aec_enabled=True, ref_uplink=True, push_config=True)
    await link.start()
    port, headers = link.bound_ports['plain'], {'X-EM-Token': 'fixture'}
    def path(epoch):
        return {'type':'audio_path','version':1,'epoch':epoch,'stream_epoch':1,
                'profile':'array_aec_v1','aec_owner':'device','aec_enabled':True,
                'fusion':'seven_mic_crossover','channels':2,'mic_channels':1,'ref_channels':1,
                'sample_rate':16000,'format':'s16le','tail_ms':256,
                'reference':'unknown','aec_state':'bypass'}
    def bind(epoch):
        return {'type':'audio_path_bind','version':1,'epoch':epoch,'stream_epoch':1}
    async def register(ctl):
        await ctl.send(json.dumps({'type':'register','capabilities':['ref_uplink','array_aec_v1']}))
        messages = [json.loads(await ctl.recv()) for _ in range(4)]
        return messages[2]['audioPathEpoch']
    try:
        async with ws_client.connect(f'ws://127.0.0.1:{port}/control', additional_headers=headers) as old:
            first = await register(old)
            async with ws_client.connect(f'ws://127.0.0.1:{port}/data', additional_headers=headers) as data:
                await data.send(json.dumps(bind(first)))
                await old.send(json.dumps(path(first)))
                await asyncio.sleep(.03)
                assert link.audio_path_ready
                async with ws_client.connect(f'ws://127.0.0.1:{port}/control', additional_headers=headers) as new:
                    second = await register(new)
                    assert second != first and not link.audio_path_ready
                    await data.send(json.dumps(bind(first)))
                    await new.send(json.dumps(path(first)))
                    await asyncio.sleep(.03)
                    assert not link.audio_path_ready
                    await data.send(json.dumps(bind(second)))
                    await new.send(json.dumps(path(second)))
                    await asyncio.sleep(.03)
                    assert link.audio_path_ready
    finally:
        await link.stop()


@pytest.mark.parametrize('ref', ['v0.8.15', 'v0.8.17'])
def test_real_baseline_effective_config_resolver(ref, tmp_path, monkeypatch):
    import importlib.util
    import os
    import subprocess
    import sys
    from pathlib import Path

    default = Path(__file__).resolve().parents[3] / '01_Code/projects/house-ear'
    upstream = os.environ.get('RUI_HOUSE_EAR_REPO', str(default))
    if not (Path(upstream) / '.git').exists():
        pytest.skip('set RUI_HOUSE_EAR_REPO to the upstream Git checkout')
    source = subprocess.check_output(['git', '-C', upstream, 'show',
                                      f'{ref}:src/rui/config/schema.py'], text=True)
    filename = tmp_path / 'schema.py'
    filename.write_text(source)
    spec = importlib.util.spec_from_file_location('rui.config.schema', filename)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, 'rui.config.schema', module)
    spec.loader.exec_module(module)
    dev = module.DeviceConfig(id='dot2', host='127.0.0.1', adapter='dot2',
        params={'array_aec_v1': True, 'aec_enabled': True}, audio={
            'aec': {'enabled': False, 'who': 'device'},
            'capture': {'rate': 16000, 'bits': 16, 'channels': 2,
                        'mic_channels': 1, 'ref_channels': 1}})
    cfg = module.Config(devices=[dev])
    assert build(dev, cfg).channels == 2
    wrong = dev.model_copy(update={'audio': dev.audio.model_copy(update={
        'aec': dev.audio.aec.model_copy(update={'enabled': True, 'who': 'both'})})})
    with pytest.raises(ValueError):
        build(wrong, module.Config(devices=[wrong]))


def _ready_link():
    link = DeviceLink(tls_port=None, plain_port=None, array_aec_v1=True,
                      aec_enabled=True, ref_uplink=True, push_config=True)
    link._control, link._data = object(), object()
    link._audio_path_epoch = 'current'
    link._accept_audio_bind({'type':'audio_path_bind','version':1,'epoch':'current','stream_epoch':1})
    link._accept_audio_path({'type':'audio_path','version':1,'epoch':'current','stream_epoch':1,
        'profile':'array_aec_v1','aec_owner':'device','aec_enabled':True,
        'fusion':'seven_mic_crossover','channels':2,'mic_channels':1,'ref_channels':1,
        'sample_rate':16000,'format':'s16le','tail_ms':256,'reference':'unknown',
        'aec_state':'bypass'})
    assert link.audio_path_ready
    return link


@run_async
async def test_dequeued_pcm_is_not_yielded_after_control_revocation():
    link = _ready_link()
    link._enqueue_audio(bytes(5120))
    original = link.audio.get
    async def revoke_after_get():
        result = await original()
        link._reset_audio_path(new_control=True)
        return result
    link.audio.get = revoke_after_get
    frames = link.frames()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(frames), .05)
    await frames.aclose()


@run_async
async def test_partial_rechunk_does_not_yield_old_stream_after_revoke():
    dev, cfg = config()
    adapter = build(dev, cfg)
    link = _ready_link()
    adapter._link = link
    link._enqueue_audio(bytes(5120))
    frames = adapter.frames()
    first = await asyncio.wait_for(anext(frames), .1)
    assert first.duration_ms == 20
    link._reset_audio_path(new_control=True)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(frames), .05)
    await frames.aclose()


def test_input_stopped_does_not_report_speaker_link_loss():
    import time
    dev, cfg = config()
    adapter = build(dev, cfg)
    link = _ready_link()
    adapter._link = link
    link.stats.data_connects = 1
    link.stats.last_pcm_sent_at = time.monotonic()
    link.connected.set()
    assert adapter.health()['connected'] is True
    assert adapter.audio_path_contract()['epoch'] == 'current'
    assert 'current' not in json.dumps(adapter.health())
    link._set_muted(True)
    status = dict(link._audio_path_status, aec_state='stopped')
    link._accept_audio_path(status)
    assert not link.audio_path_ready
    assert adapter.health()['connected'] is True


@run_async
async def test_old_rechunk_is_not_relabelled_after_new_epoch_becomes_ready():
    dev, cfg = config()
    adapter = build(dev, cfg)
    link = _ready_link()
    adapter._link = link
    link._enqueue_audio(bytes(5120))
    frames = adapter.frames()
    assert (await asyncio.wait_for(anext(frames), .1)).duration_ms == 20
    link._reset_audio_path(new_control=True)
    link._audio_path_epoch = 'new-control'
    link._accept_audio_bind({'type':'audio_path_bind','version':1,'epoch':'new-control','stream_epoch':1})
    link._accept_audio_path({'type':'audio_path','version':1,'epoch':'new-control','stream_epoch':1,
        'profile':'array_aec_v1','aec_owner':'device','aec_enabled':True,
        'fusion':'seven_mic_crossover','channels':2,'mic_channels':1,'ref_channels':1,
        'sample_rate':16000,'format':'s16le','tail_ms':256,'reference':'unknown',
        'aec_state':'bypass'})
    assert link.audio_path_ready
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(frames), .05)
    await frames.aclose()


@pytest.mark.parametrize('tail', [64.0, 256.0, True, False])
def test_array_tail_requires_integer_wire_value(tail):
    dev, cfg = config(params={'aec_tail_ms': tail})
    with pytest.raises(ValueError):
        build(dev, cfg)
    with pytest.raises(ValueError):
        DeviceLink(tls_port=None, plain_port=None, array_aec_v1=True,
                   aec_enabled=True, ref_uplink=True, push_config=True, array_aec_tail_ms=tail)


@run_async
async def test_status_ahead_of_bind_survives_old_stream_stopped_message():
    link = DeviceLink(tls_port=None, plain_port=0, token='fixture', array_aec_v1=True,
                      aec_enabled=True, ref_uplink=True, push_config=True)
    await link.start()
    port, headers = link.bound_ports['plain'], {'X-EM-Token':'fixture'}
    def status(epoch, stream, **changes):
        return {'type':'audio_path','version':1,'epoch':epoch,'stream_epoch':stream,
            'profile':'array_aec_v1','aec_owner':'device','aec_enabled':True,
            'fusion':'seven_mic_crossover','channels':2,'mic_channels':1,'ref_channels':1,
            'sample_rate':16000,'format':'s16le','tail_ms':256,'reference':'unknown',
            'aec_state':'bypass', **changes}
    def bind(epoch, stream):
        return {'type':'audio_path_bind','version':1,'epoch':epoch,'stream_epoch':stream}
    try:
        async with ws_client.connect(f'ws://127.0.0.1:{port}/control', additional_headers=headers) as ctl:
            await ctl.send(json.dumps({'type':'register','capabilities':['ref_uplink','array_aec_v1']}))
            msgs = [json.loads(await ctl.recv()) for _ in range(4)]
            epoch = msgs[2]['audioPathEpoch']
            async with ws_client.connect(f'ws://127.0.0.1:{port}/data', additional_headers=headers) as data:
                await data.send(json.dumps(bind(epoch,1)))
                await ctl.send(json.dumps(status(epoch,1)))
                await asyncio.sleep(.03)
                assert link.audio_path_ready
                await ctl.send(json.dumps(status(epoch,2)))
                await ctl.send(json.dumps(status(epoch,1,aec_state='stopped')))
                await asyncio.sleep(.03)
                await data.send(json.dumps(bind(epoch,2)))
                await asyncio.sleep(.03)
                assert link.audio_path_ready and link._audio_path_status['stream_epoch'] == 2
    finally:
        await link.stop()


@run_async
async def test_bad_pcm_length_forces_data_reconnect_then_new_stream_recovers():
    link = DeviceLink(tls_port=None, plain_port=0, token='fixture', array_aec_v1=True,
                      aec_enabled=True, ref_uplink=True, push_config=True)
    await link.start()
    port, headers = link.bound_ports['plain'], {'X-EM-Token':'fixture'}
    def status(epoch, stream):
        return {'type':'audio_path','version':1,'epoch':epoch,'stream_epoch':stream,
            'profile':'array_aec_v1','aec_owner':'device','aec_enabled':True,
            'fusion':'seven_mic_crossover','channels':2,'mic_channels':1,'ref_channels':1,
            'sample_rate':16000,'format':'s16le','tail_ms':256,'reference':'unknown',
            'aec_state':'bypass'}
    def bind(epoch, stream):
        return {'type':'audio_path_bind','version':1,'epoch':epoch,'stream_epoch':stream}
    try:
        async with ws_client.connect(f'ws://127.0.0.1:{port}/control', additional_headers=headers) as ctl:
            await ctl.send(json.dumps({'type':'register','capabilities':['ref_uplink','array_aec_v1']}))
            msgs = [json.loads(await ctl.recv()) for _ in range(4)]
            epoch = msgs[2]['audioPathEpoch']
            async with ws_client.connect(f'ws://127.0.0.1:{port}/data', additional_headers=headers) as data:
                await data.send(json.dumps(bind(epoch,1)))
                await ctl.send(json.dumps(status(epoch,1)))
                await asyncio.sleep(.03)
                assert link.audio_path_ready
                await data.send(bytes([1,0,1]) + bytes(2560))
                with pytest.raises(ConnectionClosed):
                    await asyncio.wait_for(data.recv(), 1)
                assert not link.audio_path_ready
            async with ws_client.connect(f'ws://127.0.0.1:{port}/data', additional_headers=headers) as data:
                await data.send(json.dumps(bind(epoch,2)))
                await ctl.send(json.dumps(status(epoch,2)))
                await asyncio.sleep(.03)
                assert link.audio_path_ready
                await data.send(bytes([1,0,2]) + bytes(5120))
                assert len(await asyncio.wait_for(link.audio.get(), 1)) == 5120
    finally:
        await link.stop()


@run_async
async def test_unverified_pcm_is_bounded_but_silent_and_muted_do_not_reconnect(monkeypatch):
    import rui_dot2_bridge.device_link as device_link
    monkeypatch.setattr(device_link, 'CANDIDATE_NEGOTIATION_TIMEOUT_S', .06, raising=False)
    link = DeviceLink(tls_port=None, plain_port=0, token='fixture', array_aec_v1=True,
                      aec_enabled=True, ref_uplink=True, push_config=True)
    await link.start()
    port, headers = link.bound_ports['plain'], {'X-EM-Token':'fixture'}
    try:
        async with ws_client.connect(f'ws://127.0.0.1:{port}/control', additional_headers=headers) as ctl:
            await ctl.send(json.dumps({'type':'register','capabilities':['ref_uplink','array_aec_v1']}))
            for _ in range(4):
                await ctl.recv()
            async with ws_client.connect(f'ws://127.0.0.1:{port}/data', additional_headers=headers) as data:
                # Idle without PCM must not trigger the negotiation watchdog.
                await asyncio.sleep(.09)
                assert link.data_plane_up
                await ctl.send(json.dumps({'type':'mute_state','muted':True}))
                await asyncio.sleep(.02)
                pcm = bytes([1,0,1]) + bytes(5120)
                for _ in range(5):
                    await data.send(pcm)
                    await asyncio.sleep(.02)
                assert link.data_plane_up
                await ctl.send(json.dumps({'type':'mute_state','muted':False}))
                await asyncio.sleep(.02)
                for _ in range(5):
                    try:
                        await data.send(pcm)
                    except ConnectionClosed:
                        break
                    await asyncio.sleep(.02)
                with pytest.raises(ConnectionClosed):
                    await asyncio.wait_for(data.recv(), .5)
                assert not link.audio_path_ready and link.audio.empty()
    finally:
        await link.stop()


@run_async
async def test_valid_contract_arriving_before_deadline_cancels_negotiation_timer(monkeypatch):
    import rui_dot2_bridge.device_link as device_link
    monkeypatch.setattr(device_link, 'CANDIDATE_NEGOTIATION_TIMEOUT_S', .12, raising=False)
    link = DeviceLink(tls_port=None, plain_port=0, token='fixture', array_aec_v1=True,
                      aec_enabled=True, ref_uplink=True, push_config=True)
    await link.start()
    port, headers = link.bound_ports['plain'], {'X-EM-Token':'fixture'}
    try:
        async with ws_client.connect(f'ws://127.0.0.1:{port}/control', additional_headers=headers) as ctl:
            await ctl.send(json.dumps({'type':'register','capabilities':['ref_uplink','array_aec_v1']}))
            msgs = [json.loads(await ctl.recv()) for _ in range(4)]
            epoch = msgs[2]['audioPathEpoch']
            async with ws_client.connect(f'ws://127.0.0.1:{port}/data', additional_headers=headers) as data:
                pcm = bytes([1,0,1]) + bytes(5120)
                await data.send(pcm)
                await asyncio.sleep(.04)
                await data.send(json.dumps({'type':'audio_path_bind','version':1,
                                            'epoch':epoch,'stream_epoch':1}))
                await ctl.send(json.dumps({'type':'audio_path','version':1,'epoch':epoch,'stream_epoch':1,
                    'profile':'array_aec_v1','aec_owner':'device','aec_enabled':True,
                    'fusion':'seven_mic_crossover','channels':2,'mic_channels':1,'ref_channels':1,
                    'sample_rate':16000,'format':'s16le','tail_ms':256,'reference':'unknown',
                    'aec_state':'bypass'}))
                await asyncio.sleep(.03)
                assert link.audio_path_ready
                await asyncio.sleep(.14)
                await data.send(pcm)
                assert len(await asyncio.wait_for(link.audio.get(), .5)) == 5120
                assert link.data_plane_up
    finally:
        await link.stop()


@run_async
async def test_repeated_candidate_failures_latch_input_but_keep_playback(monkeypatch):
    import rui_dot2_bridge.device_link as device_link
    monkeypatch.setattr(device_link, 'CANDIDATE_NEGOTIATION_TIMEOUT_S', .04, raising=False)
    monkeypatch.setattr(device_link, 'CANDIDATE_FAILURE_LIMIT', 2, raising=False)
    link = DeviceLink(tls_port=None, plain_port=0, token='fixture', array_aec_v1=True,
                      aec_enabled=True, ref_uplink=True, push_config=True)
    await link.start()
    port, headers = link.bound_ports['plain'], {'X-EM-Token':'fixture'}
    def bind(epoch, stream):
        return {'type':'audio_path_bind','version':1,'epoch':epoch,'stream_epoch':stream}
    def status(epoch, stream):
        return {'type':'audio_path','version':1,'epoch':epoch,'stream_epoch':stream,
            'profile':'array_aec_v1','aec_owner':'device','aec_enabled':True,
            'fusion':'seven_mic_crossover','channels':2,'mic_channels':1,'ref_channels':1,
            'sample_rate':16000,'format':'s16le','tail_ms':256,'reference':'unknown',
            'aec_state':'bypass'}
    async def trigger(data):
        for _ in range(6):
            try:
                await data.send(bytes([1,0,1]) + bytes(5120))
            except ConnectionClosed:
                return
            await asyncio.sleep(.02)
    try:
        async with ws_client.connect(f'ws://127.0.0.1:{port}/control', additional_headers=headers) as ctl:
            await ctl.send(json.dumps({'type':'register','capabilities':['ref_uplink','array_aec_v1']}))
            msgs = [json.loads(await ctl.recv()) for _ in range(4)]
            epoch = msgs[2]['audioPathEpoch']
            async with ws_client.connect(f'ws://127.0.0.1:{port}/data', additional_headers=headers) as first:
                await trigger(first)
                with pytest.raises(ConnectionClosed):
                    await asyncio.wait_for(first.recv(), .5)
            async with ws_client.connect(f'ws://127.0.0.1:{port}/data', additional_headers=headers) as data:
                await trigger(data)
                assert link.audio_path_blocked and link.audio_path_failures == 2
                assert link.data_plane_up and link.connected.is_set()
                dev, cfg = config()
                adapter = build(dev, cfg)
                adapter._link = link
                assert adapter.health()['connected'] is True
                assert adapter.health()['audio_path']['blocked'] is True
                assert await link.send_pcm(bytes(4096)) is True
                assert (await asyncio.wait_for(data.recv(), .5))[0] == 2
                await data.send(json.dumps(bind(epoch,1)))
                await ctl.send(json.dumps(status(epoch,1)))
                await asyncio.sleep(.03)
                assert not link.audio_path_ready
                link.reset_audio_path_failures()
                await data.send(json.dumps(bind(epoch,2)))
                await ctl.send(json.dumps(status(epoch,2)))
                await asyncio.sleep(.03)
                assert link.audio_path_ready
                assert not link.audio_path_blocked and link.audio_path_failures == 0
    finally:
        await link.stop()


@run_async
async def test_repeated_ready_then_bad_pcm_still_latches(monkeypatch):
    import rui_dot2_bridge.device_link as device_link
    monkeypatch.setattr(device_link, 'CANDIDATE_FAILURE_LIMIT', 2, raising=False)
    link = DeviceLink(tls_port=None, plain_port=0, token='fixture', array_aec_v1=True,
                      aec_enabled=True, ref_uplink=True, push_config=True)
    await link.start()
    port, headers = link.bound_ports['plain'], {'X-EM-Token':'fixture'}
    def bind(epoch, n):
        return {'type':'audio_path_bind','version':1,'epoch':epoch,'stream_epoch':n}
    def status(epoch, n):
        return {'type':'audio_path','version':1,'epoch':epoch,'stream_epoch':n,
            'profile':'array_aec_v1','aec_owner':'device','aec_enabled':True,
            'fusion':'seven_mic_crossover','channels':2,'mic_channels':1,'ref_channels':1,
            'sample_rate':16000,'format':'s16le','tail_ms':256,'reference':'unknown',
            'aec_state':'bypass'}
    try:
        async with ws_client.connect(f'ws://127.0.0.1:{port}/control', additional_headers=headers) as ctl:
            await ctl.send(json.dumps({'type':'register','capabilities':['ref_uplink','array_aec_v1']}))
            msgs = [json.loads(await ctl.recv()) for _ in range(4)]
            epoch = msgs[2]['audioPathEpoch']
            for n in (1, 2):
                async with ws_client.connect(f'ws://127.0.0.1:{port}/data', additional_headers=headers) as data:
                    await data.send(json.dumps(bind(epoch,n)))
                    await ctl.send(json.dumps(status(epoch,n)))
                    await asyncio.sleep(.03)
                    assert link.audio_path_ready and link.audio_path_failures == n - 1
                    await data.send(bytes([1,0,1]) + bytes(2560))
                    await asyncio.sleep(.03)
                    assert link.audio_path_failures == n
                    if n == 1:
                        with pytest.raises(ConnectionClosed):
                            await asyncio.wait_for(data.recv(), .5)
                    else:
                        assert link.audio_path_blocked and link.data_plane_up
                        assert await link.send_pcm(bytes(4096))
                        assert (await asyncio.wait_for(data.recv(), .5))[0] == 2
    finally:
        await link.stop()


@run_async
async def test_ready_only_clears_failure_count_after_ten_valid_frames(monkeypatch):
    import rui_dot2_bridge.device_link as device_link
    monkeypatch.setattr(device_link, 'CANDIDATE_STABLE_FRAMES', 10, raising=False)
    monkeypatch.setattr(device_link, 'CANDIDATE_STABLE_S', .08, raising=False)
    link = DeviceLink(tls_port=None, plain_port=0, token='fixture', array_aec_v1=True,
                      aec_enabled=True, ref_uplink=True, push_config=True)
    await link.start()
    port, headers = link.bound_ports['plain'], {'X-EM-Token':'fixture'}
    try:
        async with ws_client.connect(f'ws://127.0.0.1:{port}/control', additional_headers=headers) as ctl:
            await ctl.send(json.dumps({'type':'register','capabilities':['ref_uplink','array_aec_v1']}))
            msgs = [json.loads(await ctl.recv()) for _ in range(4)]
            epoch = msgs[2]['audioPathEpoch']
            link.audio_path_failures = 1
            async with ws_client.connect(f'ws://127.0.0.1:{port}/data', additional_headers=headers) as data:
                await data.send(json.dumps({'type':'audio_path_bind','version':1,'epoch':epoch,'stream_epoch':1}))
                await ctl.send(json.dumps({'type':'audio_path','version':1,'epoch':epoch,'stream_epoch':1,
                    'profile':'array_aec_v1','aec_owner':'device','aec_enabled':True,
                    'fusion':'seven_mic_crossover','channels':2,'mic_channels':1,'ref_channels':1,
                    'sample_rate':16000,'format':'s16le','tail_ms':256,'reference':'unknown',
                    'aec_state':'bypass'}))
                await asyncio.sleep(.03)
                assert link.audio_path_ready and link.audio_path_failures == 1
                for _ in range(9):
                    await data.send(bytes([1,0,1]) + bytes(5120))
                await asyncio.sleep(.03)
                assert link.audio_path_failures == 1
                await data.send(bytes([1,0,2]) + bytes(5120))
                await asyncio.sleep(.03)
                assert link.audio_path_failures == 1
                await asyncio.sleep(.04)
                await data.send(bytes([1,0,3]) + bytes(5120))
                await asyncio.sleep(.03)
                assert link.audio_path_failures == 0
    finally:
        await link.stop()


@run_async
async def test_blocked_silent_input_does_not_trip_data_watchdog_or_speaker_health(monkeypatch):
    import time

    import rui_dot2_bridge.device_link as device_link
    monkeypatch.setattr(device_link, 'CANDIDATE_NEGOTIATION_TIMEOUT_S', .04, raising=False)
    monkeypatch.setattr(device_link, 'CANDIDATE_FAILURE_LIMIT', 1, raising=False)
    link = DeviceLink(tls_port=None, plain_port=0, token='fixture', array_aec_v1=True,
                      aec_enabled=True, ref_uplink=True, push_config=True)
    await link.start()
    port, headers = link.bound_ports['plain'], {'X-EM-Token':'fixture'}
    try:
        async with ws_client.connect(f'ws://127.0.0.1:{port}/control', additional_headers=headers) as ctl:
            await ctl.send(json.dumps({'type':'register','capabilities':['ref_uplink','array_aec_v1']}))
            for _ in range(4):
                await ctl.recv()
            async with ws_client.connect(f'ws://127.0.0.1:{port}/data', additional_headers=headers) as data:
                for _ in range(5):
                    await data.send(bytes([1,0,1]) + bytes(5120))
                    await asyncio.sleep(.02)
                assert link.audio_path_blocked
                data_socket, control_socket = link._data, link._control
                link._meters[id(data_socket)].last_rx_at = time.monotonic() - 120
                link._meters[id(control_socket)].last_rx_at = time.monotonic()
                link.stats.last_mic_at = link.stats.last_pcm_sent_at = 0
                link._enforce_data_silence()
                assert link._data is data_socket and link._control is control_socket
                assert link.stats.data_silence_trips == 0
                dev, cfg = config()
                adapter = build(dev, cfg)
                adapter._link = link
                assert adapter.health()['connected'] is True
                assert await link.send_pcm(bytes(4096))
                assert (await asyncio.wait_for(data.recv(), .5))[0] == 2
    finally:
        await link.stop()


@run_async
async def test_sparse_unverified_pcm_and_muted_pause_do_not_consume_retry(monkeypatch):
    import rui_dot2_bridge.device_link as device_link
    monkeypatch.setattr(device_link, 'CANDIDATE_NEGOTIATION_TIMEOUT_S', .06)
    monkeypatch.setattr(device_link, 'CANDIDATE_UNVERIFIED_MAX_GAP_S', .035, raising=False)
    link = DeviceLink(tls_port=None, plain_port=0, token='fixture', array_aec_v1=True,
                      aec_enabled=True, ref_uplink=True, push_config=True)
    await link.start()
    port, headers = link.bound_ports['plain'], {'X-EM-Token': 'fixture'}
    try:
        async with ws_client.connect(f'ws://127.0.0.1:{port}/control', additional_headers=headers) as ctl:
            await ctl.send(json.dumps({'type': 'register', 'capabilities': ['ref_uplink', 'array_aec_v1']}))
            for _ in range(4):
                await ctl.recv()
            async with ws_client.connect(f'ws://127.0.0.1:{port}/data', additional_headers=headers) as data:
                pcm = bytes([1, 0, 1]) + bytes(5120)
                await data.send(pcm)
                await asyncio.sleep(.10)
                await data.send(pcm)
                await asyncio.sleep(.01)
                assert link.audio_path_failures == 0 and link.data_plane_up
                await ctl.send(json.dumps({'type': 'mute_state', 'muted': True}))
                await asyncio.sleep(.02)
                await ctl.send(json.dumps({'type': 'mute_state', 'muted': False}))
                await asyncio.sleep(.02)
                await data.send(pcm)
                await asyncio.sleep(.01)
                assert link.audio_path_failures == 0 and link.data_plane_up
                for _ in range(8):
                    try:
                        await data.send(pcm)
                    except ConnectionClosed:
                        break
                    await asyncio.sleep(.02)
                with pytest.raises(ConnectionClosed):
                    await asyncio.wait_for(data.recv(), .5)
                assert link.audio_path_failures == 1
    finally:
        await link.stop()


@run_async
async def test_uplink_diagnostics_only_count_current_verified_aec_generation():
    link = DeviceLink(tls_port=None, plain_port=0, token='fixture', array_aec_v1=True,
                      aec_enabled=True, ref_uplink=True, push_config=True, playing=lambda: True)
    await link.start()
    port, headers = link.bound_ports['plain'], {'X-EM-Token': 'fixture'}
    try:
        async with ws_client.connect(f'ws://127.0.0.1:{port}/control', additional_headers=headers) as ctl:
            await ctl.send(json.dumps({'type': 'register', 'capabilities': ['ref_uplink', 'array_aec_v1']}))
            msgs = [json.loads(await asyncio.wait_for(ctl.recv(), 1)) for _ in range(4)]
            epoch = msgs[2]['audioPathEpoch']
            await ctl.send(json.dumps({'type': 'mute_state', 'muted': False}))
            await ctl.send(json.dumps({'type': 'listen_state', 'state': 'stream'}))
            async with ws_client.connect(f'ws://127.0.0.1:{port}/data', additional_headers=headers) as data:
                pcm = bytes([1, 0, 1]) + bytes(5120)
                await data.send(pcm)
                await asyncio.sleep(.03)
                assert not link.audio_path_ready and link.audio.empty()
                assert link.mic_uplink_status() == {'state': 'unknown', 'mic_age_ms': None}
                status = {'type': 'audio_path', 'version': 1, 'epoch': epoch, 'stream_epoch': 1,
                          'profile': 'array_aec_v1', 'aec_owner': 'device', 'aec_enabled': True,
                          'fusion': 'seven_mic_crossover', 'channels': 2, 'mic_channels': 1,
                          'ref_channels': 1, 'sample_rate': 16000, 'format': 's16le', 'tail_ms': 256,
                          'reference': 'unknown', 'aec_state': 'bypass'}
                await ctl.send(json.dumps(status))
                await data.send(json.dumps({'type': 'audio_path_bind', 'version': 1,
                                            'epoch': epoch, 'stream_epoch': 1}))
                for _ in range(100):
                    if link.audio_path_ready:
                        break
                    await asyncio.sleep(.01)
                assert link.audio_path_ready
                assert link.mic_uplink_status() == {'state': 'waiting_first_frame', 'mic_age_ms': None}
                await data.send(pcm)
                assert len(await asyncio.wait_for(link.audio.get(), 1)) == 5120
                assert link.mic_uplink_status()['state'] == 'recent_mic_frame'
                await ctl.send(json.dumps({**status, 'aec_state': 'stopped'}))
                for _ in range(100):
                    if not link.audio_path_ready:
                        break
                    await asyncio.sleep(.01)
                assert link.mic_uplink_status() == {'state': 'unknown', 'mic_age_ms': None}
                await ctl.send(json.dumps({**status, 'stream_epoch': 2}))
                await data.send(json.dumps({'type': 'audio_path_bind', 'version': 1,
                                            'epoch': epoch, 'stream_epoch': 2}))
                for _ in range(100):
                    if link.audio_path_ready:
                        break
                    await asyncio.sleep(.01)
                assert link.audio_path_ready
                assert link.mic_uplink_status() == {'state': 'waiting_first_frame', 'mic_age_ms': None}
                link.audio_path_blocked = True
                await data.send(pcm)
                await asyncio.sleep(.03)
                assert link.mic_uplink_status() == {'state': 'unknown', 'mic_age_ms': None}
                assert link.audio.empty()
                assert await link.send_pcm(bytes(4096))
                assert (await asyncio.wait_for(data.recv(), 1))[0] == 2
                link._enforce_data_silence()
                assert link.data_plane_up and link.control_plane_up
    finally:
        await link.stop()
