"""
什么值得买（smzdm）每日自动签到
================================
设计要点：
1. 模拟 Android 客户端签到（网页端签到接口已废弃）
2. 签名算法：参数按 key 字母序拼接，末尾追加固定 secret，MD5 后转大写
3. 幂等：已签到则识别为 ALREADY，不视为失败
4. 异常分级：Cookie 失效 / 网络异常 / 风控，分别用不同退出码，便于 Actions 判断
5. 通知失败不影响签到结果判定

环境变量：
    SMZDM_COOKIE        必填，登录后的 Cookie 字符串
    PUSHPLUS_TOKEN      选填，填写后推送结果到微信
    SMZDM_SK            选填，部分接口需要的 sk（抓包可得）
"""

import hashlib
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

import requests


VERSION = "10.4.1"
FORM = "android"
SECRET = "apr1$AwP!wRRT$gJ/q.X24poeBInlUJC"

ANDROID_UA = (
    "smzdm_android_V10.4.1 rv:841 (22021211RC;Android12;zh)smzdmapp"
)
WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 退出码约定
EXIT_OK = 0
EXIT_ALREADY = 0        # 已签到，同样算正常
EXIT_COOKIE_INVALID = 2  # Cookie 失效，需要人工更新
EXIT_NETWORK = 3         # 网络问题，次日重试即可
EXIT_UNKNOWN = 4         # 其他

CST = timezone(timedelta(hours=8))


def now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_cst()}] {msg}", flush=True)


def make_sign(params: dict) -> str:
    """参数按 key 字母序拼接 + secret，MD5 大写"""
    items = sorted(params.items())
    raw = "&".join(f"{k}={v}" for k, v in items)
    raw = f"{raw}&key={SECRET}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def build_params(cookie: str, extra: dict | None = None) -> dict:
    ts = str(int(round(time.time() * 1000)))
    params = {
        "f": FORM,
        "v": VERSION,
        "weixin": "1",
        "time": ts,
    }
    if extra:
        params.update(extra)
    params["sign"] = make_sign({k: v for k, v in params.items() if k != "sign"})
    return params


def android_headers(cookie: str, referer: str = "https://m.smzdm.com/") -> dict:
    return {
        "Host": "user-api.smzdm.com",
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": cookie,
        "User-Agent": ANDROID_UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh-Hans;q=0.9",
        "Referer": referer,
    }


class Result:
    def __init__(self, status: str, message: str, detail: dict | None = None):
        self.status = status
        self.message = message
        self.detail = detail or {}

    @property
    def ok(self) -> bool:
        return self.status in ("SUCCESS", "ALREADY")


def nickname_from_cookie(cookie: str) -> str:
    """从 Cookie 的 user 字段解析昵称，格式：user=昵称|用户ID"""
    import urllib.parse

    for part in cookie.split(";"):
        part = part.strip()
        if not part.startswith("user="):
            continue
        val = part[5:]
        try:
            val = urllib.parse.unquote(val)
        except Exception:
            pass
        if "|" in val:
            return val.split("|", 1)[0].strip()
        return val.strip()
    return ""


