# 部署与回滚 runbook

前提：Dot 2 已解锁、已装 EchoMuse 设备端 v2.18.1、controller 证书与设备 token 都在。
旧链路（EchoMuse controller + HA）**先保留**，它是回滚路径。

## 0. 两个部署位置

| 目标 | 位置 | 用途 |
|---|---|---|
| rui 生产 | NAS `192.168.5.168:8080`（v0.0.1，`/health` 正常，当前 1 台 LX06 在线） | adapter 最终要挂进这里的 rui 容器（x86_64） |
| 开发/标定 | 本机 `01_Code/projects/house-ear` 的 checkout + 13 个本地 `house-ear:*` 镜像 | 改 adapter 时本机跑，不动 NAS |

> rui 处于 **v1 冻结期**（`docs/v1-freeze.md`：只做四道发布门槛，不改代码）。
> 本项目的接入方式正好不需要改 rui 代码，只加挂载和配置，冲突面为零。
> NAS 上的 rui 还是上一版镜像，等验收日统一更新；adapter 可以等它更新后再挂。

> 两份部署的路径不同：本机是 `/Users/bakeyzhang/...`，NAS 上是它自己的卷路径（x86_64）。
> 证书和 `sitecustomize.py` 都要各放一份；`secrets/` 不进 git。

## 0. 素材准备

```bash
# 1) controller 的证书三件套（ca.key / ca.pem 里的 ca.key 不需要）
mkdir -p ~/AI_Workspace/01_Code/projects/rui-dot2-bridge/secrets
cp ~/AI_Workspace/01_Code/services/echomuse-controller/data/tls/{ca.pem,server.pem,server.key} \
   ~/AI_Workspace/01_Code/projects/rui-dot2-bridge/secrets/
chmod 600 ~/AI_Workspace/01_Code/projects/rui-dot2-bridge/secrets/*

# 2) 设备 token（只读出来用于比对，不要写进任何仓库文件）
sqlite3 ~/AI_Workspace/01_Code/services/echomuse-controller/data/echomuse.db \
  "select device_id, length(token) from devices;"
```

> `secrets/` 必须在 `.gitignore` 里。**这套证书丢了 = 设备要重新配网刷机。**

## 1. 把设备指向 adapter（不改固件、不重新配网）

设备静态地址文件优先于 mDNS，每次重连重新读：

```bash
adb push controller.json /data/local/tmp/controller.json
adb shell su -c "mkdir -p /data/local/etc/echomuse && cp /data/local/tmp/controller.json /data/local/etc/echomuse/controller.json"
adb shell su -c "cat /data/local/etc/echomuse/controller.json"
```

内容（**顺序即优先级**，第二个是自动回滚路径）：

```json
{
  "endpoints": [
    {"host": "192.168.5.18", "port": 8867, "tls_port": 8870},
    {"host": "192.168.5.18", "port": 8767, "tls_port": 8770}
  ],
  "mdns": false
}
```

> 第一条是本项目 adapter（8867/8870），第二条是旧 EchoMuse controller（8767/8770），按顺序尝试：
> adapter 起不来时设备会自动落到旧 controller。端口必须分开，见下方端口约定。
> **`mdns:false` 是有意的**：旧 controller 在 OrbStack 里出不了 mDNS，开着只会让每轮重连多等一次超时。

回滚到旧 controller：

```bash
adb shell su -c "rm /data/local/etc/echomuse/controller.json"   # 或改回只留旧 endpoint
# 旧 controller 需要 mDNS 广播时再起那条前台命令：
# dns-sd -P echomuse-mac _emcontroller._tcp local 8767 echomuse-mac.local 192.168.5.18 \
#   version=1 server=echomuse tls_port=8770
```

## 2. 端口约定（避免和旧 controller 打架）

| 用途 | 端口 | 备注 |
|---|---|---|
| 旧 EchoMuse controller | 8767 明文 / 8770 TLS / 8768 dashboard | OrbStack `network_mode: host` |
| 本项目 adapter | **8867 明文 / 8870 TLS** | 设备按 `controller.json` 里的 `tls_port` 走 wss |
| rui 控制台 / API | 8080 | rui 容器 |
| rui 的 LX06 回连 | 4412 | 与本项目无关，别占 |

## 3. 挂进 rui（不改 rui 仓库）

adapter 放在本仓库 `src/rui_dot2_bridge/`。rui 容器里用 compose override 挂载并注入注册钩子：

```yaml
# rui 仓库之外的 docker-compose.override.yml（放在 rui 部署目录旁）
services:
  house-ear:
    volumes:
      # 挂 src/ 而不是包目录：PYTHONPATH 的根必须同时能 import
      # rui_dot2_bridge 和 sitecustomize
      - /Users/bakeyzhang/AI_Workspace/01_Code/projects/rui-dot2-bridge/src:/opt/rui-py:ro
    environment:
      PYTHONPATH: /opt/rui-py
```

`src/sitecustomize.py` 在解释器启动时执行，给父进程和音频面子进程都注册：

```python
from house_ear.device import registry
registry.register("dot2", "rui_dot2_bridge.adapter:build")
```

> `sitecustomize.py` 必须能被 `import` 到——只放在 `PYTHONPATH` 根目录即可，Python 启动时会自动导入。
> 若 rui 镜像用了 `-S` 启动参数（不会），改用 `PYTHONSTARTUP`/显式 wrapper，届时再定。

rui 的 `config.yaml`：

```yaml
devices:
  - id: dot2
    adapter: dot2
    host: 192.168.5.18        # adapter 自身地址（告诉设备往哪拨）
    room: 客厅
    wake_words: ["你好小瑞"]

audio:
  playback:
    rate: 48000               # 关键：设备下行是 48 kHz S16 mono
    channels: 1
  capture:                    # 本 adapter 上报 1ch / 0ref，覆盖全局默认
    rate: 16000
    channels: 1
    mic_channels: 1
    ref_channels: 0
    bits: 16
```

## 4. 验收检查单

```bash
# adapter 侧
tail -f <adapter 日志>                    # 期望：register → ack → mic_start
# rui 侧
curl -s http://127.0.0.1:8080/health      # 期望：设备在线、audio_plane 正常
# 设备侧（最细）
adb shell su -c "tail -50 /tmp/server.log"   # 期望：wss 连上、[data] 有 PCM 收发
adb shell su -c "date"                        # 时钟漂移会影响 TLS，异常时先查它
```

对话验收：说唤醒词 → rui 出 ASR 文本 → TTS 从 Dot 播放 → 打断测试。

## 5. 回滚（目标 < 2 分钟）

1. `adb shell su -c "rm /data/local/etc/echomuse/controller.json"`；
2. 恢复旧 EchoMuse controller：`docker compose -f docker-compose.deploy.yml up -d`；
3. 需要时起 mDNS 广播；4. 设备 30 s 内重拨，`curl :8768/api/devices` 确认 `approved=true`。

> 因为 `controller.json` 的 endpoints 是有序数组，**bridge 挂掉时设备会自动落到第二条旧 controller**，
> 这是默认的自动回滚；手动回滚只是在它连续失败时才需要。

## 6. 已知坑（沿用旧项目记录）

- **WebUSB / mDNS**：只有重新跑 EchoMuse 向导时才需要，本方案不需要。
- **adb 抢 USB**：只有向导需要，本方案不需要。
- **容器 mDNS 出不去**：本方案用静态 `controller.json` 绕开，不再需要 `dns-sd -P` 常驻进程。
- **设备时钟**：TLS 校验把时钟钳到固件 build 时间，别去"修正"它。
