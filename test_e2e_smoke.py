"""端到端冒烟测试：对运行中的服务发起真实 HTTP 请求。

用法（需要先启动服务）：
    # 终端 1
    python server.py
    # 终端 2
    python test_e2e_smoke.py http://127.0.0.1:8002 /path/to/music_dir

第二个参数（音乐目录）可省略，省略时跳过扫描/写入相关用例。
注意：这是针对运行中实例的手工冒烟脚本，不属于 unittest 套件。
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8002"
MUSIC = sys.argv[2] if len(sys.argv) > 2 else ""


def req(path, method="GET", data=None, headers=None):
    url = BASE + path
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(
        url, method=method, data=body, headers={"Content-Type": "application/json", **(headers or {})}
    )
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


results = []


def check(label, ok, detail=""):
    results.append((label, ok, detail))
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail else ""))


status, body = req("/")
token_match = re.search(rb'__APP_TOKEN__="([^"]+)"', body)
check("首页可访问且注入令牌", status == 200 and bool(token_match), f"status={status}")
token = token_match.group(1).decode() if token_match else ""
H = {"X-App-Token": token}

check("健康检查", req("/api/health")[0] == 200)
check("应用图标", req("/icon.png")[0] == 200)
check("未授权访问被拒 (401)", req("/api/config")[0] == 401)
check("非法令牌被拒 (401)", req("/api/config", headers={"X-App-Token": "bad"})[0] == 401)
check("合法令牌可访问", req("/api/config", headers=H)[0] == 200)

if MUSIC:
    st, _ = req("/api/config", "POST", {"music_dir": MUSIC}, H)
    check("设置音乐目录", st == 200, f"status={st}")

st, body = req("/api/music/scan?refresh=true", headers=H)
scan = json.loads(body) if st == 200 else {}
check("扫描音乐库", st == 200 and scan.get("total", 0) > 0, f"total={scan.get('total')}")

files = scan.get("files") or []
if files:
    fp = files[0]["filepath"]
    st, body = req(
        "/api/music/write_tag",
        "POST",
        {"filepath": fp, "candidate": {"title": "测试曲", "artist": "测试歌手", "lyric": "[00:01.00]歌词"}},
        H,
    )
    check("写入标签", st == 200, f"status={st}")
    check("读取歌词", req("/api/music/lyric?filepath=" + urllib.parse.quote(fp), headers=H)[0] == 200)
    check(
        "整理预演",
        req("/api/music/organize/preview", "POST", {"filepaths": [fp]}, H)[0] == 200,
    )
    check(
        "越权文件访问被拒",
        req("/api/music/lyric?filepath=" + urllib.parse.quote("C:\\Windows\\win.ini"), headers=H)[0] in (403, 404),
    )

check("批量任务启动", req("/api/music/batch_scrape/start", "POST", {"dry_run": True}, H)[0] == 200)
check("批量任务状态", req("/api/music/batch_scrape/status", headers=H)[0] == 200)
check("插件列表", req("/api/plugins/lyrico", headers=H)[0] == 200)
check("内置插件不可删除", req("/api/plugins/lyrico/delete", "POST", {"key": "apple"}, H)[0] == 403)
check("配置非法端口被拒 (422)", req("/api/config", "POST", {"port": "abc"}, H)[0] == 422)

failed = [label for label, ok, _ in results if not ok]
print(f"\n合计 {len(results)} 项，失败 {len(failed)} 项")
if failed:
    print("失败项:", failed)
    sys.exit(1)
