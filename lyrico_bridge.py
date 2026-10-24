"""
飞牛 NAS 原生音乐刮削服务 - Lyrico 插件桥接与管理引擎 (Lyrico Bridge)
========================================================================
负责扫描、管理与调度 Lyrico-Plugins 规范的 JavaScript 扩展插件。
通过零依赖轻量 Node.js 宿主 (lyrico_host.js) 实现跨生态插件加载，
将 Apple Music、MusicBrainz、LrcShare、汽水音乐等插件无缝融入多源刮削引擎。
"""

import os
import sys
import json
import shutil
import logging
import threading
import subprocess
import time
from typing import List, Dict, Any, Optional

import app_paths

logger = logging.getLogger("musictag.lyrico")

PROJECT_ROOT = app_paths.BASE_DIR
BUNDLED_PLUGINS_DIR = app_paths.BUNDLED_PLUGINS_DIR
DEFAULT_PLUGINS_DIR = app_paths.PLUGINS_DIR
RUNNER_JS = os.path.join(PROJECT_ROOT, "lyrico_host.js")


class LyricoBridge:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init_engine()
            return cls._instance

    def _seed_bundled_plugins(self):
        """如果运行状态插件目录为空，将随包内置的官方插件种子拷贝至可写目录"""
        try:
            os.makedirs(self.plugins_dir, exist_ok=True)
            if os.path.isdir(BUNDLED_PLUGINS_DIR):
                for item in os.listdir(BUNDLED_PLUGINS_DIR):
                    src = os.path.join(BUNDLED_PLUGINS_DIR, item)
                    dst = os.path.join(self.plugins_dir, item)
                    if os.path.isdir(src) and not os.path.exists(dst):
                        shutil.copytree(src, dst)
        except Exception as e:
            logger.warning(f"同步随包插件至数据目录异常: {e}")

    @staticmethod
    def _find_node() -> Optional[str]:
        """按优先级查找 Node.js 可执行文件。

        飞牛应用以受限环境启动，PATH 里通常只有 /usr/bin、/bin，而 Node.js 常装在
        /usr/local/bin、/usr/local/node/bin 或飞牛应用中心的应用目录
        （/var/apps/nodejs*/target/bin）下，shutil.which 找不到，需要额外探测。
        """
        candidates: List[str] = []

        env_node = (os.environ.get("MUSICTAG_NODE") or os.environ.get("NODE_BIN") or "").strip()
        if env_node:
            candidates.append(env_node)

        found = shutil.which("node") or shutil.which("nodejs")
        if found:
            candidates.append(found)

        if sys.platform == "win32":
            candidates.extend([
                r"C:\Program Files\nodejs\node.exe",
                r"C:\Program Files (x86)\nodejs\node.exe",
            ])
        else:
            candidates.extend([
                "/usr/local/bin/node",
                "/usr/bin/node",
                "/bin/node",
                "/usr/local/node/bin/node",
                "/usr/local/nodejs/bin/node",
                "/opt/node/bin/node",
                "/snap/bin/node",
            ])
            # Node 版本管理器与飞牛应用中心的 Node 应用目录
            home = os.path.expanduser("~")
            for manager in (".nvm/versions/node", ".volta/bin", ".fnm/node-versions"):
                base = os.path.join(home, manager)
                if os.path.isdir(base):
                    for entry in sorted(os.listdir(base), reverse=True):
                        candidates.append(os.path.join(base, entry, "bin", "node"))
                        candidates.append(os.path.join(base, entry, "node"))
            for apps_root in ("/var/apps", "/vol1/@appcenter", "/vol1/@appdata"):
                try:
                    for entry in sorted(os.listdir(apps_root)):
                        if entry.lower().startswith("node"):
                            for tail in ("target/bin/node", "bin/node", "runtime/bin/node"):
                                candidates.append(os.path.join(apps_root, entry, tail))
                except OSError:
                    continue

        for path in candidates:
            if path and os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        return None

    def _init_engine(self):
        self.plugins_dir = DEFAULT_PLUGINS_DIR
        self._seed_bundled_plugins()
        self.plugins: Dict[str, Dict[str, Any]] = {}
        self.proc: Optional[subprocess.Popen] = None
        self.proc_lock = threading.Lock()
        self._stderr_thread = None
        self.node_path = self._find_node()

        self.is_available = bool(self.node_path and os.path.exists(RUNNER_JS))
        if self.is_available:
            logger.info(f"LyricoBridge 初始化成功 (Node: {self.node_path})")
            self._log_node_version()
            self.discover_plugins()
        elif not os.path.exists(RUNNER_JS):
            logger.error("Lyrico 宿主脚本缺失: %s，插件加载器已挂起。", RUNNER_JS)
        else:
            logger.warning(
                "未检测到 Node.js 运行环境，Lyrico 插件加载器已挂起（PATH=%s）。"
                "若已安装 Node，请在应用设置里通过环境变量 MUSICTAG_NODE=/path/to/node 指定绝对路径后重启应用。",
                os.environ.get("PATH", ""),
            )

    def _log_node_version(self) -> None:
        """记录 Node 版本，便于确认用的是哪个运行时（22 / 24 等）。"""
        try:
            out = subprocess.run(
                [self.node_path, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            version = (out.stdout or out.stderr or "").strip()
            if version:
                logger.info("Lyrico 运行时版本: Node %s", version)
        except Exception:
            pass

    def _drain_stderr(self, proc: subprocess.Popen):
        """排空 stderr 避免 64KB 管道写满阻塞挂死"""
        try:
            while proc.poll() is None:
                line = proc.stderr.readline()
                if not line:
                    break
        except Exception:
            pass

    def shutdown(self):
        """应用停止时平稳清理子进程"""
        with self.proc_lock:
            if self.proc is not None:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=2.0)
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
                self.proc = None

    def _ensure_process(self) -> bool:
        """确保底层 lyrico_host.js 常驻 IPC 服务已启动"""
        if not self.is_available:
            return False
        with self.proc_lock:
            if self.proc is None or self.proc.poll() is not None:
                try:
                    self.proc = subprocess.Popen(
                        [self.node_path, RUNNER_JS, "--server"],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        bufsize=1
                    )
                    # 启动后台线程排空 stderr，防止插件打印日志填满系统管道引发死锁
                    self._stderr_thread = threading.Thread(target=self._drain_stderr, args=(self.proc,), daemon=True)
                    self._stderr_thread.start()
                    logger.info("Lyrico 插件常驻宿主服务已启动。")
                except Exception as e:
                    logger.error(f"启动 Lyrico 宿主服务失败: {e}")
                    self.proc = None
                    return False
        return True

    def discover_plugins(self) -> List[Dict[str, Any]]:
        """扫描并解析全部 Lyrico 插件元数据与多语言配置"""
        if not os.path.isdir(self.plugins_dir):
            return []

        discovered = {}
        for folder_name in os.listdir(self.plugins_dir):
            folder_path = os.path.join(self.plugins_dir, folder_name)
            if not os.path.isdir(folder_path):
                continue

            manifest_path = os.path.join(folder_path, "manifest.json")
            if not os.path.exists(manifest_path):
                continue

            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)

                # 解析多语言资源 (优先 zh-Hans)
                display_name = manifest.get("name", folder_name)
                description = manifest.get("description", "")
                locales_dir = os.path.join(folder_path, "locales")
                zh_json = os.path.join(locales_dir, "zh-Hans.json")
                if os.path.exists(zh_json):
                    try:
                        with open(zh_json, "r", encoding="utf-8") as lf:
                            i18n_map = json.load(lf)
                            if display_name.startswith("@"):
                                display_name = i18n_map.get(display_name.lstrip("@"), display_name)
                            if description.startswith("@"):
                                description = i18n_map.get(description.lstrip("@"), description)
                    except Exception:
                        pass

                icon_path = os.path.join(folder_path, manifest.get("icon", "icon.png"))
                has_icon = os.path.exists(icon_path)

                plugin_info = {
                    "key": folder_name,
                    "id": manifest.get("id", f"com.{folder_name}.source"),
                    "name": display_name,
                    "version": manifest.get("versionName", "1.0.0"),
                    "author": manifest.get("author", "Lyrico"),
                    "description": description,
                    "capabilities": manifest.get("capabilities", []),
                    "has_icon": has_icon,
                    "folder_path": folder_path,
                    # 启用状态唯一来源是配置（enabled_lyrico_plugins），此处不做默认启用判断
                    "enabled": False
                }
                discovered[folder_name] = plugin_info
            except Exception as e:
                logger.warning(f"解析 Lyrico 插件元数据失败 ({folder_name}): {e}")

        self.plugins = discovered
        logger.info(f"已扫描并加载 {len(self.plugins)} 个 Lyrico 插件: {list(self.plugins.keys())}")
        return list(self.plugins.values())

    def call_plugin(self, plugin_key: str, action: str, request: Dict[str, Any]) -> Any:
        """通过 IPC 向常驻 Node 宿主下发任务并获取结构化结果"""
        if not self.is_available:
            return None

        if plugin_key not in self.plugins:
            logger.warning(f"未找到插件: {plugin_key}")
            return None

        if not self._ensure_process():
            return None

        plugin_dir = self.plugins[plugin_key]["folder_path"]
        task = {
            "pluginDir": plugin_dir,
            "action": action,
            "request": request
        }

        task_line = json.dumps(task, ensure_ascii=False) + "\n"

        # 设置超时上限，避免慢插件无限挂起
        with self.proc_lock:
            proc = self.proc
            if proc is None or proc.stdin is None or proc.stdout is None:
                return None
            try:
                proc.stdin.write(task_line)
                proc.stdin.flush()
            except Exception as e:
                logger.error(f"Lyrico IPC 写入失败 [{plugin_key}.{action}]: {e}")
                self._kill_process()
                return None

            response_line = self._read_line_with_timeout(proc, 25.0)
            if response_line is None:
                logger.warning(f"Lyrico 插件执行超时 [{plugin_key}.{action}]")
                # 关键：超时后必须连同读取线程一起销毁进程，
                # 否则遗留的读线程会抢走下一个进程的输出行，造成请求/响应错配。
                self._kill_process()
                return None
            if not response_line:
                return None

            try:
                res = json.loads(response_line)
            except ValueError:
                logger.error(f"Lyrico 插件返回非法 JSON [{plugin_key}.{action}]")
                self._kill_process()
                return None
            if not res.get("ok"):
                logger.warning(f"Lyrico 插件执行报错 [{plugin_key}.{action}]: {res.get('error')}")
                return None
            return res.get("data")

    def _kill_process(self) -> None:
        """彻底销毁宿主进程并等待其退出，确保不再有残留读线程。"""
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=3.0)
        except Exception:
            pass
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    @staticmethod
    def _read_line_with_timeout(proc: subprocess.Popen, timeout: float) -> Optional[str]:
        """在独立线程中读取一行，超时返回 None。

        线程只在进程被销毁后才会自然退出（调用方负责 kill），
        因此不会出现新旧进程共用同一管道读取者的情况。
        """
        result: List[str] = []

        def _reader() -> None:
            try:
                line = proc.stdout.readline()
                result.append(line)
            except Exception:
                result.append("")

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            return None
        return result[0] if result else None

    def search_songs(self, plugin_key: str, keyword: str, page: int = 1, page_size: int = 5) -> List[Dict[str, Any]]:
        """调用插件搜歌接口 searchSongs"""
        p_info = self.plugins.get(plugin_key)
        if not p_info or "searchSongs" not in p_info.get("capabilities", []):
            return []

        res = self.call_plugin(plugin_key, "searchSongs", {
            "keyword": keyword,
            "page": page,
            "pageSize": page_size,
            "separator": "/"
        })

        if isinstance(res, list):
            return res
        return []

    def get_lyrics(self, plugin_key: str, song_dict: Dict[str, Any]) -> Optional[str]:
        """调用插件歌词接口 getLyrics 并转换为标准 LRC 字符串"""
        p_info = self.plugins.get(plugin_key)
        if not p_info or "getLyrics" not in p_info.get("capabilities", []):
            return None

        res = self.call_plugin(plugin_key, "getLyrics", {
            "song": song_dict
        })

        if not res:
            return None

        # 针对各插件不同的歌词返回结构做统一 LRC 解析
        if isinstance(res, list) and len(res) > 0:
            first_lyric = res[0]
            # 1. 如果已含有 text
            if isinstance(first_lyric, dict):
                if first_lyric.get("text"):
                    return first_lyric["text"]
                # 2. 如果包含 lines (逐行歌词)
                lines = first_lyric.get("lines")
                if isinstance(lines, list):
                    lrc_rows = []
                    for row in lines:
                        t_ms = row.get("time_ms")
                        txt = row.get("text", "")
                        if t_ms is not None:
                            sec = int(t_ms // 1000)
                            ms = int(t_ms % 1000)
                            m = sec // 60
                            s = sec % 60
                            lrc_rows.append(f"[{m:02d}:{s:02d}.{ms:03d}]{txt}")
                        elif txt:
                            lrc_rows.append(txt)
                    if lrc_rows:
                        return "\n".join(lrc_rows)

        return None


# 全局单例
lyrico_bridge = LyricoBridge()
