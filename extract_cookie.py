"""
从浏览器 DevTools 的 "Copy as cURL" 输出中提取 Cookie
同时也支持直接粘贴裸 Cookie 字符串。

用法：
    python extract_cookie.py <文件路径>
    python extract_cookie.py --paste      # 交互式粘贴
或直接修改 INPUT 变量
"""
import re
import sys


def extract_cookie(text: str) -> str | None:
    text = text.strip()
    if not text:
        return None

    # 情况 1：cURL 命令中包含 -H 'cookie: xxx' 或 -b 'xxx'
    # 兼容 bash (-H 'Cookie: ...') 与 cmd (-H "Cookie: ...")
    for pattern in (
        r"""-H\s+['"]cookie:\s*([^'"]+)['"]""",
        r"""--header\s+['"]cookie:\s*([^'"]+)['"]""",
        r"""-b\s+['"]([^'"]+)['"]""",
        r"""--cookie\s+['"]?([^'"\s]+)['"]?""",
    ):
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()

    # 情况 2：直接粘贴的裸 Cookie（形如 a=b; c=d; ...）
    if "=" in text and ";" in text and "curl" not in text.lower():
        # 去掉可能存在的 "Cookie:" 前缀
        cleaned = re.sub(r"^\s*cookie:\s*", "", text, flags=re.IGNORECASE).strip()
        return cleaned

    return None


def summarize(cookie: str) -> dict:
    """解析关键字段，判断登录态是否齐全"""
    pairs = {}
    for part in cookie.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        pairs[k.strip()] = v.strip()

    keys = set(pairs.keys())
    # smzdm 登录态关键字段
    critical = {
        "sess": "会话主凭证",
        "sess_uid": "用户 ID",
        "user_login_sess": "网页登录态",
        "device_id": "设备标识",
        "smzdm_id": "站点用户标识",
    }
    present = {k: v for k, v in critical.items() if k in keys}

    return {
        "total_fields": len(pairs),
        "length": len(cookie),
        "critical_present": present,
        "missing_critical": [k for k in ("sess", "sess_uid") if k not in keys],
        "all_keys": sorted(keys),
    }


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--paste":
        print("请粘贴 cURL 命令或 Cookie，粘贴后按 Ctrl+Z 回车（Linux/Mac 为 Ctrl+D）：")
        text = sys.stdin.read()
    elif len(sys.argv) >= 2:
        with open(sys.argv[1], encoding="utf-8", errors="ignore") as f:
            text = f.read()
    else:
        print("用法: python extract_cookie.py <文件> | --paste")
        sys.exit(1)

    cookie = extract_cookie(text)
    if not cookie:
        print("✗ 未能从输入中识别出 Cookie")
        sys.exit(1)

    info = summarize(cookie)
    print(f"✓ 已提取 Cookie，长度 {info['length']} 字符")
    print(f"  字段总数：{info['total_fields']}")
    print(f"  关键字段：{info['critical_present']}")
    if info["missing_critical"]:
        print(f"  ⚠ 缺失关键字段：{info['missing_critical']}")
    else:
        print("  ✓ 关键登录字段齐全")
    print(f"  全部字段：{', '.join(info['all_keys'])}")
    print()
    print("--- 前 80 字符预览 ---")
    print(cookie[:80] + ("..." if len(cookie) > 80 else ""))
