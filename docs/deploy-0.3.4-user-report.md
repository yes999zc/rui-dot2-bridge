# rui 0.2.2 → 0.3.4 upgrade on 192.168.5.34 — user-perspective report

- Date: 2026-10-25 (Asia/Shanghai)
- Operator: deploy-ops (acting as a **fresh user** following only upstream rui docs)
- Host: `yes999zc@192.168.5.34` / `horner-nas`, Debian 12 kernel 6.18.18, Docker 28.5.2, Compose v2.40.3, 8 vCPU / 6.7 GiB / 36.1 GiB free
- Upstream source of truth: `github.com/yes999zc/rui` tag **v0.3.4**, commit `1d0ccfecf3a274ec5ad2d6354e16c4c024f3e1db`
- Scope: vanilla 0.3.4 (no adapter/plugin, `devices: []`), `network_mode: host`, console `:8080`, port 4412 untouched, **192.168.5.168 never contacted**, rui source never modified, existing backups untouched

## TL;DR

| Item | Result |
|---|---|
| Image | `ghcr.io/yes999zc/rui:0.3.4` @ `sha256:e1cd2bede49edc90eae0085c83f7caee566ba2fa9acc094858b42b5c796c2aed` |
| Container | healthy, `User=1000:1000` (`rui`), `ReadOnly=true`, `CapDrop=[ALL]`, `no-new-privileges`, `/tmp` tmpfs 256m, host network |
| `/health` | `status=ok`, `version=0.3.4`, `config_version=2`, `config_version_supported=2`, `devices=0`, `audio_plane=false` |
| `rui doctor` | `verdict=degraded` — only the expected in-container `container_runtime: none` WARN; all other checks PASS |
| Console | `/ = 200`, `/setup = 200`; `rui-healthcheck` exit 0 |
| Config migration | **not needed** (on-disk config was already `version: 2`, schema v2); `config.yaml` byte-identical after upgrade |
| Secrets | all 9 original `.env` keys preserved value-for-value; only `RUI_IMAGE` changed, `RUI_UID/RUI_GID` added |
| 0.2.2 image | kept locally (`sha256:05ed982a…`) — rollback **verified working** |
| Logs | no traceback, no permission errors after the chown step |
| Duration | image pull 3.8 s (layers cached), container healthy ~6 s per recreate |

## 1. Material used (everything read is upstream, tag v0.3.4)

Requested set: `README.md`, `docs/deploy.md`, `deploy/install.sh`, `docker-compose.yml`, `config.example.yaml`, release notes. In addition I read only **upstream source** needed to explain observed behaviour: `docs/runbook.md` (linked from deploy.md), `Dockerfile`, `src/rui/config/store.py`, `src/rui/config/schema.py`, and the GitHub Releases feed.

No internal bridge doc, handoff or deploy report was opened. The v0.3.4 tree was cloned to `/tmp/rui-upstream-v0.3.4` (not into the bridge repo).

Release notes available upstream are one-liners only (GitHub Releases atom):

- v0.3.2 — "P3.5 security: SSRF guard, non-root container, CVE scanning"
- v0.3.3 — "console IA P0 …"
- v0.3.4 — "console Phase 2: merged devices/integrations/music/voice-ass…" (truncated by GitHub)

## 2. What upstream actually documents for an in-place upgrade

The upgrade path is spread over three files and is **not** written as one checklist:

