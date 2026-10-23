# rui (house-ear) local dev sandbox

本机把 rui 跑起来，供 dot2 adapter 联调。rui 本体是 **只读** 的冻结版 checkout：
`01_Code/projects/house-ear`。本目录里的配置/密钥映射/唤醒词/启动脚本是唯一可写面，
`git -C 01_Code/projects/house-ear status --porcelain` 在联调前后都应为空。

## 一条命令启动

```bash
cd /Users/bakeyzhang/AI_Workspace
./scripts/dev_rui.sh
```

- 控制台 / 健康检查：<http://127.0.0.1:8091> · `curl -s http://127.0.0.1:8091/health`
- 日志：`dev/rui/logs/rui.log`（追加写，同时打到终端；rui 自己还会写
  `dev/rui/data/service.log`）
- 纯控制面（不拉起 audio 子进程）：`./scripts/dev_rui.sh --no-audio`
- Ctrl-C 停；脚本会先校验 config、检查 8091 是否被占用，再启动。

## 实测结果（2026-10-24 02:31，本机 macOS）

| 检查项 | 结果 |
|---|---|
| `./scripts/dev_rui.sh` 启动 | 成功，约 2–3 s 进入 `Application startup complete`（含 config 复校验 + ASR/TTS/LLM 预热） |
| `curl -s http://127.0.0.1:8091/health` | **HTTP 200**，`status=ok`，`audio_plane` 字段存在（`false`，见下） |
| 持续运行 | 16.1 s 时再次探活仍 200（验收要求 ≥10 s；另一次实测 20.4 s） |
| 本地 sherpa ASR | `local asr ready: model.int8.onnx (threads=2)`（warm 时加载成功） |
| 讯飞 TTS | 启动时预热 + 渲染唤醒应答成功：`ack.wav ... 490 ms at 16000 Hz`，3 条 filler 全部渲染 |
| DeepSeek LLM | 启动 warm 通过；`POST /api/v1/chat` 文本回合 200，回复「12 乘 7 等于 84。」，1.15 s，出 131520 B PCM@16k |
| house-ear 工作区 | `git status --porcelain` 为空 |

`/health` 实测摘要：

```json
{"status":"ok","version":"0.0.1","config_version":2,"uptime_s":12.0,
 "devices":0,"enabled_devices":0,"device_errors":0,"audio_plane":false,
 "speaker_connected":false,
 "ack":{"mode":"tts","wav_exists":true,
        "note":"ack.wav written from xfyun_tts: 490 ms at 16000 Hz -> .../dev/rui/data/ack.wav"}}
```

> `audio_plane=false` 是当前 `devices: []` 的预期结果，不是故障。日志里对应
> `audio plane did not start: no device this build can drive` +
> `plane watchdog idle: no drivable device in the config`。加设备（或 dot2 adapter）后
> 该字段才会变 true。

## 文件说明

| 路径 | 作用 |
|---|---|
| `dev/rui/config.yaml` | rui 配置，字段以本机 checkout 的 `config.example.yaml` / `config/schema.py` 为准（`version: 2`）。web 绑 `127.0.0.1:8091`；`system.data_dir` = `dev/rui/data`；模型全部用绝对路径；providers 只留 `local_asr` / `xfyun_tts` / `deepseek_llm` |
| `dev/rui/env` | 被 `source` 的 shell 片段：**不存明文 secret**，source 时从 `~/.hermes/.env`(0600) 读取并按名字映射（`HOUSE_EAR_LLM_KEY←DEEPSEEK_API_KEY`、`DEEPSEEK_API_KEY`、`XFYUN_*`；`HOUSE_EAR_ASR_KEY` 可选留空）。缺变量只报**变量名**，不打印值 |
| `dev/rui/keywords.txt` | 唤醒词门限文件，由 repo 自己的逻辑生成（见下），一行：`n ǐ h ǎo x iǎo r uì :2.00 #0.15 @你好小瑞` |
| `dev/rui/adapters/` | **PYTHONPATH 预留位**：外挂（dot2）adapter 模块放这里即可 `import` |
| `scripts/dev_rui.sh` | 启动脚本：导出 env、`HOUSE_EAR_CONFIG`、`PYTHONPATH`，日志到 `dev/rui/logs/rui.log` |
| `dev/rui/data/` | rui 运行时数据（`ack.wav`、`ack.stamp`、`service.log`、`memory.json` 等），脚本自动创建 |
| `dev/rui/logs/rui.log` | 启动脚本的 stdout/stderr 追加日志（含每次启动头、uvicorn 访问日志） |
| `dev/rui/config.last-good.yaml` | rui 启动时自己写在 config 旁边的「最后可用配置」快照（`load_with_recovery`），不是手写文件，删了会重建 |

## 关键实现事实（都是从本机 checkout 读出来的，不是猜的）

1. **入口**：`python -m house_ear serve`（`src/house_ear/__main__.py`）。配置路径由
   `HOUSE_EAR_CONFIG` 决定（`config/store.py: default_config_path()`），否则默认
   `~/.config/house-ear/config.yaml`。`--host/--port` 可覆盖 config，但本沙箱直接用 config 里的值。
