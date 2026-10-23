"""rui-side adapter: the EchoMuse device protocol as a ``DeviceAdapter``.

Phase 1 target (see ROADMAP.md). The shape below is the contract rui's audio
plane calls; every method marked TODO raises rather than pretending, so a
half-built adapter fails loudly in one place instead of streaming silence.

Design notes that are already settled:

* inbound transport — the device dials us, exactly like the LX06 adapter;
* mono 16 kHz uplink, no reference channel — capabilities must say so, because
  rui's plane gates AEC and barge-in on ``ref_channels >= 1``;
* playback arrives pre-resampled by rui when ``audio.playback.rate: 48000``,
  so this adapter does no DSP and no sample-rate conversion.
"""

from __future__ import annotations

import logging

from house_ear.device.contract import (
    AudioFrame,  # noqa: F401  (returned by frames() once implemented)
    Capability,
    DeviceAdapter,
    DeviceInfo,
    Transport,
)

from .frames import MIC_RATE, SPEAKER_RATE

log = logging.getLogger(__name__)

LISTEN_PORT = 8867        # plain ws, for a local probe only
TLS_PORT = 8870           # what the device dials; certs live in secrets/
WSS_SERVER_NAME = "echomuse-controller"  # the device's fixed TLS ServerName


class Dot2Adapter(DeviceAdapter):
    """Echo Dot 2 running EchoMuse firmware, driven by rui instead of EchoMuse."""

    def __init__(
        self,
        device_id: str,
        *,
        host: str,
        room: str = "",
        secrets_dir: str = "secrets",
        tls_port: int = TLS_PORT,
        allow_any_token: bool = False,
    ) -> None:
        self.device_id = device_id
        self.host = host
        self.secrets_dir = secrets_dir
        self.tls_port = tls_port
        self.allow_any_token = allow_any_token
        self.token: str | None = None
        self._open = False
        # Deliberately honest: the firmware hands us neither the mic array nor a
        # reference channel. Advertising them would let rui plan barge-in it
        # cannot actually do (docs/adapter-contract.md, "Capabilities, not
        # promises").
        self.info = DeviceInfo(
            id=device_id,
            adapter="dot2",
            model="Echo Dot 2 (RS03QR / biscuit)",
            host=host,
            room=room,
            capabilities=frozenset({Capability.PLAYBACK, Capability.LED, Capability.VOLUME, Capability.KEYS}),
            transport=Transport.INBOUND,
            channels=1,
            sample_rate=MIC_RATE,
            bit_depth=16,
            mic_channels=1,
            ref_channels=0,
        )
        log.info("dot2 adapter for %s; speaker rate %d, mic rate %d",
                 device_id, SPEAKER_RATE, MIC_RATE)

    # ------------------------------------------------------------------ contract
    async def probe(self) -> DeviceInfo:
        return self.info

    async def open(self) -> None:
        """Serve wss, wait for ``register``, answer ``ack``, send ``mic_start``."""
        # TODO(P1): TLS context from secrets/{ca.pem,server.pem,server.key};
        #           accept /control and /data; check X-EM-Token; start the
        #           mic_start(lock_mic=False) stream state machine.
        raise NotImplementedError("Phase 1: open() — see ROADMAP.md")

    async def close(self) -> None:
        # TODO(P1): send mic_stop, close both sockets, stop the server.
        raise NotImplementedError("Phase 1: close()")

    def frames(self):  # -> AsyncIterator[AudioFrame]
        """Yield 20 ms mono AudioFrames from the device's 80 ms ``0x01`` chunks."""
        # TODO(P1): parse_data_frame -> rechunk_voice -> AudioFrame(
        #     channels=1, sample_rate=16000, reference_channels=0)
        raise NotImplementedError("Phase 1: frames()")

    async def play(self, pcm: bytes, *, sample_rate: int, channels: int) -> None:
        """Send ``0x02`` frames; rui already resampled to ``playback.rate``."""
        # TODO(P1): assert sample_rate == SPEAKER_RATE and channels == 1, then
        #           await send(split_playback(pcm)) in order, paced by the socket.
        raise NotImplementedError("Phase 1: play()")

    async def stop_playback(self, fade_ms: int = 120) -> None:
        """Make the device silent *now*: flush what it has buffered, not just stop sending."""
        # TODO(P1): send speaker_flush on /control (and 0x03 if a stream is open).
        raise NotImplementedError("Phase 1: stop_playback()")

    async def set_volume(self, volume: int) -> None:
        # TODO(P2): map rui's 0-100 onto the device's volume_set.
        raise NotImplementedError("Phase 2: set_volume()")

    async def set_led(self, effect: str, color: str = "", *, repaint: bool = False) -> None:
        # TODO(P2): map rui states (listening/thinking/speaking/error) onto leds/led_anim.
        raise NotImplementedError("Phase 2: set_led()")


def build(dev, cfg) -> Dot2Adapter:
    """rui registry entry point: ``register("dot2", "rui_dot2_bridge.adapter:build")``.

    ``dev`` is a ``house_ear.config.schema.DeviceConfig``; ``cfg`` is the whole
    ``Config`` (used later for capture/voice/playback overrides).
    """
    return Dot2Adapter(
        dev.id,
        host=dev.host,
        room=dev.room,
        secrets_dir=getattr(dev, "secrets_dir", "secrets"),
        tls_port=getattr(dev, "listen_port", None) or TLS_PORT,
    )
