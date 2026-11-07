"""打包便携版：PyInstaller onedir + 预建语料库，双击即用，不需要 Python。

    python scripts/build_portable.py

产物：dist/战双剧情问答/（exe + _internal + data/pgr.db + config.example.json + 使用说明.txt）
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "战双剧情问答"
DIST = ROOT / "dist"
WORK = ROOT / "build"
APP_DIR = DIST / NAME

README_TXT = """战双剧情问答 · 便携版
=====================================

怎么用
  1. 双击「战双剧情问答.exe」
  2. 浏览器会自动打开；关掉浏览器窗口，后台会自己退出
  3. 第一次想生成回答，需要 DeepSeek API Key：
     在页面右上角点「设置密钥」，粘贴后点「保存并测试」

不需要安装 Python，也不需要联网抓取语料——剧情库已经打包在里面。

目录说明
  data\\pgr.db        剧情语料库（3,692 关 / 40 万句），删掉就得重新抓取
  config.json        你的密钥（第一次保存后才有，不要外发）
  logs\\app.log       运行日志，出问题先看这里
  _internal\\        运行库，不要动

常见问题
  · 双击没反应：Windows 可能拦了未签名程序，右键 →「属性」→ 勾「解除锁定」
  · 端口被占用：关掉另一个实例，或命令行运行 exe --port 8713
  · 站点语料有更新：页面右上角「更新语料」（不要频繁点，会触发限流）
"""


def run(cmd: list[str]) -> int:
    print("$", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-exe", action="store_true", help="只重铺数据，不重新打 exe")
    args = ap.parse_args()

    db = ROOT / "data" / "pgr.db"
    if not db.exists():
        print("缺少 data/pgr.db，先跑 python scripts/bootstrap.py")
        return 1

    if not args.skip_exe:
        cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
               "--windowed", "--name", NAME,
               "--add-data", f"{ROOT / 'web'}{os.pathsep}web",
               "--distpath", str(DIST), "--workpath", str(WORK),
               "--specpath", str(WORK),
               "app.py"]
        ico = ROOT / "PGR-QA.ico"
        if ico.exists():
            cmd += ["--icon", str(ico)]
        rc = run(cmd)
        if rc != 0:
            print("PyInstaller 失败")
            return rc

    # 先彻底清掉暂存区里的运行残留：history.db / logs / config.json 都是本机的，
    # 绝不能进包。只保留 PyInstaller 刚生成的 exe 与 _internal。
    keep = {APP_DIR / "战双剧情问答.exe", APP_DIR / "_internal"}
    if APP_DIR.exists():
        for child in APP_DIR.iterdir():
            if child in keep:
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()

    # 白名单式铺数据：只有语料库和关卡清单
    (APP_DIR / "data").mkdir(parents=True, exist_ok=True)
    print(f"复制语料库 {db.stat().st_size/1024/1024:.0f} MB ...", flush=True)
    shutil.copy2(db, APP_DIR / "data" / "pgr.db")
    stages = ROOT / "data" / "stages.json"
    if stages.exists():
        shutil.copy2(stages, APP_DIR / "data" / "stages.json")
    if (ROOT / "config.example.json").exists():
        shutil.copy2(ROOT / "config.example.json", APP_DIR / "config.example.json")
    (APP_DIR / "使用说明.txt").write_text(README_TXT, encoding="utf-8")

    # 出厂校验：发现任何本机数据就失败，别把用户的密钥和历史发出去
    bad = []
    allowed_root = {"战双剧情问答.exe", "_internal", "data", "config.example.json", "使用说明.txt"}
    for child in APP_DIR.iterdir():
        if child.name not in allowed_root:
            bad.append(str(child.relative_to(APP_DIR)))
    allowed_data = {"pgr.db", "stages.json"}
    data_dir = APP_DIR / "data"
    if data_dir.exists():
        for child in data_dir.iterdir():
            if child.name not in allowed_data:
                bad.append("data/" + child.name)
    if bad:
        print("\n!! 包里混进了不该有的东西，已中止：")
        for b in bad:
            print("   ", b)
        return 2

    total = sum(f.stat().st_size for f in APP_DIR.rglob("*") if f.is_file())
    print(f"\n完成：{APP_DIR}")
    print(f"体积：{total/1024/1024:.0f} MB")
    print("出厂校验：干净（无 config.json / history.db / logs）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