2. **config 校验**：pydantic `Config`，`version: 2`。几个容易踩的字段：
   - `capture.mic_channels + ref_channels == channels`，否则直接校验失败；
   - `ack.chime` 必须是 `house_ear.audio.chime.PRESETS` 里的名字（soft2/blip/marimba/glass/piano…）；
   - `ack.mode: tts` 的 `ack.text` 渲染超过 `ack.max_ms`(600ms) 会**降级成 tone**（不是报错）；
   - `music.enabled: true` 必须同时给 `music.secrets_path`；
   - `web.host` 非 loopback 且 `auth_token` 为空时**拒绝启动**（除非 `allow_anonymous: true`）。
     本沙箱是 loopback + 空 token + `allow_anonymous: true`，API 免 token。
3. **模型路径**（绝对路径，全部来自 `03_Data/Models/sherpa-onnx/`）：
   - ASR：`.../sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17`（provider 自动选
     `model.int8.onnx` + `tokens.txt`）；
   - KWS：`.../kws/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20`（`model_files()` 要求
     chunk-8 的 encoder/decoder/joiner + tokens.txt，本目录齐全）；
   - VAD：`.../silero_vad.onnx`。
4. **唤醒词文件格式**（`audio/kws.py`）：拼音音节空格分隔 + `:boost` + `#threshold` + `@原文`。
   真正的门限在文件里，config 里没有这个字段。生成命令（写 `dev/rui/keywords.txt`，不碰 repo）：

   ```bash
   cd 01_Code/projects/house-ear
   .venv/bin/python -m house_ear keywords --config /Users/bakeyzhang/AI_Workspace/dev/rui/config.yaml
   # => keywords: 1 wake word(s) -> .../dev/rui/keywords.txt
   ```

   repo 默认写 `#0.15`（catalog 注释: 0.20 命中 4/5，0.15 命中 4/4）；模型目录里那份旧
   生产文件是 `#0.20`，需要时改 keywords.txt 里的数字即可，不用动 config。
5. **启动时会做网络预热**（都不致命）：sherpa ASR 载模型、讯飞 TTS 试合成 + 渲染 ack/filler、
   DeepSeek `max_tokens=1` ping。所以**首次启动需要外网**；断网时只是 provider 不 warm，
   `/health` 照样 200。想要完全离线可把 `ack.mode` 改成 `earcon`。
6. **dot2 adapter 接入方式**（本冻结版 registry 只有 `xiaoai`/`mock`）：
   `house_ear.device.registry.register(name, "module:function")` 是唯一注册点，
   且必须在 `build_adapters()` 之前执行。脚本已预留：
   - 把外挂模块放进 `dev/rui/adapters/`（脚本会把它加到 `PYTHONPATH`）；
   - 启动时带 `RUI_ADAPTER_SPEC='dot2=dot2_adapter:build'`，脚本会先 register 再调用原 CLI；
   - 然后在 `dev/rui/config.yaml` 的 `devices:` 里加条目（`id`/`adapter: dot2`/`host` 必填，
     `host` 是必须字段，可先写占位；`wake_words` 默认「你好小瑞」）。
   如果 adapter 需要不同的 capture 布局，就用 config 里的 `audio.capture.*`（当前是 LX06 的
   16k/8ch/6mic+2ref 生产值），或届时再加设备级覆盖。
7. **沙箱坑**：DSH 文件沙箱下 `exec > >(tee ...)` 会因 `/dev/fd` 被拒而失败
   （`Operation not permitted`）。脚本改成管道 `... 2>&1 | tee -a "$LOG"`，靠 `pipefail`
   保留退出码；不要再改回进程替换。
8. **敏感信息**：`dev/rui/env` 刻意不落明文 secret（工作区规范 §5），所有值在 source 时
   从 `~/.hermes/.env` 读；控制台没有 token，所以 `dev/rui/data/secrets.yaml` 也不需要。
   排查时只看变量名/长度，不要 `env | grep` 打印值。

## 常用核对命令

```bash
# 是否在跑 + 健康
curl -s http://127.0.0.1:8091/health | python3 -m json.tool | head -20

# 仓库必须保持干净（联调前后都查）
git -C 01_Code/projects/house-ear status --porcelain

# 换个端口/配置做对比实验（不动默认文件）
RUI_CONFIG=/tmp/rui-alt.yaml ./scripts/dev_rui.sh

# 验证 config 能被 rui 的 schema 接受（只读，不启动）
01_Code/projects/house-ear/.venv/bin/python -c \
  "from house_ear.config import load_config; c=load_config('/Users/bakeyzhang/AI_Workspace/dev/rui/config.yaml'); print(c.web.host, c.web.port, c.providers.asr.chain)"
```

## 还缺什么（交给 Lead / dot2 联调时）

- `devices:` 目前为空 → `audio_plane` 为 false、`speaker_connected` 为 false。需要 dot2
  adapter 模块 + `RUI_ADAPTER_SPEC` 注册 + 一台 `devices[]` 条目，才能走真实唤醒→ASR→LLM→TTS→播放。
- 本 checkout 没有 dot2 adapter 源码（`docs/remaining-work.md` 提到
  `rui-dot2-bridge/docs/adr-0001-adapter-as-controller.md`，不在本仓库），本任务只搭沙箱，
  不写 adapter。
- 采集布局仍是 LX06 生产值；dot2 若设备能力不同，需要在 `audio.capture` 上按实测改。
