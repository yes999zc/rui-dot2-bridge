# NAS 部署（P4）runbook — 待用户提供两项信息后就能执行

> **时机（2026-10-24 定）**：NAS 不做日常调试。先在 Mac 本地通过
> [`milestone-m1.md`](milestone-m1.md) 的 M1 验收，再一次性部署；NAS 上的 rui 是生产实例，
> 重启会短暂影响小爱 LX06 链路。

目标：把 dot2 adapter 挂进 **NAS 上的 rui 容器**（`<lan-ip>:8080`，x86_64），
**不修改 rui 仓库、不重建 rui 镜像**。

## 需要 9 哥 给的两样

1. NAS 上 rui 的部署目录（`docker-compose.yml`、`config/`、`data/` 所在），以及本项目要放的位置；
2. rui 验收日之后，NAS 镜像更新到本轮版本的时间窗（当前 NAS 仍是上一版）。

## 部署步骤（拿到路径后）

```bash
# 0) 本项目放到 NAS 上（任选其一：rsync / SMB 拷贝 / git clone）
#    只需要 src/、secrets/、deploy/、dev/rui/config.yaml 的参考。

# 1) 证书与 token：0600，且不要进 git
#    ca.pem server.pem server.key 来自旧 controller 的 data/tls/
#    device_token 来自旧 controller SQLite（devices.token）
chmod 700 secrets && chmod 600 secrets/*

# 2) 在 rui 的 config/config.yaml 里加设备，并改两个音频参数（其余不动）：
#    devices:
#      - id: dot2
#        adapter: dot2
#        host: <lan-ip>
#        room: 客厅
#        wake_words: ["你好小瑞"]
#        volume: 30
#        listen_port: 8870
#        token_env: DOT2_TOKEN
#    audio.capture:  {rate: 16000, channels: 1, mic_channels: 1, ref_channels: 0, bits: 16}
#    audio.playback: {rate: 48000, channels: 1}

# 3) 把 DOT2_TOKEN 放进 rui 的 .env（与设备 /data/local/etc/echomuse/token 相同），
#    不要在 compose 文件里写明文。

# 4) 挂载并启动
cd <rui-deploy-dir>
docker compose -f docker-compose.yml -f <本仓库>/deploy/rui-dot2-override.yml up -d

# 5) 设备指向 NAS（静态 endpoints，有序：adapter 第一、旧 controller 第二作为回滚）
#    {"endpoints":[{"host":"<lan-ip>","port":8867,"tls_port":8870},
#                  {"host":"<lan-ip>","port":8767,"tls_port":8770}],
#     "mdns":false}
adb shell "su -c 'cat > /data/local/etc/echomuse/controller.json'" < controller.json
```

## 验收

```bash
curl -s http://<lan-ip>:8080/health | jq '{audio_plane,speaker_connected,devices}'
# 期望 audio_plane=true, speaker_connected=true, devices=2（dot2 + living）
docker compose -f docker-compose.yml -f .../rui-dot2-override.yml logs house-ear | grep -E "dot2|registered|inject"
```

设备侧：`adb shell su -c 'cat /tmp/server.log'`，应看到
`Connecting to wss://<lan-ip>:8870` → `Registered as G090LF0964340RN6` → `Mic streaming started`。

## 回滚（< 2 分钟）

1. 设备端恢复 `controller.json` 只保留旧 controller（或删掉走 mDNS）；
2. `docker compose -f docker-compose.yml -f .../rui-dot2-override.yml down`
   再用 rui 原始 compose 起回来（adapter 只是挂载，镜像和数据没变）；
3. 旧 EchoMuse controller 容器保持可启动（本机已保留）。

## 注意

- NAS 是 x86_64：本项目的 Python 代码与平台无关；TLS 证书、模型都不在容器外依赖。
- 适配器与 rui 共用 host 网络，端口 8867/8870 需要空闲（当前 8767/8770/8768 被旧
  EchoMuse controller 占用，所以本项目换了一组端口，避免冲突）。
- 30% 音量上限由 adapter 强制：连接时 `volume_set(38/127)`、物理按键超限会被拉回、
  rui 的 `set_volume` 也走同一上限。
