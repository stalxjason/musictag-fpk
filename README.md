# 飞牛 NAS 原生音乐刮削服务 (MusicTag Native for fnOS)

专为飞牛 fnOS (Debian 12) 与现代 NAS 打造的**极轻量、原生、自带自适应 WebUI** 的音乐刮削与标签编辑后台服务。

> 不依赖 Wine 或虚拟桌面，直接继承原版 MusicTag 的多源网络刮削能力，毫秒级扫描，安全写入标签并同步生成 `.lrc` 歌词。
> 严格遵循飞牛应用开放平台规范（`run-as: package`、`TRIM_PKGVAR` 运行数据目录、生命周期脚本），支持应用中心一键安装。

---

## 🌟 核心能力

1. **轻量原生架构**
   * Python 3.11+ + FastAPI + Uvicorn 异步驱动，无 Wine 依赖；
   * 内存占用可通过 Web 界面运行状态查看（Linux 下读取 `/proc/self/status`）。

2. **多源刮削（五源 + 可扩展插件）**
   * **Apple iTunes**：免鉴权极速匹配，最高 3000×3000 封面；
   * **网易云音乐**：华语曲库，逐行时间轴 LRC 歌词；
   * **QQ 音乐**：独家版权曲库，LRC 歌词；
   * **酷我音乐**：辅助元数据补充源；
   * **MusicBrainz + CoverArtArchive**：国际权威元数据（内置 1 req/s 限速）；
   * **Lyrico 插件**：可导入第三方 JS 插件扩展数据源（需系统安装 Node.js）。

3. **繁简归一化与置信度算法**
   * 基于 `zhconv` 的字符级繁简归一化，避免词级误转（如“西元”不会被改成“公元”）；
   * 标题 50% + 歌手 35% + 元数据完整度 15% 综合评分；
   * 标题相似度过低时不再给予完整度加分，避免无关歌曲虚高。

4. **安全的标签写入**
   * 基于 `mutagen` 写入 MP3 (ID3v2.3 UTF-8)、FLAC、M4A、**WAV (ID3)**、**OGG (Vorbis)**；
   * 封面按文件头真实识别 MIME（PNG/JPEG/WebP/GIF），不再一律标为 JPEG；
   * 写入时保留原文件权限，不破坏 NAS 上其它服务的读取权限；
   * 同步生成外部同名 `.lrc`；显式清空歌词时同步清理旧 `.lrc`。

5. **自适应 Web 控制台**
   * 目录浏览（仅限授权根目录内）、统计卡片、缺失封面/歌词筛选；
   * 单曲多源候选切换、封面预览、歌词查看；
   * 批量刮削（实时进度、Dry-Run 预演、日志输出、并发保护）；
   * 整张专辑对齐写入、自动整理归档（预览 + 执行，支持重名跳过/覆盖/自动重命名）。

6. **安全设计**
   * 所有 `/api/*` 需要访问令牌（由首页自动下发，或使用 `X-App-Token` 请求头）；
   * 校验同源，拦截跨站写请求；输出 CSP / X-Frame-Options / nosniff 等安全响应头；
   * 所有文件路径限制在授权根目录内；封面 URL 防 SSRF；插件压缩包防路径穿越与超限。

---

## 📂 目录结构

```text
musictag-fnos/
├── build_official_fpk.py     # 飞牛官方标准打包脚本（fnpack），版本号取自 manifest.json
├── build_fpk.py              # 旧入口，转交 build_official_fpk.py
├── tools/                    # 放置官方 fnpack（不入库）
│   └── fnpack.exe            #   飞牛应用开放平台官方打包工具（约 4MB，请自行下载）
├── wheels_cache/             # 离线 Wheel 依赖（manylinux x86_64, cp311），打包时打进 app/wheels
├── requirements.txt          # Python 运行时依赖
├── manifest.json             # 应用元数据（唯一版本号来源）
├── icon.png                  # 应用图标（桌面入口与包图标均由它生成）
├── app_paths.py              # fnOS 路径规范（TRIM_PKGVAR / TRIM_PKGETC）
├── security.py               # 令牌、路径白名单、SSRF、ZIP 解压、MIME 嗅探
├── server.py                 # FastAPI 服务主入口
├── scraper_engine.py         # 多源刮削引擎
├── tag_engine.py             # 音频扫描、推断与标签写入
├── lyrico_bridge.py          # Lyrico 插件桥接（Node IPC，自动探测 Node 路径）
├── lyrico_host.js            # Lyrico 插件宿主
├── Lyrico-Plugins/           # 随包内置的官方插件（首次运行复制到数据目录）
├── web/
│   ├── index.html            # 单页 Web 控制台（明亮 / 深色双主题）
│   └── icon.png              # 界面图标（同时用于桌面入口）
├── install.sh / start.sh / stop.sh / musictag.service   # 非飞牛环境部署脚本
└── test_*.py                 # 自动化测试
```

