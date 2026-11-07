"""密钥配置：读取、校验、**先测通再落盘**、写入后保留 base/model。

顺序很重要：先用候选 key 真的调一次模型，通过了才覆盖 config.json。
否则一个写错的 key 会把原本可用的配置弄坏。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import llm
from .logfile import get as get_log

CONFIG = llm.CONFIG


def mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return key[:2] + "…"
    return key[:5] + "…" + key[-4:]


def current() -> dict:
    d = llm.diagnose()
    cfg = llm.load_config()
    return {
        "has_key": d["ok"],
        "mask": mask(cfg["api_key"]),
        "key_len": d["key_len"],
        "source": d["source"],
        "model": cfg["model"],
        "base": cfg["base"],
        "config_path": CONFIG,
        "hint": d.get("hint", ""),
    }


def _probe(key: str, base: str, model: str) -> tuple[bool, str, str]:
    """用候选 key 真的调一次模型。返回 (ok, 说明, 实际服务的模型)。"""
    payload = {"model": model, "messages": [{"role": "user", "content": "ping"}],
               "max_tokens": 4, "temperature": 0}
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                 data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            data = json.loads(r.read().decode("utf-8"))
        return True, "", data.get("model") or model
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return False, f"接口返回 HTTP {e.code}：{detail}", ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", ""


def save(key: str, model: str = "", base: str = "") -> dict:
    log = get_log()
    key = (key or "").strip()
    if not key:
        return {"ok": False, "message": "密钥不能为空"}

    warn = llm.key_shape_warning(key)
    if warn:
        log.warning("拒绝写入密钥（格式）：%s", warn)
        return {"ok": False, "message": warn}

    cfg = llm.load_config()
    base = (base or cfg["base"]).strip()
    model = (model or cfg["model"]).strip()

    # 1) 先测通，再决定要不要动配置文件
    ok, err, served = _probe(key, base, model)
    if not ok:
        log.error("候选密钥测试失败 model=%s base=%s err=%s", model, base, err[:200])
        return {"ok": False,
                "message": f"这个 key 没能通过测试，配置未改动。{err}",
                "mask": mask(key)}

    data = {"api_key": key, "base": base, "model": model, "timeout": 90}
    tmp = CONFIG + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, CONFIG)
    except Exception as e:
        log.error("写入配置失败：%s: %s", type(e).__name__, e)
        return {"ok": False, "message": f"写入配置失败：{type(e).__name__}: {e}"}

    log.info("已更新密钥 len=%d mask=%s model=%s served=%s", len(key), mask(key), model, served)
    return {"ok": True, "message": "保存成功，模型可正常调用",
            "model": served or model, "mask": mask(key)}


def test() -> dict:
    """测试当前配置文件里的 key。"""
    log = get_log()
    cfg = llm.load_config()
    if not cfg["api_key"]:
        return {"ok": False, "message": "没有配置密钥（证据模式）", "mode": "evidence"}
    ok, err, served = _probe(cfg["api_key"], cfg["base"], cfg["model"])
    if not ok:
        log.error("密钥复测失败：%s", err[:200])
        return {"ok": False, "message": err}
    log.info("密钥复测通过 served=%s", served)
    return {"ok": True, "message": "模型可正常调用", "model": served}
