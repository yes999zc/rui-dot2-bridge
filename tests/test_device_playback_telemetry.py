"""Device completion statistics retain their actual attribution and lifetime."""
import asyncio
import json

import pytest
from test_adapter import make_adapter

from rui_dot2_bridge.device_link import DeviceLink


def test_device_report_is_bounded_and_never_assigned_to_current_turn():
    async def scenario():
        link = DeviceLink(token='synthetic', tls_port=None, plain_port=None)
        await link._on_control_message(json.dumps({
            'type': 'playback_stats', 'periods': 120, 'underruns': 2,
            'stats': {'primeWaitMs': 64, 'maxGapMs': 7, 'minDepth': 0, 'bytesRecv': 4000,
                      'turn_id': 'spoofed', 'recvSpanMs': 'unknown'},
        }))
        report = link.device_playback_telemetry()
        assert report['periods'] == 120 and report['primeWaitMs'] == 64
        assert report['turn_correlated'] is False
        assert report['scope'] == 'last_completed_device_stream'
        assert report['report_age_ms'] >= 0
        assert 'turn_id' not in report and 'recvSpanMs' not in report
        link._clear_link_state()
        assert link.device_playback_telemetry() is None
    asyncio.run(scenario())


def test_malformed_report_cannot_invent_measurement():
    async def scenario():
        link = DeviceLink(token='synthetic', tls_port=None, plain_port=None)
        await link._on_control_message(json.dumps({
            'type': 'playback_stats', 'stats': {'primeWaitMs': -1, 'periods': True,
                                              'underruns': 10**15},
        }))
        assert link.device_playback_telemetry() is None
    asyncio.run(scenario())


@pytest.mark.parametrize('reported', [None, 0, 89])
def test_health_volume_and_loudness_are_observed_state(reported):
    adapter = make_adapter(device_managed_volume=True)
    adapter._link.volume_level = 127
    adapter._link.reported_level = reported
    health = adapter.health()
    assert health['volume_level'] == reported
    assert health['device_volume_level'] == reported
    assert health['loudness']['ladder']['corrected'] is True
    assert health['loudness']['limiter']['ceiling_db'] == -1
    assert health['device_playback_stats'] is None