> `*.fpk`、`build_target/`、`.venv/`、`tools/fnpack*` 已在 `.gitignore` 中排除，不会入库。

### `.fpk` 内部标准结构

```text
MusicTagNative-1.0.4.fpk        # fnpack 1.2.3 产出：外层 gzip + tar
├── manifest                    # 应用元数据（无后缀，含 version / service_port / changelog）
├── config/
│   ├── privilege               # run-as: package
│   └── resource                # 共享目录声明
├── cmd/                        # main + install/upgrade/uninstall/config 生命周期脚本
├── ICON.PNG                    # 64x64 包图标
├── ICON_256.PNG                # 256x256 包图标
├── wizard/                     # 预留（当前为空）
└── app.tgz                     # gzip+tar：app/ 内容 + ui/ 入口
    ├── server.py … (全部运行代码)
    ├── web/                    # 内置 Web 控制台
    ├── wheels/                 # 离线依赖（manylinux x86_64, cp311）
    ├── Lyrico-Plugins/         # 内置数据源插件
    └── ui/                     # 桌面入口 config + images/icon_{64,256}.png
```

> 打包脚本内置**产物自检**：核对官方要求的成员、JSON 合法性、脚本 shebang/LF 换行、包内 `server.py` 版本号与 manifest 版本一致，任一不符即构建失败。

### 安装后的数据位置（fnOS 规范）

| 用途 | 路径 |
| --- | --- |
| 程序（只读） | `${TRIM_APPDEST}`（`/var/apps/MusicTagNative/target`） |
| 配置 | `${TRIM_PKGETC}/config.json` |
| 曲库缓存 / 令牌 / 日志 / 插件 | `${TRIM_PKGVAR}/` |
| 共享目录 | `${TRIM_PKGVAR}` 之外由 `config/resource` 声明 |

> 本地开发时状态目录回退到用户数据目录；也可用 `MUSICTAG_STATE_DIR` 指定。

---

## 🚀 安装与运行

### 方式一：飞牛应用中心一键安装

1. 应用中心 → 右上角 **手动安装**，上传 `MusicTagNative-1.0.4.fpk`；
2. 安装过程会创建专用应用用户，并从包内 `wheels` 离线安装依赖（无需外网）；
3. 安装完成后在桌面点击「音乐标签」，或访问 `http://<飞牛NAS的IP>:8002`；
4. 首次使用请在应用设置的「访问权限」中授权音乐目录（如 `/vol1/Music`）。

> 历史版本包（1.0.0 / 1.0.1 / 1.0.2）已清理，功能与当前代码不一致，请以最新构建的 `MusicTagNative-1.0.4.fpk` 为准。

### 方式二：本地开发 / 自行构建

```powershell
# 安装依赖（开发环境）
pip install -r requirements.txt

# 启动服务（状态目录默认在用户数据目录，可用 MUSICTAG_STATE_DIR 覆盖）
python server.py
# 浏览器访问 http://127.0.0.1:8002
```

```powershell
# 重新打包（会调用 tools/fnpack.exe）
python build_official_fpk.py
```

```powershell
# 下载官方 fnpack 放到 tools/ 后执行（Windows：tools/fnpack.exe）
python build_official_fpk.py
```

