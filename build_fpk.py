"""
飞牛 NAS 原生音乐刮削服务 - 打包构建工具入口引导
=================================================
旧版构建入口已正式统一迁移至符合飞牛官方规范的 build_official_fpk.py。
请直接执行:
    python build_official_fpk.py
"""

import os
import sys

if __name__ == "__main__":
    current_dir = os.path.dirname(os.path.abspath(__file__))
    official_script = os.path.join(current_dir, "build_official_fpk.py")
    print("=" * 70)
    print("💡 提示: 打包脚本已统一标准对齐至 build_official_fpk.py")
    print(f"   正在转交执行: {official_script}")
    print("=" * 70)
    os.execv(sys.executable, [sys.executable, official_script] + sys.argv[1:])
