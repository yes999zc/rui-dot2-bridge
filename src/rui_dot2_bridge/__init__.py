"""Echo Dot 2 as a rui satellite.

The device runs the EchoMuse firmware (already provisioned); this package is the
rui-side ``DeviceAdapter`` that speaks the EchoMuse device protocol in place of
the EchoMuse controller. See ``docs/adr-0001-adapter-as-controller.md``.
"""

__all__ = ["SAMPLE_RATE_MIC", "SAMPLE_RATE_SPEAKER"]

SAMPLE_RATE_MIC = 16000      # uplink: 0x01 frames, S16LE mono
SAMPLE_RATE_SPEAKER = 48000  # downlink: 0x02 frames, S16LE mono