> 打包前请把 [飞牛官方 fnpack](https://developer.fnnas.com/) 放到 `tools/` 下；版本号只改 `manifest.json`，`server.py`、产物文件名与包内 manifest 会随之同步校验。
> 若目标设备为 ARM，需用 `pip download --platform manylinux2014_aarch64 --only-binary=:all:` 重新生成 `wheels_cache`，否则二进制依赖不匹配。

---

## 📦 下载与发布

- **安装包下载**：[Releases](https://github.com/stalxjason/musictag-fpk/releases) → 选择最新版本的 `MusicTagNative-x.y.z.fpk`（`.fpk` 不作为源码提交，仅作为 Release 附件发布）。
- **自动构建**：推送 `v*` 形式的 tag 会触发 [.github/workflows/build-fpk.yml](.github/workflows/build-fpk.yml)，自动跑测试、打包并上传到对应的 Release；也可在 Actions 页面手动触发，仅产出构建产物。
  - CI 需要仓库变量 `FNPACK_URL` 指向飞牛官方 `fnpack` 的 Linux 可执行文件（Settings → Variables）。
- **本地打包**：把 `fnpack` 放到 `tools/`（Windows 为 `tools/fnpack.exe`），然后 `python build_official_fpk.py`；也可用环境变量 `FNPACK=/path/to/fnpack` 指定。

---
## 🧪 测试

```powershell
python test_scraper.py      # 繁简归一化、评分算法
python test_tag_engine.py   # MP3/WAV 写入、封面 MIME、重命名冲突、目录整理
python test_web_api.py      # 鉴权、CSRF、配置校验、插件上传安全、批量并发

# 端到端冒烟（需先启动服务，第二个参数为音乐目录）
python server.py
python test_e2e_smoke.py http://127.0.0.1:8002 /path/to/music
```

在线刮削冒烟测试默认跳过，设置 `MUSICTAG_ONLINE_TEST=1` 可开启。

---

## 🔐 安全说明

* 访问令牌保存在 `${TRIM_PKGVAR}/auth_token`（权限 600）。API 客户端需携带 `X-App-Token` 或 `Authorization: Bearer <token>`。
* 默认监听 `0.0.0.0`；如需仅本机访问，设置 `MUSICTAG_HOST=127.0.0.1`。
* 目录选择器仅能在授权根目录内浏览，无法读取系统目录。
* Lyrico 插件属于可执行第三方代码，仅安装可信插件；未安装 Node.js 时插件功能自动停用。

### Lyrico 插件与 Node.js

应用会依次探测以下位置的 Node.js：环境变量 `MUSICTAG_NODE` / `NODE_BIN` → `PATH` 中的 `node` / `nodejs` → `/usr/local/bin`、`/usr/bin`、`/opt/node/bin`、`/snap/bin` 等常见路径 → `~/.nvm`、`~/.volta`、`~/.fnm` → 飞牛应用目录（`/var/apps/node*`、`/vol1/@appcenter/node*`）。

飞牛应用环境的 `PATH` 通常不含 `/usr/local/bin`，若自动探测仍未命中，可在应用中心的「环境变量」中设置 `MUSICTAG_NODE=/绝对路径/node` 后重启应用。日志会打印 `Lyrico 运行时版本: Node vX.Y.Z`。

---

## 🙏 致谢

本项目的数据源扩展能力来自以下两个开源项目，特此致谢：

- **[Lyrico](https://github.com/Replica0110/Lyrico)** —— 提供插件运行时规范与宿主生态基础；
- **[Lyrico-Plugins](https://github.com/Replica0110/Lyrico-Plugins)** —— 提供 Apple Music、酷狗、汽水音乐、网易云、QQ 音乐、LRCShare、MusicBrainz 等插件实现。

感谢 **Replica0110** 与 Lyrico 社区的插件作者。本项目通过其开放的 JavaScript 插件规范接入这些数据源，并内置了部分插件（首次运行复制到应用数据目录）。

| 内置插件 | 来源项目 | 提供能力 |
| --- | --- | --- |
| Apple Music | Lyrico-Plugins | 曲目检索、高清封面、歌词 |
| 酷狗音乐 | Lyrico-Plugins | 曲目检索、KRC 歌词 |
| 汽水音乐 | Lyrico-Plugins | 曲目检索、歌词 |
| 网易云音乐 | Lyrico-Plugins | 曲目检索、LRC 歌词 |
| QQ 音乐 | Lyrico-Plugins | 曲目检索、歌词 |
| LRCShare | Lyrico-Plugins | 时间轴歌词 |
| MusicBrainz | Lyrico-Plugins | 国际元数据 |

> 插件版权归 [Lyrico-Plugins](https://github.com/Replica0110/Lyrico-Plugins) 项目及各插件作者所有；本项目仅提供加载与桥接能力（`lyrico_host.js` / `lyrico_bridge.py`），不修改插件本身的授权与许可。如需更新插件，可将新版插件目录放入应用数据目录的 `plugins/` 下，或在界面中导入 ZIP。

---

## 📄 许可证

本项目未声明开源许可证，请勿在未授权情况下二次分发。
