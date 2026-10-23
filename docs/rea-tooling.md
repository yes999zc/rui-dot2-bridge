# REA（Reverse Engineer Anything）本机调研

> 调研时间：2026-10-24 · 调研人：rea-scout
> 目的：确认「GitHub 上的 REA 项目」是什么、本机装在哪、能不能用、对 EchoMuse Dot 2 任务有什么价值。
> 结论先行：**找到了，且能用**。它是 `morluto/rea`（npm 包 `rea-agents`），本机 **v6.7.0**，入口 `/opt/homebrew/bin/rea`。

---

## 一、结论速览

| 问题 | 答案 |
|---|---|
| 项目全名 | **REA: Reverse Engineer Anything**（仓库 `morluto/rea`，npm 包 `rea-agents`，MCP 名 `io.github.morluto/rea`） |
| GitHub | https://github.com/morluto/rea ・中文 README：https://github.com/morluto/rea/blob/main/docs/readme/README_zh.md |
| 是什么 | 给 AI Agent 用的**逆向工程 MCP 服务 + CLI**：反编译原生二进制、分析 Android 设备/APK、抓包、固件、JS/Electron、.NET、Go/Rust 二进制……结果是带 `evidence_id`/`confidence`/`limitations` 的 Evidence JSON |
| 本机安装方式 | **Homebrew 版 Node 的全局 npm 安装**（不是 pipx/uv/brew/cargo），安装于 2026-10-19 22:28 |
| 本机路径 | 命令 `/opt/homebrew/bin/rea` → `/opt/homebrew/lib/node_modules/rea-agents/scripts/rea.mjs` |
| 版本 | **6.7.0**（与 npm 最新版一致），Node v26.12.0 / npm 12.2.0 |
| 能直接跑吗 | CLI 能跑：`rea --version` → `6.7.0`；`rea doctor` → `healthy: false`（缺 Hopper/Ghidra/IDA/tshark 等，但 adb 链路可用） |
| 对本项目 | **中等偏上价值**：可以 1 分钟内取证"设备上跑的 server 二进制 = EchoMuse v2.18.1 / Go 1.24.0 / android-arm"（已实测）；深度反编译/固件分析需额外安装工具，**不建议为它先装 Hopper** |

---

## 二、项目全名、链接与用途

- 仓库：**morluto/rea** — https://github.com/morluto/rea
- 中文 README：https://github.com/morluto/rea/blob/main/docs/readme/README_zh.md
- npm：https://www.npmjs.com/package/rea-agents （`npx rea-agents setup` 是官方推荐安装方式）
- 官网：https://rea.tools/ ・中文站：https://rea.tools/zh-CN/
- License：MIT

**定位（摘自中文 README）**：

> REA：逆向一切 —— 通过一个 MCP 服务，逆向分析二进制文件、应用程序和运行时行为。看到喜欢的功能，弄清它的工作原理，深入到二进制层面。

它把本机已有的逆向工具（Hopper / Ghidra / IDA / adb / tshark / binwalk / JADX / Frida / rizin …）统一封装成 **MCP 工具 + CLI 命令**，每条结论都返回证据和限制，方便 Agent 做可复核的逆向调查。

与本项目相关的官方能力对照（中文 README「可以分析什么」表）：

| 目标 | REA 返回内容 | 依赖 |
|---|---|---|
| **Go 二进制** | 内嵌编译器版本、模块、替换项、构建设置 | **内置静态检查，无需装 Go**（本项目最有用） |
| **Android 设备** | 设备信息、软件包、进程、日志、截图、APK/文件传输 | 调用方自备 `adb` |
| **原生二进制** | 伪代码、汇编、字符串、符号、调用与引用 | Hopper / Ghidra / IDA（本机未装） |
| 离线 ELF 布局 | 节、段、符号/重定位、静态防护候选项 | Linux x64 + pwntools（**不支持 ARM ELF**，见实测） |
| 数据包捕获 | 端点、DNS、TLS SNI、协议分布、HTTP 请求 | tshark + capinfos（本机未装） |
| 固件 | 区域、提取结果、转交原生分析 | **仅 Linux** + binwalk/unblob（本机 macOS 不可用） |
| Android APK | 清单、类、反编译方法、引用 | JADX headless MCP JAR + JDK（本机不符合） |
| .NET / JS / Electron / 网站 / WASM / Rust | 静态或运行时分析 | 各自工具链 |

