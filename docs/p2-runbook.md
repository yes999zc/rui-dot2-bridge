# P2 runbook — 唤醒 / 打断 / 误唤醒（需要真人，约 40 分钟）

目标：拿到 Gate G1 的五个数字：安静唤醒率、背景唤醒率、8 h 误唤醒、打断静音延迟、
播放中自激。工具与配置已经就绪，本文件是明天的操作顺序。

## 0. 前置检查（2 分钟）

```bash
cd ~/AI_Workspace/01_Code/projects/rui-dot2-bridge
curl -s http://127.0.0.1:8091/health | python3 -m json.tool | head -12
# 期望 audio_plane=true, speaker_connected=true

# 录音留证（可选，重启 rui 时加环境变量）
DOT2_RECORD=/tmp/p2-mic.wav ./scripts/dev_rui.sh
```

音量保持 **30%**（adapter 在连接时已强制 `volume_set(38/127)`，物理按键调高会被拉回）。
这是 9 哥定的上限，P2 全部在这个音量下测。

## 1. 安静房间唤醒（10 分钟，20 次）

```bash
python scripts/p2_trials.py --trials 20
```

- 站在**平时说话的位置**（3–4 米），说「你好小瑞」，等它回应，再说一句短命令
  （「现在几点」//「今天天气怎么样」）；
- 每次之间停 3–5 秒，别连着喊；
- 脚本会在每次唤醒后打印 `trial N: xxx ms`，并把表格追加到 `docs/measurements.md`。

**通过**：≥ 19/20 唤醒，唤醒→首帧 P50 记录在案。

## 2. 背景噪声唤醒（10 分钟，20 次）

先放一段音乐（走 rui 自己的播放链路，音量同样 30%）：

```bash
# 把一段音乐放到 rui 的 data_dir 下（debug/play 只允许这个目录）
cp ~/Music/某首.mp3 dev/rui/data/music-test.mp3
curl -s -X POST http://127.0.0.1:8091/api/v1/debug/play \
  -H "Content-Type: application/json" -d '{"file":"'"$PWD"'/dev/rui/data/music-test.mp3"}'
```

然后重复 20 次唤醒。结束时：

```bash
curl -s -X POST http://127.0.0.1:8091/api/v1/debug/play -H "Content-Type: application/json" -d '{"stop":true}'
```

**通过**：≥ 17/20；同时留意有没有"自己回答的话触发了唤醒"（自激）。

## 3. 打断（5 分钟，5 次）

```bash
# 让 rui 说一段长回答（30 秒左右），说唤醒词打断它
curl -s -X POST http://127.0.0.1:8091/api/v1/debug/say \
  -H "Content-Type: application/json" \
  -d '{"text":"请从一数到六十，每个数字之间停半秒"}'
```

在它数数时喊「你好小瑞」。脚本会记录 `barge:` 行的 `silence_after_ms`（服务端停推时间），
注意：单声道无参考时 rui 的 AEC 是关的，但**唤醒打断仍然触发**（2026-10-24 实测 4 次、server 0 ms），
只是设备 ALSA 缓冲（约 300 ms）不在这个数字里，真实端到端打断延迟要用声学录音测。
设备侧静音由音箱缓冲决定；两者相加才是用户听到的静音。

**通过**：打断后不再继续说话，静音 ≤ 600 ms（P50）。

## 4. 误唤醒（挂在后台 8 小时）

```bash
python scripts/p2_trials.py --trials 999 --idle-timeout 28800
```

不用管它；有误唤醒会记一行（`transcript` 为空或乱码）。早上看表格次数即可。
**通过**：≤ 2 次 / 8 h。

## 5. 结果怎么读

- `docs/measurements.md`：追加的表格（可直接贴进 ROADMAP/汇报）；
- `dev/rui/p2-trials.jsonl`：每次的原始记录（唤醒时刻、延迟、转写、回答、打断）；
- 若唤醒率不达标：先看 `/tmp/rui-prod.log` 是否有 `wake:` 但 `wake with no speech`
  （说明听见了唤醒词、没听见命令，调 `vad_*`）；完全没有 `wake:` → 调 KWS 阈值/位置/音量。
- **不要**靠提高音量来"通过" P2：30% 是硬约束。如果 30% 下唤醒率不够，结论应当是
  "当前音量下不可用"，而不是悄悄调高。

## 6. 已知的量测结论（2026-10-24 自测）

- 30% 音量下用音箱自己回放唤醒词，到达麦克风的电平比 KWS 模型需要低约 **20 dB**
  （离线评分：0 命中；+10 dB 仍 0；+20 dB 才 1 命中）；
- 同一段回放录音的 **ASR** 转写完全正确（「你好，小瑞。」「小瑞，现在几点？」），
  说明麦克风/acoustic 路径没问题，差的是"小声回放"这个自测方式；
- 因此 P2 只能由真人说话完成，脚本已按这个前提设计。
