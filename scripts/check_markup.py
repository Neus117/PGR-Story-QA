"""语料残留检查：抓取/清洗后如果还有维基源码漏进正文，必须当场发现。

用法
    python scripts/check_markup.py            # 检查 data/dialogue.jsonl，有残留则退出码 1
    python scripts/check_markup.py --soft     # 只报告不失败
    python scripts/check_markup.py --file X   # 检查任意 jsonl（用来验证门禁本身有效）
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pgr.markup_check import FATAL, INFO, scan, fatal_total  # noqa: E402
from pgr.paths import DATA                                   # noqa: E402


def main() -> int:
    args = sys.argv[1:]
    soft = "--soft" in args
    path = DATA / "dialogue.jsonl"
    if "--file" in args:
        path = pathlib.Path(args[args.index("--file") + 1])
    if not path.exists():
        print(f"找不到 {path}")
        return 2

    r = scan(path)
    print(f"关卡 {r['stages']}  ·  {path}")
    for name, (n, s, samples) in r["fatal"].items():
        if n:
            print(f"  [残留] {name:10} {n:5} 处 / {s} 关")
            for title, ctx in samples[:3]:
                print(f"           [{title}] {ctx}")
    for name, (n, s) in r["info"].items():
        if n:
            print(f"  [提示] {name:10} {n:5} 处 / {s} 关（可能是内容，不判失败）")

    bad = fatal_total(r)
    if bad == 0:
        print("残留检查：通过（0 处）")
        return 0
    print(f"残留检查：发现 {bad} 处源码残留")
    return 0 if soft else 1


if __name__ == "__main__":
    raise SystemExit(main())