`schema`/证据模型：每次调用返回 `evidence_id`、`provider`、`raw_result`、`normalized_result`、`confidence`、`limitations`、`locations`，很适合写进我们 `docs/findings.md` 这种"每条事实带出处"的文档。

**命名歧义提醒**：GitHub 搜 `REA` 还会命中 REAPER 的 `ReaScripts`、`.NET` 的 `ReaLTaiizor`、`react` 等，都不是。周边项目：
- `bbylw/rea-cn`：中文版/镜像（描述同 REA）
- `JoasASantos/REA-Prompts`、`reacher-z/Awesome-REA`、`songshikang0111/awesome-REA-cases`：提示词与案例集
- `aicodeking/leanrea`：号称用 8KB skill 替代 REA 的方案

---

## 三、本机安装（已确认）

### 3.1 安装事实

```text
命令： /opt/homebrew/bin/rea          （软链，10月19日 22:28 创建）
       /opt/homebrew/bin/rea-agents   （同一目标的另一个名字）
目标： /opt/homebrew/lib/node_modules/rea-agents/scripts/rea.mjs
包：   rea-agents@6.7.0   （package.json: mcpName io.github.morluto/rea, MIT）
安装： Homebrew 版 Node 的全局 npm 安装（npm prefix = /opt/homebrew）
       /opt/homebrew/lib/node_modules/ 目录 mtime = 2026-10-19 22:28:13
运行时： node v26.12.0（`/opt/homebrew/bin/node`），npm 12.2.0
最新版： npm registry 上 rea-agents latest = 6.7.0（本机即最新）
```

### 3.2 为什么一开始没找到（重要）

1. **不是 pipx/uv/brew/cargo 装的**：`pipx list`、`uv tool list` 都是空；`brew list --formula` 里只有 `readline`/`libudfread`/`libunibreak` 这些带 "rea" 子串的包；`brew search rea` 没有对应 formula。
2. **`npm ls -g` 看不到**：本机 npm 默认 prefix 是 `~/.local`（`~/.local/lib/node_modules` 为空），而 REA 装在 **Homebrew 的 npm prefix `/opt/homebrew/lib/node_modules`**。要查它得用：
   ```bash
   npm ls -g --depth=0 --prefix /opt/homebrew | grep rea
   # └── rea-agents@6.7.0
   ```
3. **PATH 差异**：用户登录 shell 的 `~/.zshrc` 第 4 行就是 `export PATH="/opt/homebrew/bin:$PATH"`，所以**普通终端里 `rea` 直接可用**；但 agent 的 bash 工具继承的是一个极简 PATH（只有 `~/.hermes/node/bin`、`/usr/bin` 等），所以 `gh`、`rea`、`adb` 一开始都"找不到"。本文件所有命令都显式 `export PATH`。
4. 同名工具 `reasonix` 也在 `/opt/homebrew/lib/node_modules/`，与 REA 无关。

### 3.3 adb 环境（REA 的 Android 能力依赖它）

```text
ANDROID_HOME = /opt/homebrew/share/android-commandlinetools   （~/.zshrc 第 20 行）
adb          = /opt/homebrew/share/android-commandlinetools/platform-tools/adb
版本         = 37.0.0-14910828
```
REA 不把 adb 当 provider，而是直接调用 PATH 上的 `adb`；所以必须把 `platform-tools` 放进 PATH（用户终端已默认有）。

### 3.4 MCP 注册状态（当前只能 CLI 用）

`rea doctor` 显示 `registration:*` 全部 `config_drift`（`dsh`、`claude_code`、`codex`、`cursor`、`hermes`、`gemini_cli` 等 19 个客户端均未注册），`skill:identity` 也缺失。