def fetch_user_info(cookie: str) -> dict:
    """通过网页端个人中心判断 Cookie 是否有效"""
    url = "https://zhiyou.smzdm.com/user/"
    headers = {
        "Cookie": cookie,
        "User-Agent": WEB_UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.smzdm.com/",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
    except Exception as exc:
        return {"valid": None, "error": f"{type(exc).__name__}: {exc}"}

    text = resp.text
    if resp.status_code != 200:
        return {"valid": None, "error": f"HTTP {resp.status_code}"}

    # 未登录通常会被重定向到登录页或页面出现登录引导
    if "登录" in text and "user_name" not in text and "个人中心" not in text:
        return {"valid": False, "error": "页面提示登录，Cookie 已失效"}

    # 尝试提取昵称
    import re

    name = ""
    for pattern in (
        r'"nickname"\s*:\s*"([^"]+)"',
        r'id="J_user_name"[^>]*>([^<]+)<',
        r'class="user_name"[^>]*>([^<]+)<',
    ):
        m = re.search(pattern, text)
        if m:
            name = m.group(1).strip()
            break

    has_login_hint = ("退出" in text) or ("我的消息" in text) or bool(name)
    return {"valid": has_login_hint, "nickname": name or "（未解析到昵称）"}


def get_token(cookie: str) -> tuple[str | None, dict]:
    """获取签到所需 token"""
    url = "https://user-api.smzdm.com/robot/token"
    params = build_params(cookie)
    try:
        resp = requests.post(
            url, headers=android_headers(cookie), data=params, timeout=20
        )
    except Exception as exc:
        return None, {"error": f"{type(exc).__name__}: {exc}"}

    try:
        payload = resp.json()
    except Exception:
        return None, {"error": f"非 JSON 响应: {resp.text[:200]}"}

    if str(payload.get("error_code")) != "0":
        return None, payload

    data = payload.get("data") or {}
    token = data.get("token") or data.get("checkin_token")
    if not token:
        return None, {"error": "响应中无 token 字段", "raw": payload}
    return token, payload


def do_checkin(cookie: str, token: str) -> Result:
    """执行签到"""
    url = "https://user-api.smzdm.com/checkin"
    params = build_params(cookie, extra={"token": token})
    try:
        resp = requests.post(
            url, headers=android_headers(cookie, "https://m.smzdm.com/"), data=params,
            timeout=20,
        )
    except Exception as exc:
        return Result("NETWORK", f"请求异常：{type(exc).__name__}: {exc}")

    try:
        payload = resp.json()
    except Exception:
        return Result(
            "UNKNOWN", f"非 JSON 响应（HTTP {resp.status_code}）：{resp.text[:200]}"
        )

    code = str(payload.get("error_code", ""))
    msg = payload.get("error_msg", "") or ""
    data = payload.get("data") or {}

    if code == "0":
        parts = []
        for key, label in (
            ("daily_num", "签到天数"),
            ("checkin_num", "连续签到"),
            ("point", "积分"),
            ("gold", "金币"),
            ("exp", "经验"),
        ):
            if key in data and data[key] not in (None, "", 0):
                parts.append(f"{label} {data[key]}")
        detail = ", ".join(parts) if parts else "签到成功"
        return Result("SUCCESS", detail, payload)

    # 已签到
    if "已签到" in msg or "已经签到" in msg or code in ("10001",):
        return Result("ALREADY", msg or "今日已签到", payload)

    # 登录态失效
    if code == "11111" or "登录" in msg:
        return Result("COOKIE_INVALID", msg or "Cookie 已失效", payload)

    # 风控
    if "频繁" in msg or "稍后" in msg or code in ("10002", "10003"):
        return Result("RISK", msg or "请求过于频繁", payload)

    return Result("UNKNOWN", f"[{code}] {msg or '未知错误'}", payload)


def pushplus_notify(token: str, title: str, content: str) -> bool:
    url = "https://www.pushplus.plus/send"
    body = {
        "token": token,
        "title": title,
        "content": content,
        "template": "html",
    }
    try:
        resp = requests.post(url, json=body, timeout=20)
        payload = resp.json()
        ok = str(payload.get("code")) == "200"
        log(f"PushPlus 推送{'成功' if ok else '失败'}：{payload.get('msg', '')}")
        return ok
    except Exception as exc:
        log(f"PushPlus 推送异常：{type(exc).__name__}: {exc}")
        return False


def main() -> int:
    log("==== 什么值得买每日签到 开始 ====")

    cookie = (os.getenv("SMZDM_COOKIE") or "").strip()
    if not cookie:
        log("错误：未配置 SMZDM_COOKIE 环境变量")
        return EXIT_COOKIE_INVALID

    # 规范化 Cookie：去掉可能混入的换行、首尾引号
    cookie = cookie.strip().strip('"').strip("'").replace("\n", "").replace("\r", "")
    log(f"Cookie 长度 {len(cookie)} 字符")

    pushplus_token = (os.getenv("PUSHPLUS_TOKEN") or "").strip()

    # 步骤 1：校验登录态
    log("步骤 1/3：校验登录态 ...")
    info = fetch_user_info(cookie)
    if info.get("valid") is False:
        msg = info.get("error", "Cookie 已失效")
        log(f"  ✗ {msg}")
        if pushplus_token:
            pushplus_notify(
                pushplus_token,
                "【什么值得买】签到失败：Cookie 已失效",
                f"<p>签到未能执行。</p><p>原因：{msg}</p>"
                f"<p>请重新登录 smzdm.com 获取 Cookie，并在 GitHub 仓库 Secrets 中更新 "
                f"<code>SMZDM_COOKIE</code>。</p>"
                f"<p>时间：{now_cst()}</p>",
            )
        return EXIT_COOKIE_INVALID

    nickname = nickname_from_cookie(cookie) or info.get("nickname", "")
    log(f"  ✓ 登录态有效  {nickname}")

    time.sleep(random.uniform(1.5, 3.5))

    # 步骤 2：获取 token
    log("步骤 2/3：获取签到 token ...")
    token, token_payload = get_token(cookie)
    if not token:
        err = token_payload.get("error_msg") or token_payload.get("error") or token_payload
        log(f"  ✗ 获取 token 失败：{err}")
        if str(token_payload.get("error_code")) == "11111" and pushplus_token:
            pushplus_notify(
                pushplus_token,
                "【什么值得买】签到失败：Cookie 已失效",
                f"<p>获取 token 返回未登录，Cookie 已失效。</p>"
                f"<p>请更新 <code>SMZDM_COOKIE</code>。</p><p>时间：{now_cst()}</p>",
            )
        return EXIT_COOKIE_INVALID if str(
            token_payload.get("error_code")
        ) == "11111" else EXIT_NETWORK
    log("  ✓ token 获取成功")

    time.sleep(random.uniform(1.0, 2.5))

    # 步骤 3：签到
    log("步骤 3/3：执行签到 ...")
    result = do_checkin(cookie, token)
    log(f"  结果：{result.status} — {result.message}")

    # 通知
    if pushplus_token:
        emoji = {"SUCCESS": "✅", "ALREADY": "✅", "COOKIE_INVALID": "⚠️"}.get(
            result.status, "❌"
        )
        title = f"【什么值得买】{emoji} {result.status}"
        body = (
            f"<p><b>账号：</b>{nickname}</p>"
            f"<p><b>结果：</b>{result.message}</p>"
            f"<p><b>状态码：</b>{result.status}</p>"
            f"<p><b>时间：</b>{now_cst()}</p>"
        )
        if result.status == "COOKIE_INVALID":
            body += "<p style='color:#c00'>请更新 GitHub Secrets 中的 SMZDM_COOKIE</p>"
        pushplus_notify(pushplus_token, title, body)

    log("==== 结束 ====")

    if result.status in ("SUCCESS", "ALREADY"):
        return EXIT_OK
    if result.status == "COOKIE_INVALID":
        return EXIT_COOKIE_INVALID
    if result.status == "NETWORK":
        return EXIT_NETWORK
    return EXIT_UNKNOWN


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        log(f"未捕获异常：{type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
        sys.exit(EXIT_UNKNOWN)
