# Changelog

## v0.1.0 — 2026-10-24

First runnable version: the Dot 2 is a rui voice satellite, with **no changes to
rui's code** and **no reflash of the device**.

### Added

- `rui_dot2_bridge.device_link` — an EchoMuse controller the device dials:
  TLS wss on 8870 with the transplanted controller certificate, `X-EM-Token`,
  `register`/`ack`, `mic_start(lock_mic=False)`, `0x01` capture frames,
  `0x02`/`0x03` playback, `speaker_flush`, volume/LED/button messages.
- `rui_dot2_bridge.adapter` — the rui `DeviceAdapter`: 80 ms mono blocks cut to
  20 ms `AudioFrame`s, playback in whole 4096-byte periods with an idle EOS,
  barge-in flush, the 12-LED ring, and a hard 30 % volume ceiling enforced at
  connect, on rui requests, and after the physical buttons.
- `src/sitecustomize.py` + `scripts/dev_rui.sh` — registers `dot2` inside rui's
  parent **and** audio-plane child via `PYTHONPATH`, so the rui checkout and
  image stay untouched.
- `standalone.py` (probe without rui), `inject.py`/`inject_cli.py` (test-only
  audio injection), `tools/fake_device.py` (drives the adapter without hardware).
- Two-channel capture support behind `DOT2_REF_CHANNELS=1`, ready for the P3
  firmware that hands over the de-aliased ch8 reference.
- P3 firmware patch (`patches/0001-ref-uplink-v2.19.0-ea.1.patch`), cross-built
  with the upstream NDK image (ARM, sha256 `876f63b4…`), not installed.
- Tooling: `scripts/check.sh`, `doctor.py`, `device_health.py`, `p2_trials.py`,
  `soak.py`, `soak_report.py`, `make_deploy_bundle.sh`, `build_p3_firmware.sh`.
- Tests: 34 unit/integration tests (frame format, handshake, volume cap, LED,
  layout, EOS/flush), ruff clean.

### Measured on the hardware

- register/ack, `listen_state=stream`, 12.5 mic frames/s (80 ms), RTT 5–8 ms;
- TTS reply played with `periods=131 underruns=0`; text turn ~2 s to first audio;
- acoustic loop at 30 % volume: recorded mic → rui ASR returns the wake word and
  command correctly; the live plane logged `wake: 你好小瑞` on the matched voice;
- adapter stop → device on the old controller in 40 s; restore → back in 45 s;
- plane `kill -9` → watchdog rebuild in 3 s → device re-registered in 3 s.

### Known limits

- P2 wake/ barge-in rates need a human (the tooling is ready);
- rui has no AEC on a mono stream without a reference, so barge-in is off in
  v0.1.0 by design; P3 lifts that if the patch is accepted;
- the old EchoMuse controller pushes volume 0.669 (85/127) when it takes over —
  it stays stopped for unattended runs.