**意味着：现在没有任何 Agent 能通过 MCP 直接调用 REA**，只能用 CLI。要接进 agent 需要运行 `rea setup`——它会写 `~/.claude.json`、`~/.codex/config.toml`、`~/.hermes/config.yaml`、`~/.cursor/mcp.json` 等一堆配置（有备份），属于跨项目配置变更，**本次未执行**，需用户明确同意。

---

## 四、实测结果（2026-10-24，本机 + 已连接的 Dot 2）

### 4.1 基础自检

```bash
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
rea --version        # → 6.7.0
rea doctor           # → healthy: false / environment_healthy: false
rea providers        # → 4 个 provider
```

`rea doctor` 关键项：

| 检查 | 结果 | 说明 |
|---|---|---|
| node | ✅ healthy | v26.12.0 |
| host | ✅ healthy | 27.0.1 |
| hopper | ❌ missing_analysis_engine | 未安装（`HOPPER_LAUNCHER_PATH` 未设） |
| ghidra | ❌ missing_analysis_engine | `GHIDRA_INSTALL_DIR` 未设，且本机无 Java |
| ida-registration | ❌ missing_analysis_engine | 未配置 |
| jadx | ❌ missing_analysis_engine | 需 `REA_JADX_MCP_JAR` 指向 jadx-headless-mcp 0.7.1 JAR（不是 brew 的 jadx CLI） |
| kuna | ❌ missing_analysis_engine | 未配置 |
| skill:identity | ❌ config_drift | 未装 REA skill |
| registration:* | ❌ config_drift | 19 个客户端均未注册 MCP |

`rea providers` 可用 provider：`native-macos`、`rea-artifact-graph`、`rea-dotnet-static`、`sqlite`。
（`adb` 不在其中：Android 设备操作是内置直调 adb CLI，不经过 provider 体系。）

### 4.2 设备链路（真机，已实测成功）

设备：`adb devices` 显示 `G090LF…RN6`（USB，`product:csm_biscuit`，`model:AEOBC`，`device:biscuit`）——即 Dot 2；另有一台 `emulator-5554`。设备已 root，`adb shell` 可直接读 `/data/local/`。

**① 设备发现**

```bash
rea list-adb-devices
# devices[2]: G090LF…RN6 (physical, usb) / emulator-5554 (emulator, tcp)
# provider: adb 37.0.0-14910828, installed_path .../platform-tools/adb
```

**② 构建信息（`inspect-adb-device`）** — 输出为我们已知的 FireOS/Android 5.1.1：

```text
ro.build.version.release  5.1.1
ro.build.version.sdk      22
ro.build.id               LVY48F
ro.build.fingerprint      Amazon/csm_biscuit/biscuit:5.1.1/LVY48F/272.6.8.0_user_680767620:user/release-keys
ro.build.characteristics  headless
ro.product.brand          Amazon
ro.product.device         biscuit
ro.product.model          AEOBC
ro.product.cpu.abilist    armeabi-v7a,armeabi
```

**③ 拉取设备端 server 二进制（`pull-adb-file`）**

```bash
rea pull-adb-file "$S" /data/local/bin/server --output-directory /tmp/dot2
# local_path: /tmp/dot2/server
# bytes: 12591808
# sha256: 2f3523088af1a463b72260aa6fa761edfde508e118d70b7a9084260b24cd4169
```
（`/data/local/bin/server` 是软链 → `server_a`，12591808 字节；另同目录有 `start_server.sh` 11352 字节。）

**④ 解析 Go 构建信息（`inspect-go-binary`）** — **本次最有价值的输出**：