- `README.md:251` — upgrade = pin `RUI_IMAGE=…` in `.env`, `docker compose pull && docker compose up -d`, wait healthy, `rui doctor`.
- `README.md:252` / `docs/deploy.md:515` — "升级到非 root 镜像（已有安装，一次性）": `scripts/backup.sh`, `sudo chown -R 1000:1000 config data`, `docker compose pull && docker compose up -d --force-recreate`, `rui-healthcheck`.
- `docs/deploy.md:513` (§3.6) and `docs/deploy.md:622` (§7) repeat the two bullets above; §7 additionally mentions the 0.3.x `USER=rui:1000` change and rollback to root images.
- `docs/runbook.md:106-134` (§5) is the most complete version, and is the only place that says old compose files keep working but **`read_only`/`tmpfs`/`cap_drop` hardening only applies if you merge the new compose manually** (`runbook.md:133-134`).
- `deploy/install.sh` is re-runnable and does the chown + `.env` `RUI_UID/RUI_GID` upsert itself; it **keeps an existing `docker-compose.yml`** (`install.sh:118-122`) and **keeps an existing `.env` except for UID/GID** (`install.sh:187-216`).
- There is **no** "0.2.2 → 0.3.4" section, no migration guide by version, and no CHANGELOG file in the repo. GitHub release notes for v0.3.4 are a single truncated line.

