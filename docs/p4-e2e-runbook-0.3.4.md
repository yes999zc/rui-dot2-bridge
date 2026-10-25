# 端到端回归手册（rui 0.3.4 + dot2 v1 插头）

> 2026-10-25。目标：验证唤醒 → 回合 → 播放、打断（barge）、自听、断线恢复、回滚。
> 环境：yes999zc@192.168.5.34（horner-nas），Dot 2 192.168.5.69，~/rui 下游镜像部署。

## 0. 前提

- 部署是 **下游镜像**：rui-dot2:0.3.4（= ghcr.io/yes999zc/rui:0.3.4 + /opt/rui-plugins/rui_dot2_bridge），
  RUI_ADAPTER_PLUGINS=dot2=rui_dot2_bridge.adapter:build。
- manifest 双份：镜像内 plugin.yaml（loader 门控）+ ~/rui/data/plugs/dot2-echomuse/（catalog/wake 策略读这份）。
- 设备侧 AEC 为生产选择（ref_channels=0），rui 不跑 AEC；device_wake 默认**未启用**（设备无 OWW 模型），
  rui 走自己的 KWS —— 见 p4-device-wake-feasibility.md。

## 1. 基线核查（每次回归先跑）

    cd ~/rui
    docker compose ps                       # Up (healthy)
    curl -s 127.0.0.1:8080/health | jq '{version,devices,enabled_devices,audio_plane,speaker_connected,adapter_plugins}'
    ss -ltn | grep -E ':(8080|8870|8872)'   # 8080 控制台；8870 wss 设备口
    docker compose logs --tail=40 rui | grep -Ei 'registered|adapter plugin|device_wake|traceback'

期望：adapter_plugins.registered=["dot2"]、warnings=0、devices=1、audio_plane=true、
speaker_connected=true；日志有 "dot2 link: dot2 registered"；可有且仅有 1 条
"device_wake ... fallback to RUI KWS"（预期）。

## 2. 自动回路段（仅对"设备 AEC 关闭/无 AEC"的拓扑）

> 目的：无人值守验证管线与时延，**不代表生产声学配置**。
>
> ⚠️ 本设备的 EchoMuse **设备 AEC 会抵消它自己喇叭的声音**（voice-pipeline Stage 3）。
> 实测：从 Dot 喇叭注入"你好小瑞"，KWS 唤醒 0 次——这条自动唤醒回路在本拓扑**不可用**。
> 唤醒/打断只能真人发声（§3）。注入钩子仍可用于验证播放链路与指标。

1. 以测试开关重启（可回滚，见 §5）：在 ~/rui/.env 增加

       DOT2_AEC_ENABLED=0
       DOT2_INJECT_PORT=8871
       DOT2_PLAYBACK_GATE_MS=0

   然后 docker compose up -d

2. 播放链路的注入验证（注入钩子是**裸 TCP JSON 行协议**，不是 HTTP；音档在 ~/rui/data/media/）：

       python3 -c "
       import json,socket,sys
       s=socket.create_connection(('127.0.0.1',8871),timeout=10)
       s.sendall(json.dumps({'wav':'/var/lib/rui/media/wake.wav'}).encode()+b'\n')
       print(s.recv(4096).decode().strip()); s.close()"

   注意：它把 WAV 从 Dot 喇叭放出来，设备 AEC 会抵消，所以不会唤醒——只用来确认播放路径与指标。

   期望：日志出现 KWS 唤醒（"你好小瑞"）→ 转录含"几点" → 回复 → playback 指标 periods>0、underruns=0。

3. 打断（播放中再唤醒）：

       curl -s -X POST 127.0.0.1:8080/api/v1/debug/say -H "X-Auth-Token: $TOKEN" \
            -H 'Content-Type: application/json' -d '{"text":"请讲一个三十秒的睡前故事"}'
       # 播放开始约 3 s 后注入唤醒词
       curl -s -X POST 127.0.0.1:8871 -d '{"wav":"/var/lib/rui/media/barge.wav"}'

   期望：日志 barge / stop_playback，服务端停推 ≤300 ms（设备 ALSA 缓冲另计）；
   用 grep -E 'silence_after_ms|barge' 取证。

4. 自听（关键回归）：只放长回复、**不**注入唤醒词，确认不产生 turn：

       curl -s -X POST 127.0.0.1:8080/api/v1/debug/say -H "X-Auth-Token: $TOKEN" \
            -H 'Content-Type: application/json' -d '{"text":"请从一数到二十"}'
       sleep 40
       curl -s 127.0.0.1:8080/api/v1/turns -H "X-Auth-Token: $TOKEN" | jq '.turns[0:3]'

   期望：不新增第二段 turn；日志无 "wake with no speech"。注意：debug/say 会把输入文本记成一条
   turn（历史假象），判据是"没有新 turn、没有新唤醒"。

5. 断线恢复：docker compose restart rui 后设备应在 5 s 内重连（registered）；再重启 Dot 复验。

## 3. 真人段（生产配置，AEC 开）

1. 去掉测试开关：删掉 DOT2_AEC_ENABLED / DOT2_INJECT_PORT / DOT2_PLAYBACK_GATE_MS（或设回 1/空/空），
   docker compose up -d。
2. 在房间里正常说"你好小瑞"，再说一句命令（如"现在几点"）→ 期望 LED 亮、回复出声。
3. 播放长回复时直接说"你好小瑞"→ 期望打断（目标 ≤300 ms 服务端停推）。
4. 安静房间放长回复 10 次，确认 0 空唤醒（记录次数/结果）。

## 4. 取证清单（每项都要数字/日志）

| 项 | 证据 |
|---|---|
| 插件注册 | /health 的 adapter_plugins、/api/v1/plugs、日志 registered 行 |
| 设备在线 | /health devices/audio_plane/speaker_connected、ss -ltn、dot2 link: registered |
| 唤醒→回合 | /api/v1/turns 的 transcript/reply、KWS 日志、TurnTrace |
| 打断 | silence_after_ms、stop_playback、播放指标 underruns |
| 自听 | 长回复期间 turn 计数不变（基线对比） |
| 音/灯/音量 | mixer 读数（0–255）、配置 0–100 映射、LED 效果 |
| 降级诚实性 | device_wake_fallbacks 计数 >0 且 KWS 持续可用（OWW 未装时的预期） |

## 5. 回滚

    # 回到 vanilla 0.3.4（无插件）
    cd ~/rui && cp docker-compose.yml.vanilla-0.3.4 docker-compose.yml
    sed -i 's/^RUI_IMAGE=.*/RUI_IMAGE=ghcr.io\/yes999zc\/rui:0.3.4/' .env
    docker compose up -d

    # 回到 0.2.2（已验证路径，必须保留 RUI_UID/GID=1000，uid 0 会 crash-loop）
    sed -i 's/^RUI_IMAGE=.*/RUI_IMAGE=ghcr.io\/yes999zc\/rui:0.2.2/' .env
    docker compose up -d

回滚判据：/health version 正确且 healthy；设备在 8870 重新注册（0.2.2 上不会有插件，设备会离线重试，
属预期）。