```yaml
format: elf
architecture: arm        # 32-bit, little endian, Go BuildID 存在
build_info:
  go_version: go1.24.0
  module.path: github.com/wilbowes/EchoMuse/cmd
  module.main: { path: github.com/wilbowes/EchoMuse, version: "(devel)" }
  build settings:
    -tags=server
    GOOS=android  GOARCH=arm  GOARM=7
    CGO_ENABLED=1
    -ldflags="-X github.com/wilbowes/EchoMuse/internal/client.Version=v2.18.1
              -X github.com/wilbowes/EchoMuse/internal/client.BuildUnix=1792045135"
  dependencies[14]:
    github.com/gorilla/websocket v1.5.3        # /control /data 的 wss
    github.com/grandcat/zeroconf v1.0.0        # mDNS 发现
    github.com/miekg/dns v1.1.41
    github.com/Binozo/GoTinyAlsa v1.0.3 => ../GoTinyAlsa (devel)   # ALSA 采集
    github.com/mewkiz/flac v1.0.14             # FLAC 解码
    github.com/flynn/noise v1.1.0              # Noise 协议
    github.com/gvalkov/golang-evdev ...        # 输入设备
    github.com/cenkalti/backoff / icza/bitio / mewkiz/pkg / mewpkg/term /
    golang.org/x/crypto v0.46.0 / x/net v0.48.0 / x/sys v0.41.0
```

`BuildUnix=1792045135` → **2026-10-15 14:18:55 +0800**；`file` 判定为 `ELF 32-bit LSB shared object, ARM, EABI5, dynamically linked, with debug_info, not stripped`。

**结论（可直接写进 findings）**：设备上实际运行的 EchoMuse 设备端 = **v2.18.1**，由**本地源码构建**（`main` 为 `(devel)`，且 `GoTinyAlsa` 用 `../GoTinyAlsa` 本地 replace），构建时间 2026-10-15 14:18（东八区），不是从官方 release 直接下载的二进制。这与本项目 README 里"EchoMuse 设备端 v2.18.1"一致，也与 `/tmp/echomuse-inspect`（commit `87a3d21`，tag `emos-v0.11-ea.1`，文档提到 v2.18.1）对得上。

**⑤ 其他已实测命令**

| 命令 | 结果 |
|---|---|
| `rea read-adb-logcat $S --count 5` | ✅ 正常；顺带看到 `adbd ... avc: denied { getattr } for path="/data/local/bin/server" ... permissive=1`（与"adb 直接可读 /data/local"相关） |
| `rea list-adb-features $S` | ✅ 正常（大量 `com.fireos.sdk.*`，说明这是 Amazon FireOS 设备） |
| `rea list-adb-services $S` | ✅ 正常 |
| `rea list-adb-packages $S` | ✅ 正常（49 个系统包，`com.amazon.*`） |
| `rea inspect-adb-display $S` | ✅ 正常（32x32，headless 设备无屏） |
| `rea inspect-artifact /tmp/dot2/server` | ✅ 正常（生成 artifact inventory + next probes） |
| `rea providers` / `rea capabilities` | ✅ 正常 |

### 4.3 实测失败的命令（重要限制）

| 命令 | 结果 | 原因 | 绕法 |
|---|---|---|---|
| `rea list-adb-directory $S /data/local/bin` | ⚠️ `coverage: partial`，`entries: []`，`unparsed_lines: "find: not found"` | FireOS 5.1.1 的 toolbox **没有 `find`** | 用 `adb shell ls -la <dir>` |
| `rea list-adb-processes $S` | ⚠️ `processes: []`（空） | 设备 `ps -A` 只输出表头（Android 5.1.1 的 ps 不支持 `-A`） | 用 `adb shell ps` |
| `rea analyze /tmp/dot2/server` | ❌ `provider_unavailable` | 缺少 Hopper/Ghidra/IDA/Kuna；候选 `ghidra,hopper,ida,kuna` 全部 `not_configured` | 需装分析引擎（Ghidra 还要 JDK 21） |
| `rea search /tmp/dot2/server "mic_start"` | ❌ `provider_unavailable` | 字符串/过程名搜索也走深度分析 provider | 同上；或先用 `strings` 粗看 |
| `rea inspect-binary-layout /tmp/dot2/server` | ❌ `capability_unavailable` | `pwntools-elf` 初始 profile **只支持 Linux x64**，不支持 ARM ELF | 用 `inspect-go-binary`（Go 专用）或 `inspect-artifact` |
| `rea inspect-firmware-regions <path>` | ❌ `capability_unavailable` | binwalk 集成**要求 Linux** + 绝对路径 `REA_BINWALK_COMMAND` | 换 Linux 机器 / 用 macOS 上其他方式 |
| `rea inspect-pcap-capture <pcap>` | ❌ `capability_unavailable` | 无 `tshark`/`capinfos`（`spawn tshark ENOENT`） | `brew install wireshark`（4.6.9，CLI 含 tshark） |
| `rea inspect-adb-package` / APK 反编译 | ⚠️ 未测 | REA 用 `REA_JADX_MCP_JAR`（jadx-headless-mcp 0.7.1 JAR + JDK），本机只有 brew 的 jadx 1.5.6 CLI，**不是 REA 要的东西** | 非本项目重点，可跳过 |

