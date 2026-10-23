# 早上好 — 夜班交接（2026-10-24 03:00）

> 这一晚把 P0/P1 做完，P3 的固件补丁写完并编译成功（未烧设备），P2 的测量工具备好；
> P2 的唤醒数字和 P4 的 NAS 部署在等你的两件事。

## 1. 现在正在跑什么

| 项 | 状态 |
|---|---|
| rui（本机 dev 实例） | `:8091`，`audio_plane=true`、`speaker_connected=true`，用 `dev/rui/config.yaml` |
| adapter | 在 rui 音频面子进程里，监听 `wss 8870`；设备已连上（`v2.18.1`，`192.168.5.69`） |
| 设备音量 | **38/127 = 30%**（`/api/v1/device/volume` 显示 `76/255`），配置已持久化到设备（`/tmp/em-config.json` 的 `received.startupVolume=38`） |
| 设备 AEC | **开**（`[aec] enabled: frame=512 tail=64ms`，7 麦回声路径已加载） |
| 旧 EchoMuse controller | **已停**（见 §4 的原因），容器还在，随时可起 |
| 8 小时 soak | 在跑，每分钟一行：`dev/rui/soak.jsonl`；汇总会写 `dev/rui/soak-summary.md` |

## 2. 这一晚新增的、已验证的东西

- **音量/灯/回滚**
  - 音量在 rui 控制台可见（0–255 刻度显示 76），任何来源的请求都被夹到 30%；
  - 12 颗 LED 已接上 rui 的状态（off/listening/thinking/speaking/wake/error/music…），
    `POST /api/v1/debug/led` 四个状态实测 200、无报错；
  - **自动回滚实测**：杀掉 adapter → 设备 40 s 内落到 endpoints[1] 的旧 controller；
    恢复 adapter、停旧 controller → 设备 45 s 内回到我们这边，音量重新压到 38。
- **鲁棒性**：`kill -9` 音频面 → rui watchdog 3 s 重建、设备 3 s 重连，不用重启任何东西；
  设备侧自报 `mic clock: 4086.7s audio / 4085.8s wall, stalls=0, sub_drops=0`。
- **P3 固件补丁**（未安装）：基线 `v2.19.0-ea.1` + `patches/0001-ref-uplink-*.patch`，
  已交叉编译出 ARM 二进制（sha256 `876f63b4…`），产物在 `artifacts/`，
  `./scripts/build_p3_firmware.sh` 可复现。
- **P3 的 2ch 链路**：用假设备（`tools/fake_device.py`）在第二个 rui 实例上验证：
  `aec_enabled=true`、`ref_active=true`、13.4 s 2ch 音频全部送达。
- **测试**：34 个单测全绿（帧格式、握手、音量上限、LED、2ch 布局、EOS/flush）。
- 一次真实回合：问「报一下现在的时间和音量」→ 回答「现在是10月24日凌晨2点58分。音量是30%。」，
  TTS 177 KB，设备出声、0 欠载。

## 3. 你醒来只需要做三件

1. **说 20 次「你好小瑞」**（约 5 分钟）：
   ```bash
   cd ~/AI_Workspace/01_Code/projects/rui-dot2-bridge
   python scripts/p2_trials.py --trials 20
   ```
   说完停 3–5 秒再接下一句；表格自动写进 `docs/measurements.md`。
   之后想测背景噪声/打断，照 `docs/p2-runbook.md` §2/§3。
2. **决定 P3**：补丁二进制已就绪。装它 = 固件从 `v2.18.1` 升到 `v2.19.0-ea.1-dev`，
   用设备已有的 A/B 槽，3 次启动失败会自动退回 `server_a`。**你说装我才装**（目标里写明不重刷设备）。
3. **给 NAS 路径**：rui 在 NAS 上的部署目录（有 `docker-compose.yml` / `config/` / `data/` 的那个），
   我把 `deploy/rui-dot2-override.yml` 落进去并验收。

## 4. 已知问题 / 注意

- **旧 controller 会把音量推回 85/127（0.669）**：它接管时按 HA 的 volume 状态下发，不看我们的 30% 上限。
  所以它在附件状态下是**停着**的。要启用旧 controller 做回滚演练，先把它的设备音量改成 0.3，
  否则违反"音箱 ≤30%"的约束。
- **KWS 在 30% 音量下自回放听不见**（离线评分：合成唤醒词 0 命中；+10 dB 仍 0；+20 dB 才 1 命中），
  这不是链路故障，是"小声自测"的物理上限；真人说话请按 §3 测。
- **设备采集有 ~0.05 Hz 低频漂移**（AEC 开关都有），ASR 不受影响，但会污染 `voice_rms_db` 指标；
  候选修法是 adapter 侧 90 Hz 高通，先在 P2 数据上确认再做。
- 固件补丁的基线是 **EA 版本**（`v2.19.0-ea.1`），不是 GA；如果不想碰 EA，P3 就保持"方案就绪、不执行"。

## 5. 应急 / 回滚

```bash
# 停本机 rui（设备会自动尝试 endpoints[1]，旧 controller 起不来就只是重试，不会出声）
PIDS=$(lsof -nP -iTCP:8091 -iTCP:8870 -sTCP:LISTEN | awk 'NR>1{print $2}' | sort -u)
for p in $PIDS; do kill $p; done

# 回滚到旧 EchoMuse controller（先把音量改 30%，再起容器）
export DOCKER_HOST=unix://$HOME/.orbstack/run/docker.sock
~/.orbstack/bin/docker start echomuse-controller

# 设备端 endpoints（有序；[0]=本项目，[1]=旧 controller）
cat /data/local/etc/echomuse/controller.json
# {"endpoints":[{"host":"192.168.5.18","port":8867,"tls_port":8870},
#               {"host":"192.168.5.18","port":8767,"tls_port":8770}],"mdns":false}

# 固件槽（仅 P3 安装后相关）
adb shell su -c 'readlink /data/local/bin/server'   # 当前槽
adb shell su -c 'ln -sf server_a /data/local/bin/server && kill $(pidof server)'  # 回退
```

## 6. 常用命令

```bash
curl -s http://127.0.0.1:8091/health | python3 -m json.tool | head -14
curl -s http://127.0.0.1:8091/api/v1/audio | python3 -m json.tool | head -30
curl -s http://127.0.0.1:8091/api/v1/device/volume
# 设备日志（最细）
adb shell "su -c 'cat /tmp/server.log'" | tail -50
# 设备原生日志时间戳是 UTC，+8 小时才是本地时间
```