I followed the documented manual path (README/deploy.md bullets + runbook §5), not `install.sh` re-run (see friction #7 for why running it is still a trap).

## 3. Numbered command sequence and results

All commands run on `192.168.5.34` from `~/rui` unless noted. Secret values are omitted; `[…]` marks redaction. `TS=20261025-095711` is the snapshot timestamp used below.

### 3.0 Pre-flight (read-only)

```bash
ssh yes999zc@192.168.5.34
cd ~/rui
docker compose ps
docker inspect rui --format '{{.Config.User}} {{.Config.Image}} {{.State.Health.Status}}'
docker image inspect ghcr.io/yes999zc/rui:0.2.2 --format '{{index .RepoDigests 0}}'
curl -s localhost:8080/health
```

Result: `rui` / `ghcr.io/yes999zc/rui:0.2.2` / **healthy (11 h)**; `Config.User` empty (0.2.2 ran as **root**); 0.2.2 digest `sha256:05ed982a4cbe17e4dbedbca191989f43e0dd0ab93d63fa4f0bad83bc0991e738`; health `version=0.2.2 config_version=2 config_version_supported=2 devices=0 audio_plane=false`.

Baseline `rui doctor`: `verdict=degraded`, single `[WARN] container_runtime: none`, everything else PASS. Baseline listening ports: **8080 only** (no 4412).

### 3.1 Snapshot old compose/config + record rollback image

```bash
mkdir -p ~/rui-upgrade-$TS
cp -p docker-compose.yml config/config.yaml ~/rui-upgrade-$TS/
docker image inspect ghcr.io/yes999zc/rui:0.2.2 \
  --format '{{index .RepoDigests 0}}' > ~/rui-upgrade-$TS/image-0.2.2-digest.txt
cp -p config/config.yaml config/config.yaml.bak.upgrade.$TS
```

Result: old compose `sha256:d3c10c10…`, config `sha256:7a863828…`, 0.2.2 digest recorded.

### 3.2 Documented backup step — **failed as documented** (friction #3)

```bash
cd ~/rui && scripts/backup.sh --out ~/rui-backups
```

Result (exit non-zero, empty `~/rui-backups` left behind):

```text
cp: cannot open '/home/yes999zc/rui/config/./config.last-good.yaml' for reading: Permission denied
```

Cause: 0.2.2 ran as root and left 4 root-owned files (`config/config.last-good.yaml`, `data/ack.stamp`, `data/ack.wav`, `data/service.log`); the documented command is run as the login user. Re-ran with sudo:

```bash
sudo -n env HOME=/home/yes999zc scripts/backup.sh \
  --home /home/yes999zc/rui --out /home/yes999zc/rui-backups
# archive: /home/yes999zc/rui-backups/rui-backup-20261025-095717.tar.gz
# size: 88 KiB (10 files)  sha256: f50173f0f711ca2fd8e8f54cafc0d05a0e0280183ab14d12090c4daf8cba0a2c
# version: config=2 image=ghcr.io/yes999zc/rui:0.2.2 (0.2.2)
```

The archive itself came out root-owned `0600`, so it was also unreadable by the login user until `sudo chown -R yes999zc:Users ~/rui-backups` (friction #3).

### 3.3 One-time non-root ownership migration (documented)

```bash
find config data -not -user 1000 | wc -l     # 4
sudo chown -R 1000:1000 config data
find config data -not -user 1000 | wc -l     # 0
```

Result: 4 root-owned files → 0; group is now `1000(Administrators)` (this host maps gid 1000 to Administrators, and `yes999zc` is a member).

### 3.4 Pin the new version in `.env` (documented)

```bash
cp -p .env .env.bak.$TS
sed -i 's|^RUI_IMAGE=.*|RUI_IMAGE=ghcr.io/yes999zc/rui:0.3.4|' .env
grep -qx 'RUI_UID=1000' .env || echo 'RUI_UID=1000' >> .env
grep -qx 'RUI_GID=1000' .env || echo 'RUI_GID=1000' >> .env
docker compose config --quiet && echo OK
```

Result: `RUI_IMAGE=ghcr.io/yes999zc/rui:0.3.4`, `RUI_UID=1000`, `RUI_GID=1000` added (what `install.sh` would do); all 9 pre-existing keys untouched; `.env` stays `0600`; compose config validates.

### 3.5 Pull and recreate (documented)

```bash
docker compose pull          # 3.8 s (layers cached)
docker compose up -d --force-recreate
```

Result: container **healthy after 6 s**; image `ghcr.io/yes999zc/rui:0.3.4` @ `sha256:e1cd2bede49edc90eae0085c83f7caee566ba2fa9acc094858b42b5c796c2aed`; `User=rui` (image default, old compose has no `user:` line).

Note: this first recreate still used the **old 0.2.2-era compose file** — and it works (see friction #5).

### 3.6 Adopt the v0.3.4 compose file (documented as "manual merge", runbook §5)

```bash
# old file already saved at ~/rui-upgrade-$TS/docker-compose.yml
scp <upstream v0.3.4>/docker-compose.yml yes999zc@192.168.5.34:~/rui/docker-compose.yml
docker compose config --quiet && echo OK
docker compose up -d --force-recreate
```

Result: healthy after 6 s. Host `docker-compose.yml` now byte-identical to the v0.3.4 tag (`sha256:3897bfc9f3c881cb8b35444b4f07717bd0675af3ebbf5e539a5f2d0910bbab27`). Also refreshed the reference `config.example.yaml` to the v0.3.4 file (`sha256:8b81193a…`, now contains the new `security.outbound` block).

Verified runtime contract after the new compose:

```text
Image=ghcr.io/yes999zc/rui:0.3.4 User=1000:1000 ReadOnly=true CapDrop=[ALL]
SecurityOpt=[no-new-privileges:true] Tmpfs=map[/tmp:rw,nosuid,nodev,size=256m]
NetworkMode=host Restart=unless-stopped Restarts=0
in-container: uid=1000(rui) gid=1000(rui); HOME=/var/lib/rui; RUI_STATE_DIR=/var/lib/rui/state
```

### 3.7 Verification set (documented acceptance)

```bash
docker compose ps
curl -s localhost:8080/health | python3 -m json.tool
docker compose exec -T rui rui doctor
docker compose exec -T rui rui-healthcheck
curl -s -o /dev/null -w '%{http_code}' localhost:8080/ ; echo
docker compose logs --tail=200 rui
```

Results: see §4. Console `/ = 200`, `/setup = 200`, `rui-healthcheck` exit 0, no traceback.

### 3.8 Rollback verification (see §7)

Attempt A (docs' "rollback to root image" recipe, `RUI_UID=0/RUI_GID=0`): **crash loop, permission denied** (friction #6). Attempt B (keep `1000:1000`, `RUI_IMAGE=…:0.2.2`, plain `docker compose up -d`): **healthy after 6 s, `version=0.2.2`**. Re-upgrade with plain `docker compose up -d`: healthy after 6 s, `version=0.3.4`.

## 4. Evidence (final state)

`/health` (final):

```json
{"status":"ok","version":"0.3.4","config_version":2,"config_version_supported":2,
 "uptime_s":34.1,"devices":0,"enabled_devices":0,"device_errors":0,
 "adapter_plugins":{"registered":[],"warnings":0},
 "audio_plane":false,"speaker_connected":false,"speaker_device":"","speaker_last_seen":null,
 "vendor":{"quiet_s":12.0,"muted_for":0.0,"vendor_wakes":0,"yielded_turns":0},
 "ack":{"mode":"tts","note":"ack.wav written from xfyun_tts: 490 ms at 16000 Hz -> /var/lib/rui/ack.wav",
        "wav":"/var/lib/rui/ack.wav","wav_exists":true}}
```

`rui doctor` (final, exit 0):

```text
rui doctor  verdict=degraded
  [PASS] arch: x86_64
  [PASS] cpu_cores: 8
  [PASS] ram_gb: 6.7
  [PASS] disk_free_gb: 36.1
  [WARN] container_runtime: none  -> install Docker for the one-command deploy
  [PASS] llm_key: RUI_LLM_KEY from environment
  [PASS] capture_layout: 16000 Hz requested / 8 ch / S32_LE on the device (mic 6 + ref 2, >>12/>>16)
  [PASS] playback_layout: 16000 Hz / 1 ch, queue 200 ms, fade 120 ms  -> barge-in target: silence within 300 ms
  [PASS] aec_rtf: 0.014
  [PASS] wake_runtime: sherpa-onnx 2.0.0 imported; silero VAD loaded
  [PASS] wake_path: preroll 500 ms  -> ack mode tts; silero /data/models/silero_vad.onnx
  [PASS] ack_wav: 490 ms
  AEC load: rtf=0.014  (1.4% of one core)
  device capacity: ~18 speaker(s)  [assumed 0.35 core/device, KWS+ASR not measured yet]
```

(`degraded` is exactly the same WARN as on 0.2.2; it is the in-container doctor not seeing a Docker socket, not an upgrade regression.)

`docker compose ps`:

```text
NAME   IMAGE                        COMMAND     SERVICE   STATUS                   PORTS
rui    ghcr.io/yes999zc/rui:0.3.4   "rui serve" rui       Up (healthy)             
```

Images (both kept on host for rollback):

```text
ghcr.io/yes999zc/rui:0.3.4  sha256:e1cd2bede49edc90eae0085c83f7caee566ba2fa9acc094858b42b5c796c2aed  367MB
ghcr.io/yes999zc/rui:0.2.2  sha256:05ed982a4cbe17e4dbedbca191989f43e0dd0ab93d63fa4f0bad83bc0991e738  362MB
```

Ports / healthcheck / console:

```text
LISTEN 0 2048 0.0.0.0:8080   # only 8080; 4412/8870 not bound with devices: []
rui-healthcheck: ok: uptime=35.7s devices=0 enabled=0 audio_plane=False speaker_connected=False  (exit 0)
console=200 setup=200
logs (last 300 lines): 0 tracebacks, 0 "permission denied"
```

## 5. What survived the upgrade

| Item | Status |
|---|---|
| `.env` | 11 keys now: 9 originals + `RUI_UID`/`RUI_GID`. Per-key SHA-256 of values compared before/after: **all 9 original values identical**; only `RUI_IMAGE` changed (0.2.2→0.3.4), UID/GID added. `0600`, owner unchanged. `RUI_ASR_KEY` and `RUI_HA_TOKEN` were and remain empty (ASR falls back to `local_asr`, HA disabled — pre-existing condition, not caused by the upgrade). |
| `config/config.yaml` | **byte-identical** before/after: `sha256:7a863828c0c1bd728c5961e1df5397eaa913ad63faaa6121d8de2bc0dccf234a`. Still `version: 2`, `devices: []`, `web.auth_token` preserved, port 8080, audio/AEC/provider blocks unchanged. |
| Config migration | **none performed**: on-disk version was already 2 and schema `CONFIG_VERSION=2`, so no v1→v2 in-memory migration, no rewrite of the file. `/health` shows `config_version=2 / config_version_supported=2`. The new 0.3.x `security.outbound` block is optional and defaults to enabled/allow_private/block-loopback without touching the file. |
| Models | untouched (`data/models/sensevoice`, `silero_vad.onnx`, KWS dir). No `fetch-models` run. |
| `keywords.txt` | untouched (wake word unchanged). |
| `data/` | untouched except `service.log`/`ack.wav` rewritten by the new process at each start. `data/state` is not created yet because no audio plane starts with `devices: []`. |
| Ownership | all of `config/` + `data/` now `1000:1000`, no root-owned files left. |

## 6. audio_plane / 4412 / Dot2 behaviour

- **0.2.2 before**: `audio_plane=false`, `devices=0`, nothing listening on 4412, log "audio plane did not start: no device this build can drive".
- **0.3.4 after**: `audio_plane=false`, `devices=0`, nothing listening on 4412, identical log line. So for the vanilla/no-device configuration there is **no behavioural change** on the audio plane or 4412; 4412 is the *device listen_port* used only once a `devices[]` entry exists, it is not a server bind with an empty device list.
- New in 0.3.4: `/health` gained `adapter_plugins:{registered:[],warnings:0}` (plug registry surface), console is "Phase 2" (`/setup` 200; not clicked through).
- Not exercised: real speaker audio, 4412 accept, KWS/VAD turn path, barge-in — **out of scope** because the task keeps `devices: []` until the downstream image/plugin task.

## 7. Rollback (verified, with a caveat)

The 0.2.2 image is still on the host. Rollback that **was actually executed and verified twice** (0.3.4 → 0.2.2 → 0.3.4, each healthy in ~6 s):

```bash
cd ~/rui
cp -p config/config.yaml config/config.yaml.bak.rollback.$(date +%Y%m%d-%H%M%S)
sed -i 's|^RUI_IMAGE=.*|RUI_IMAGE=ghcr.io/yes999zc/rui:0.2.2|' .env
# KEEP RUI_UID=1000 and RUI_GID=1000 — do NOT set them to 0 with the v0.3.4 compose (see below)
docker compose up -d
docker compose ps                       # healthy
curl -s localhost:8080/health          # version=0.2.2, config_version=2, audio_plane=false
```

Config restore is only needed if `config.yaml` was changed after the upgrade; here it was not, so the byte-identical file worked unchanged. To return to 0.3.4: set `RUI_IMAGE=ghcr.io/yes999zc/rui:0.3.4` and `docker compose up -d` (plain `up -d` does recreate when the pinned tag changes — verified).

**Trap (verified failure):** the documented "rollback to root image" recipe (`README.md:253`, `docs/deploy.md`, `docs/runbook.md:162-166`: set `RUI_UID=0`/`RUI_GID=0`) **crash-loops with the v0.3.4 compose** because `cap_drop: ALL` removes `CAP_DAC_OVERRIDE`; uid 0 cannot read the `0600` root-user-owned config or write the data volume:

```text
rui  | config recovery: cannot write marker: [Errno 13] Permission denied: '/var/lib/rui/config-recovery.json.tmp'
rui  | config recovery: cannot read /config/config.yaml: [Errno 13] Permission denied: '/config/config.yaml'; running from defaults
rui  | config error: serving on 0.0.0.0 without web.auth_token would expose the console to the LAN.
docker compose ps -> Restarting (2) ...
```

Keeping `1000:1000` for the old 0.2.2 image is what the runbook itself allows ("继续用 1000:1000 跑旧镜像也可以"): `.env` is identical except the tag, plain `docker compose up -d`, healthy. If someone really wants uid 0, they must also revert the compose file (no `cap_drop`/`read_only`), which contradicts the "just change the tag" rollback instruction.

## 8. Friction points (numbered; this is the point of the exercise)

1. **No single upgrade checklist for an existing install.** The actual steps are scattered across `README.md:251-253`, `docs/deploy.md:513-522 / 622-628`, `docs/runbook.md:106-166`. The two README bullets even differ in command (`up -d` vs `up -d --force-recreate`). *Suggested fix:* one "Upgrade an existing install" section with a linear checklist: backup → chown → pin image → update compose → pull/up → verify → keep rollback tag.
2. **Release notes are one line and the docs are pinned to an older version.** v0.3.4's only note is "console Phase 2: merged …" (truncated). No CHANGELOG exists. README badge and every example URL still say **v0.3.0** (`README.md:117`, `docs/deploy.md:65`), so a user copy-pasting the documented commands installs 0.3.0, not 0.3.4. The tag must be substituted manually in every URL/`RUI_REF`/`RUI_IMAGE`. (v0.4.0 is already published upstream; docs are further behind.)
3. **The documented backup step fails as documented.** `scripts/backup.sh` as the login user on a legacy root-image install dies on `config/config.last-good.yaml` with `Permission denied` and leaves an empty `~/rui-backups`. It needs `sudo` (and the resulting archive is root-owned `0600`, unreadable by the user until `chown`). Docs neither say "run as root/sudo" nor warn that the backup can be complete-but-unusable. *Suggested fix:* run the backup after the chown, or document `sudo … backup.sh` and `chown` the archive.
4. **The chown is easy to miss and the failure mode looks like a broken upgrade.** 0.2.2 ran as root and left 4 root-owned files. A user who follows only the main "升级" bullet (README:251) gets a 0.3.x container that cannot write `service.log`/`ack.wav`/`config.last-good.yaml`. Docs do mention the chown, but in a separate "升级到非 root 镜像" bullet that reads like an optional hardening step. *Suggested fix:* make the chown unconditional in the upgrade checklist (install.sh already does it).
5. **The documented upgrade does not update the compose file, silently leaving the 0.3.x security posture off.** The 0.2.2-era compose has no `user:`, `read_only`, `tmpfs`, `no-new-privileges`, `cap_drop: ALL`, nor the new `RUI_STATE_DIR/RUI_AUDIO_SOCK/RUI_CHANNEL_DATA/HOME` env. 0.3.4 does run fine on it (image `USER rui` wins), but README §security promises read-only rootfs + `cap_drop: ALL` for 0.3.x. Only `runbook.md:133-134` hints that the new compose must be manually merged; `deploy.md` doesn't. `install.sh` deliberately keeps an existing compose (`install.sh:118-122`), so re-running it does **not** fix this either. *Suggested fix:* ship an upgrade step that replaces/merges `docker-compose.yml` (or have install.sh offer `--update-compose`).
6. **Documented rollback to root images is broken with the shipped compose (verified).** `RUI_UID=0/RUI_GID=0` + v0.3.4 compose crash-loops on `Permission denied` because `cap_drop: ALL` strips DAC override. The working rollback is to keep `1000:1000`. *Suggested fix:* correct the rollback docs to say "keep 1000:1000 for all images; only revert the compose file if you really need uid 0".
7. **Re-running `install.sh` (advertised as the safe upgrade/repair path) can silently downgrade you later.** For an existing `.env` it only upserts `RUI_UID/RUI_GID` (`install.sh:187-216`) and never rewrites `RUI_IMAGE`; it starts compose with the shell `RUI_IMAGE` (`install.sh:330`). So the container temporarily runs 0.3.4 while `.env` still pins 0.2.2 — the next bare `docker compose up -d` (or reboot) recreates on 0.2.2. It also keeps the old compose. *Suggested fix:* upsert `RUI_IMAGE` in an existing `.env` too and update/merge the compose file.
8. **`doctor` "degraded" is noisy for the normal container setup.** `[WARN] container_runtime: none -> install Docker for the one-command deploy` fires inside the container (no docker socket mounted) and is the *only* reason for `degraded` here; the docs list SSH-probe/music as acceptable degraded causes but not this one. A fresh user will try to "install Docker" inside a container. *Suggested fix:* suppress or reword this probe when running inside the image.
9. **No-device audio state reads like a defect.** With `devices: []` (documented default before wiring a speaker), logs say "audio plane did not start: **no device this build can drive**" and the plane watchdog is idle; `/health.audio_plane=false`. Docs mention audio_plane only when a device is configured, but the wording "this build can drive" hints at a version/adapter mismatch. *Suggested fix:* say "no devices configured" explicitly and/or link the setup wizard step.
10. **Version pin semantics aren't obvious.** `RUI_REF` (which compose/example/scripts are fetched) and `RUI_IMAGE` (which image runs) must be pinned together; `deploy.md:76-78` says so, but the "升级" bullets only mention `RUI_IMAGE`. Pinning only the image leaves `docker compose pull`/compose semantics (and any fetch) at a different ref. *Suggested fix:* mention `RUI_REF` in the upgrade bullet too.
11. **Backup/rollback artifacts live in the user's home with mixed ownership.** `~/rui-backups` from a sudo run is root-owned; the pre-upgrade compose/config snapshot has no documented location; `.env.bak.*` is created implicitly. Docs mention `~/rui-backups` only. *Suggested fix:* document a single `~/rui-backups/<version>-<ts>/` layout including compose + image digest + config.
12. **Console token after upgrade.** It is preserved (not rotated) — good — but the upgrade section never says where to find it (`rui token`); only §2.4 says that. Minor, but it is the first thing a user needs after recreate.

## 9. Limits / what was not tested

- No real speaker/Dot2 was attached (`devices: []` by design), so 4412 accept, KWS/VAD, barge-in, LED/volume and the plug registry path are unverified in this task.
- Console was verified only at the HTTP level (200 for `/` and `/setup`, API doctor with the token returns 200); the 0.3.4 "Phase 2" UI was not clicked through.
- Host reboot persistence was not tested; the container uses `restart: unless-stopped` and the tag is pinned in `.env`, so a reboot should come back on 0.3.4.
- `install.sh` re-run was **not** executed on the live install (it would race with the running `rui` container name); its `.env`/`RUI_IMAGE` behaviour above is from reading `install.sh` lines 118-122, 187-216, 330.
- I did not upgrade to v0.4.0 (already published upstream); scope is the requested v0.3.4.

## 10. Artifacts left on `.34`

| Path | Contents |
|---|---|
| `~/rui` | final 0.3.4 install: compose = upstream v0.3.4, `.env` pinned to 0.3.4 (`0600`), config untouched |
| `~/rui-upgrade-20261025-095711/` | pre-upgrade `docker-compose.yml`, `config/config.yaml`, 0.2.2 image digest |
| `~/rui-backups/rui-backup-20261025-095717.tar.gz` | documented backup, 88 KiB, `06:00`, sha256 `f50173f0…` (now owner-readable) |
| `~/rui/.env.bak.20261025-095722` | pre-edit `.env` (`0600`) |
| `~/rui/config/config.yaml.bak.upgrade.20261025-095711` | pre-upgrade config copy |
| `~/rui/config/config.yaml.bak.rollbacktest.20261025-095832` | config copy taken before the rollback test |
| `~/rui-backup-20261025-095406/` | Lead's original secrets backup — **untouched** |
| `/tmp/rui034-evidence/` (on .34) | health/doctor/log snapshots + doctor API JSON |
| `/tmp/rui-upstream-v0.3.4` (local Mac) | upstream v0.3.4 checkout used for diffs |

No backup, image, config or `.env` was deleted.

## 11. Constraint compliance

- `192.168.5.168` was never contacted (no SSH, no curl, no DNS/port probe).
- No rui source file on the host was modified; only `~/rui` deployment files changed (`.env`, `docker-compose.yml`, `config.example.yaml`).
- No adapter/plugin was added: `devices: []`, `adapter_plugins.registered: []`.
- `network_mode: host` and console `:8080` preserved; no port mapping added; 4412 untouched.
- No secret value appears in this report or in any log captured; `.env` values were only compared by hash.
- Lead backups untouched; 0.2.2 image retained for rollback.