补充：`/opt/homebrew/bin/jadx`、`jadx-gui`（1.5.6）、`~/.local/bin/mitmdump`/`mitmproxy` 存在；但 `adb`、`tshark`、`rizin`、`radare2`、`frida`、`ghidra`、`cutter`、`sigrok-cli` 均未安装（`adb` 在 platform-tools 目录里，只是默认 PATH 不含）。

---

## 五、针对本项目可直接执行的命令

> 统一前置（用户普通终端里可省略第一行）：
> ```bash
> export PATH="/opt/homebrew/bin:/opt/homebrew/share/android-commandlinetools/platform-tools:$PATH"
> S=$(adb devices | awk '/^[A-Z0-9]+[[:space:]]+device/{print $1; exit}')   # Dot 2 序列号
> ```

### A. 设备取证 / 二进制溯源（现在就能用，无需额外安装）

```bash
# 1) 设备清单与身份
rea list-adb-devices
rea inspect-adb-device "$S"

# 2) 关键：把设备上真实运行的 server 拉下来并核对版本（约 1 分钟）
mkdir -p /tmp/dot2
rea pull-adb-file "$S" /data/local/bin/server --output-directory /tmp/dot2
rea inspect-go-binary /tmp/dot2/server        # 看 go_version / module / ldflags / deps

# 3) 设备现场证据（服务是否在跑、开机脚本、日志）
rea read-adb-logcat "$S" --count 200          # 服务端日志/崩溃
rea list-adb-packages "$S"                    # 49 个 FireOS 包
rea list-adb-services "$S"                    # binder 服务
rea list-adb-features "$S"
rea inspect-adb-display "$S"
rea inspect-artifact /tmp/dot2/server         # 生成 inventory + 建议的下一步探针

# 备注：list-adb-directory / list-adb-processes 在这台老设备上会静默失败（见 4.3），改用：
adb -s "$S" shell 'ls -la /data/local/bin'
adb -s "$S" shell ps | grep -i server
```

### B. 协议抓包分析（需先装 Wireshark 4.x，本机未装）

```bash
brew install wireshark      # 提供 tshark / capinfos 4.6.9（安装需用户确认）

# 端点 / DNS / TLS SNI / 协议分布（不解析 wss 明文）
rea inspect-pcap-capture /path/to/capture.pcapng

# 按端口/字段精确查看（示例：只看 8770 的 TLS 握手）
rea inspect-packet-capture /path/to/capture.pcapng \
  --display_filter 'tcp.port == 8770' \
  --fields ip.src,ip.dst,tcp.srcport,tcp.dstport,tls.handshake.extensions_server_name
```
**注意**：设备只走 `wss://`（TLS），pcap 里看不到 `/control`、`/data` 明文。REA 的 pcap 能力只适合看**端点、SNI、时序、协议分布**；要看协议内容，仍应在 controller（旧链路）或本项目 adapter 侧解密后记录，或复用 `docs/protocol-map.md` 已有的源码结论。

### C. 深度二进制分析（需 Hopper / Ghidra / IDA，本机均未装，**未实测**）

