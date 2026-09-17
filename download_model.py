#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
download_model.py — 下载官方 Qwen3.6-35B-A3B(GGUF,IQ4_NL 量化)

用法:
    py download_model.py                 # 下到默认目录
    py download_model.py --dir D:\\some\\other\\models
    py download_model.py --file Qwen3.6-35B-A3B-UD-IQ4_XS.gguf   # 换更小的量化

断点续传:中断了重跑同一条命令就行,hf_hub_download 会接着下,不会从头来。
"""

import argparse
import os
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO = "unsloth/Qwen3.6-35B-A3B-GGUF"

# 18GB,和你原来那个 abliterated 文件同一个量化等级。
DEFAULT_FILE = "Qwen3.6-35B-A3B-UD-IQ4_NL.gguf"
# 走环境变量,别硬编码本机路径。
#   setx LLAMA_MODELS_DIR "D:\your\llama.cpp\models"
DEFAULT_DIR = os.environ.get("LLAMA_MODELS_DIR", "")

# 同仓库里的其它 4bit 档位,显存/质量不满意时换:
#   Qwen3.6-35B-A3B-UD-IQ4_XS.gguf     17.7 GB   最小
#   Qwen3.6-35B-A3B-UD-IQ4_NL.gguf     18.0 GB   ← 默认
#   Qwen3.6-35B-A3B-UD-IQ4_NL_XL.gguf  19.5 GB
#   Qwen3.6-35B-A3B-UD-Q4_K_S.gguf     20.9 GB
#   Qwen3.6-35B-A3B-UD-Q4_K_M.gguf     22.1 GB   质量最好,CPU 卸载压力也最大


def ensure_hub():
    try:
        import huggingface_hub  # noqa: F401
        return
    except ImportError:
        pass
    print("没装 huggingface_hub,正在装…")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-U",
                           "huggingface_hub[hf_xet]"])


def main():
    ap = argparse.ArgumentParser(description="下载官方 Qwen3.6-35B-A3B GGUF")
    ap.add_argument("--dir", default=DEFAULT_DIR,
                    help="模型存放目录。默认读环境变量 LLAMA_MODELS_DIR")
    ap.add_argument("--file", default=DEFAULT_FILE, help=f"文件名(默认 {DEFAULT_FILE})")
    ap.add_argument("--repo", default=REPO)
    args = ap.parse_args()

    # 先校验参数再装包 —— 参数错的时候不该白装一个依赖
    if not args.dir:
        print("没指定模型目录。两个办法:")
        print(r'  setx LLAMA_MODELS_DIR "D:\your\llama.cpp\models"   (设完新开终端)')
        print(r"  或直接 py download_model.py --dir D:\your\llama.cpp\models")
        return 1
    if not os.path.isdir(args.dir):
        print(f"目录不存在:{args.dir}")
        print("确认一下盘符和路径对不对,或者用 --dir 指定别的地方。")
        return 1

    ensure_hub()
    from huggingface_hub import hf_hub_download

    print(f"仓库:{args.repo}")
    print(f"文件:{args.file}")
    print(f"目标:{args.dir}")
    print("约 18GB。中断了重跑这条命令会续传,不会从头下。\n")

    try:
        path = hf_hub_download(
            repo_id=args.repo,
            filename=args.file,
            local_dir=args.dir,
        )
    except Exception as e:
        print(f"\n[失败] {type(e).__name__}: {e}")
        print("文件名拼错会报 EntryNotFoundError —— 去仓库页面核对一下:")
        print(f"  https://huggingface.co/{args.repo}/tree/main")
        return 1

    size = os.path.getsize(path) / (1024 ** 3)
    print(f"\n[完成] {path}")
    print(f"       {size:.1f} GB")
    print("\n下一步:双击 start_server.bat,或者手动跑里面那条 llama-server 命令")
    return 0


if __name__ == "__main__":
    sys.exit(main())
