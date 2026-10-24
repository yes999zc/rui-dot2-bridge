# honor-nas 部署复盘（2026-10-24）

> 范围：从零把 rui 0.1.3 部署到 `<lan-ip>`（Debian 12），接上 Echo Dot 2，并跑通
> 唤醒 → ASR → LLM → TTS。结论：**最终可用**（19:06:01 唤醒「你好小瑞」→ 19:06:14 转写
> 「现在几点？」→ 回复「晚上七点零六分」），但过程暴露了 rui 侧 1 个真问题，以及我自己
> 7 个操作失误。

## 1. 过程时间线（主要节点）

1. SSH 密钥直登成功（无需密码），盘点环境：Debian 12、Python 3.11、uv、docker、rsync、8 核、38G 空闲；
2. 发现 GitHub **SSH 22 端口不可用**，服务器有 `http://127.0.0.1:7890` 代理；
3. 用 GitHub HTTPS + 一次性 token 克隆私库 `yes999zc/rui` @ `50421ee`（0.1.3），随后 remote 去 token；
4. `uv sync --extra kws` 成功；
5. 生成服务器配置（基于 dev/rui/config.yaml 替换路径/端口/token/模型），准备 env（讯飞三件套 + DeepSeek key + DOT2_*）；
6. 拷贝模型（KWS 39 MB、SenseVoice 1.1 GB、silero VAD）、桥接包、secrets、EQ 曲线、keywords；
7. 装 systemd 服务 `rui-test`，启动，`/health` OK；
8. **首次启动 KWS/VAD 关闭**：日志 `sherpa-onnx is not installed (uv sync --extra kws)`，但包其实已装；
9. 排查：`import sherpa_onnx` 失败于 `libonnxruntime.so` 缺失；PyPI `onnxruntime` 版本（1.28.0/1.31.0）
   都缺 `VERS_1.28.2` 符号；最终从 ORT GitHub release 取 **1.28.2** 的 `libonnxruntime.so*` 放入
   `sherpa_onnx/lib/`，`import` 通过；
10. 重启后 ASR 就绪（`local asr ready: model.int8.onnx`），`/api/v1/asr` 把 `ack.wav` 转成「在呢。」；
11. 把 Dot 的 `controller.json` 切到 `.34`（Mac 保留为 fallback），设备注册成功；
12. TTS 播出、真机唤醒/问答跑通；同时发现 TTS 回声被当成用户输入（自听，属于 rui AEC 关闭的已知限制）。

## 2. 由 RUI 项目造成的问题（→ 已提 issue）

### 2.1 `extras` 里的 sherpa-onnx 在 Linux 上「装得上、跑不起来」

- `uv sync --extra kws` 返回成功，`pip list` 有 `sherpa-onnx 1.13.8`；
- 但 `import sherpa_onnx` 报 `ImportError: libonnxruntime.so: cannot open shared object file`；
- wheel 的 `sherpa_onnx.libs/` 里只有 `libasound`，没有 onnxruntime；
- 扩展要求符号版本 `VERS_1.28.2`，PyPI 的 `onnxruntime` 只有 1.28.0/1.31.0，版本符号不匹配；
- 结果：KWS/VAD 关闭，但服务、`/health` 都显示正常，**属于"静默降级"**。

建议：sherpa-onnx 依赖要么在 wheel 里自带 onnxruntime，要么显式 pin 可用的 onnxruntime 版本并在
安装后做 `import` 自检；文档给出 Linux 部署说明。

### 2.2 `house-ear doctor` 不检测运行时是否真的可用

- `doctor` 的 `wake_path` 只检查 `vad_model_path` 配置字符串，不 import sherpa、不加载 KWS/VAD 模型；
- KWS 完全不可用时，doctor 仍然 `wake_path: PASS`，verdict 仅因为 `ack_wav` 未生成为 degraded；
- 对"部署前体检"这个用途来说，这是关键盲区。

建议：doctor 增加 `sherpa` 探测（import + 模型 dir 存在 + 试加载 KeywordSpotter/VAD 一次），
失败即 FAIL；并把真实 `ImportError` 文本带出来。

### 2.3 运行期错误信息误导

- 实际是动态库/版本问题，日志却打印 `kws off: sherpa-onnx is not installed (uv sync --extra kws)`；
- 运维会按提示重装（无效），而不是去查 `libonnxruntime`。

建议：`kws off` 的原因应区分 "import 失败（附异常）" 与 "未安装" 两种。

## 3. 由我自己造成的问题（下次改正）

| # | 失误 | 后果 | 下次做法 |
|---|---|---|---|
| 1 | 默认 GitHub SSH 可用，先生成部署密钥 | 白白多一步，才发现 22 被封 | **先探连通性**（ssh -T / https / 代理）再选克隆方式 |
| 2 | macOS 上用了 GNU `sed -i` | keywords 路径没替换，靠 `grep /Users/` 才发现 | 路径改写一律用 Python/YAML 解析，改完 grep 校验 |
| 3 | 用字符串替换 dev 配置生成服务器配置 | 脆弱，容易漏路径/端口 | 用 rui 自带 `config.example.yaml`/schema 结构化生成 |
| 4 | 后台 rsync 输出接了 `| tail` | 命令退出码被 tail 掩盖，模型第一次实际没传上，我误以为完成 | 后台任务保留完整输出和 exit code，传输后校验大小/哈希 |
| 5 | 用设备 shell `cat > controller.json` 写配置 | 文件被截断成 0 字节，设备退回 mDNS 发现，Dot 短暂失联 | **设备文件一律 `adb push` + `su cp` + `cat` 校验**，先备份 |
| 6 | 调 API 前没查 auth 文档 | 先试 query token / Bearer，浪费几次 401 | 先 grep 代码/文档确认 `X-Auth-Token`（rui 文档里其实有） |
| 7 | `uv sync` 后没立刻 `import sherpa_onnx` | 起了服务才发现 KWS 没加载，返工重启 | 安装后先做 import + 模型加载 smoke test |
| 8 | 看到 `/api/v1/audio` 结构异常就想归因 rui | 可能只是版本差异 | 先对照 API schema/版本，再决定是否为 bug |

## 4. 最终状态

- `.34`：systemd `rui-test` active，`:8095` 控制台、`:8870` Dot 入站、`:8872` 响度控制；
  Dot 已切到 `.34`（Mac 为 fallback），真机唤醒/ASR/LLM/TTS 跑通；
- `.168` 生产 NAS、Mac dev 实例均未被改动；
- 已知限制：rui 侧无 AEC（单声道无参考）→ TTS 回声会被当成用户输入（自听）；根治需要 P3 的
  2ch 参考通道 + rui AEC。

## 5. 下一步（按 9 哥指示）

1. 安装 P3 固件补丁（2ch 参考），并在 `.34` 上验证 rui AEC + barge-in；
2. 把 P3 补丁整理成独立 GitHub 项目（EchoMuse 固件补丁 + 构建脚本 + 安装/回滚文档），
   与本仓库解耦。