```bash
# 装 Ghidra 12.1.x + JDK 21 后：export GHIDRA_INSTALL_DIR=/path/to/ghidra
# 或装 Hopper 后：rea setup 会引导；也可设 HOPPER_LAUNCHER_PATH
rea analyze /tmp/dot2/server --provider ghidra --snapshot /tmp/dot2/snapshot.json
rea search /tmp/dot2/server --kind strings --pattern "mic_start" --snapshot /tmp/dot2/snapshot.json
rea search /tmp/dot2/server --kind procedures --pattern "EchoMuse" --mode regex --snapshot /tmp/dot2/snapshot.json
rea function /tmp/dot2/server --procedure "<从搜索得到的函数名>" --snapshot /tmp/dot2/snapshot.json
rea decompile /tmp/dot2/server --procedure "<函数名>" --snapshot /tmp/dot2/snapshot.json
```
说明：二进制 `not stripped` 且带 debug_info，装引擎后符号可读；但 Go 的伪代码可读性一般，且**目前协议的权威结论已由源码（`/tmp/echomuse-inspect`）给出**，反编译主要用于"设备行为与源码不一致"时的对证。

### D. 固件分析（本机不可用）

REA 的 binwalk 集成**只支持 Linux**（`REA_BINWALK_COMMAND` 指向绝对路径）。macOS 上会直接返回 `capability_unavailable`。若以后要做 Dot 2 固件镜像分析，需要在 Linux（或容器）里跑，或用 macOS 本地的 binwalk/unblob 之后再看 REA 是否有其他入口。

### E. APK 反编译（本机不可用，且非本项目重点）

REA 需要 `REA_JADX_MCP_JAR=/abs/path/jadx-headless-mcp-0.7.1.jar` + JDK；brew 的 `jadx` 1.5.6 CLI **不被 REA 使用**。Dot 2 的 EchoMuse 设备端是 `/data/local/bin` 下的原生 Go 二进制（不是 APK），所以本项目基本用不到这条。

---

## 六、对 EchoMuse Dot 2 任务的价值判断

### 6.1 实测带来的新事实（可直接引用）

1. **设备端二进制身份已确认**：`/data/local/bin/server` = EchoMuse **v2.18.1**、Go **1.24.0**、`GOOS=android/GOARCH=arm/GOARM=7`、`-tags=server`、CGO 开、构建于 **2026-10-15 14:18:55 +0800**，`main` 模块为 `(devel)` 且 `GoTinyAlsa` 用本地 `../GoTinyAlsa` replace → **本地源码构建，非官方 release 产物**。
2. **二进制依赖印证了协议栈**：`gorilla/websocket`（wss `/control` `/data`）、`grandcat/zeroconf`（mDNS 发现）、`miekg/dns`、`GoTinyAlsa`（ALSA 采集）、`mewkiz/flac`（FLAC，可能与 `0x04/0x05` 音乐流有关）、`flynn/noise`（Noise 协议，用途待查）、`gvalkov/golang-evdev`。这些与 `docs/findings.md` 中已核实的协议面一致，可作为佐证。
3. **设备 ADB 身份**：FireOS/Android **5.1.1**（SDK 22）、`LVY48F`、`csm_biscuit`、`AEOBC`、`armeabi-v7a`、`headless`、屏幕 32x32。

### 6.2 价值打分

| 用途 | 价值 | 理由 |
|---|---|---|
| 设备二进制版本/来源取证 | **高** | `pull-adb-file` + `inspect-go-binary` 一条链，1 分钟拿到 sha256/版本/构建时间/依赖，Evidence 带限制，适合写进 findings |
| 设备现场取证（日志/包/服务/屏幕） | **中高** | adb 命令封装规范，输出结构化；但老设备上 `find`/`ps -A` 两条会失效，需退回裸 adb |
| 协议逆向（找帧处理函数） | **中** | 需要 Hopper/Ghidra；而协议已由源码核实，收益递减 |
| 抓包分析 | **中低** | 需装 tshark；且 wss 加密，只能看端点/SNI/时序，看不到 `/data` 明文 |
| 固件镜像分析 | **低（本机为零）** | 仅 Linux + binwalk；macOS 无法用 |
| APK 反编译 | **无** | 设备端是原生 Go 二进制，不是 APK；且需 jadx-headless-mcp JAR |

**总体：有用，但非必需。** 对当前 Phase 1（写 adapter）没有直接加速作用；它最大的价值是**把"设备上跑的确实是 EchoMuse v2.18.1、由本地源码构建"变成可复核的证据**，以及后续排查"设备行为 vs 源码不一致"时的取证工具。

### 6.3 建议行动（按性价比排序）

1. **立即（低成本）**：把 §6.1 的三条事实补进 `docs/findings.md` 的"设备侧"表（本次任务只允许写本文档，未改 findings.md）。
2. **可选**：若已有现成 pcap，`brew install wireshark` 后用 §5B 看端点/SNI/时序。
3. **不建议**：为了这个项目专门装 Hopper（商业授权）或 Ghidra（要 JDK，下载大）；除非出现"设备实际帧格式与源码不符"的具体问题。
4. **需要 Agent 直接调用 REA 时**：先征得用户同意再跑 `rea setup`（会改 19 个客户端的 MCP 配置，含 `~/.claude.json`、`~/.codex/config.toml`、`~/.hermes/config.yaml` 等，有备份）。

---

## 七、排查记录（"怎么找到的 / 排除了什么"）

### 7.1 命中路径

```text
/opt/homebrew/bin/rea            → ../lib/node_modules/rea-agents/scripts/rea.mjs
/opt/homebrew/bin/rea-agents     → 同一目标
/opt/homebrew/lib/node_modules/rea-agents/   (rea-agents@6.7.0, 2026-10-19 22:28)
```
注意：`find ~ -maxdepth 4 -name rea -type f` 找不到它——因为 `/opt/homebrew/bin/rea` 是**软链**，不是普通文件；用 `ls -la /opt/homebrew/bin/rea*` 才命中。

### 7.2 GitHub 确认

```bash
gh search repos REA --limit 25
# 第一条：morluto/rea — "Reverse engineer anything with agents, from app behavior down to native binaries."
# 另有：bbylw/rea-cn（中文）、JoasASantos/REA-Prompts、reacher-z/Awesome-REA 等周边
```
再结合用户给出的中文 README 地址（`morluto/rea/docs/readme/README_zh.md`）确认无误。

### 7.3 排除过的位置/命令

| 检查 | 结果 |
|---|---|
| `pipx list` | 空（未装 pipx 包） |
| `uv tool list` | 空（虽然 `~/.local/bin/uv` 存在） |
| `brew list --formula \| grep -i rea` | 仅 `readline`/`libudfread`/`libunibreak` 等误命中；无 rea |
| `brew list --cask \| grep -i rea` | 无 |
| `brew search rea` | 无对应 formula |
| `npm ls -g --depth=0`（默认 prefix `~/.local`） | 空 → **曾误导**；实际在 `--prefix /opt/homebrew` |
| `bun pm ls -g` | 未安装 bun |
| `cargo install --list` | 未安装 cargo |
| `~/Applications` | 只有 `Pensieve.app`、`dsh-ui.command`、`Chrome Apps.localized` |
| `~/.local/bin` | 有 `uv`/`mitmdump`/`hermes` 等，**无 rea** |
| `~/Downloads`、`~/Projects`、`05_Tools`、`01_Code/tools` | 未发现 REA 的 git clone（只命中 node_modules 里带 "stream/rea" 的包） |
| 全盘 `find ~ -maxdepth 4 -name 'rea*'` | 除 Homebrew 软链外无其他 |
| `~/.zshrc` / `~/.zprofile` alias 与 PATH | 无 rea 别名；`/opt/homebrew/bin` 和 `platform-tools` 已在登录 shell PATH |
| `~/.rea` / `~/.config/rea` 配置目录 | 不存在（REA 不依赖全局配置目录；配置散在各客户端 MCP 里） |
| 其他候选名 `rea-cli`/`reaagent`/`reverse-engineering-agent` | 未发现本地安装；npm 包名实际是 `rea-agents` |

---

## 附：一句话给未来的自己

> REA 在本机：`/opt/homebrew/bin/rea`（v6.7.0，Homebrew Node 全局 npm）。设备取证链路（adb → pull → inspect-go-binary）已实测可用，能直接证明设备上跑的是 EchoMuse v2.18.1 本地构建；深度反编译/固件/抓包要额外装 Hopper·Ghidra / binwalk(Linux) / tshark。MCP 未注册，agent 目前只能通过 CLI 调。
