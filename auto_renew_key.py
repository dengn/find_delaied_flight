#!/usr/bin/env python3
"""
飞常准 API Key 自动续杯工具
自动注册新账号 → 邮箱激活 → 创建 API Key，实现无限续杯。
使用 Guerrilla Mail 临时邮箱服务接收激活邮件。

注意：飞常准会按域名封禁常见的一次性邮箱（mail.tm 的 uberip.com、
guerrillamail.com、sharklasers.com、spam4.me 等均已被封）。
MAIL_DOMAINS 里按顺序放的是当前仍可通过注册校验的别名域名，
注册被拒时会自动换下一个，无需改代码。
Guerrilla Mail 的所有域名共用同一个收件箱，因此换域名不影响收信。
"""

import html
import random
import re
import string
import sys
import time
import urllib.parse

import requests

VARIFLIGHT_BASE = "https://mcp.variflight.com"
GUERRILLA_BASE = "https://api.guerrillamail.com/ajax.php"

# 候选发信域名，按顺序尝试；被飞常准拒绝时自动降级到下一个
MAIL_DOMAINS = ["grr.la", "pokemail.net"]

# Guerrilla Mail 要求请求带正常 UA
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class EmailDomainRejected(RuntimeError):
    """飞常准拒绝了该邮箱域名（一次性邮箱黑名单），可换域名重试"""


def open_mailbox() -> tuple[requests.Session, str, str]:
    """打开临时邮箱会话，返回 (session, sid_token, 邮箱本地名)"""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    resp = session.get(GUERRILLA_BASE,
                       params={"f": "get_email_address", "lang": "en"},
                       timeout=15)
    resp.raise_for_status()
    sid = resp.json()["sid_token"]

    # 自定义本地名，避免用默认随机地址（便于拼接任意别名域名）
    local = "vfbot" + "".join(
        random.choices(string.ascii_lowercase + string.digits, k=10))
    resp = session.get(GUERRILLA_BASE,
                       params={"f": "set_email_user", "email_user": local,
                               "lang": "en", "sid_token": sid},
                       timeout=15)
    resp.raise_for_status()
    sid = resp.json().get("sid_token", sid)

    return session, sid, local


def register_variflight(email: str) -> tuple[str, str]:
    """在飞常准注册新账号，返回 (username, password)"""
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    username = f"bot_{rand}"
    password = "FlightBot@2026safe"

    resp = requests.post(f"{VARIFLIGHT_BASE}/api/v1/platform/auth/register",
                         json={"username": username, "email": email,
                               "password": password}, timeout=15)
    data = resp.json()
    if data.get("code") != 200:
        # 422 且错误定位到 email 字段 = 域名被拉黑，换个域名还有戏
        for err in data.get("errors") or []:
            if "email" in (err.get("loc") or []):
                raise EmailDomainRejected(err.get("msg", "邮箱域名被拒绝"))
        raise RuntimeError(f"注册失败: {data.get('message')}")

    return username, password


def wait_for_activation_code(session: requests.Session, sid: str,
                             max_wait: int = 120) -> tuple[str, str]:
    """轮询邮箱等待激活邮件，返回 (email, code)"""
    start = time.time()
    seen: set[str] = set()

    while time.time() - start < max_wait:
        resp = session.get(GUERRILLA_BASE,
                           params={"f": "get_email_list", "offset": 0,
                                   "sid_token": sid}, timeout=15)
        for msg in resp.json().get("list") or []:
            mail_id = str(msg.get("mail_id"))
            if mail_id in seen:
                continue
            seen.add(mail_id)

            detail = session.get(GUERRILLA_BASE,
                                 params={"f": "fetch_email",
                                         "email_id": mail_id,
                                         "sid_token": sid}, timeout=15)
            # 邮件正文是 HTML，激活链接藏在 <a href> 里
            body = html.unescape(detail.json().get("mail_body", ""))
            match = re.search(r"activate\?[^\s\"'<>\)]+", body)
            if not match:
                continue

            query = urllib.parse.urlparse("?" + match.group(0).split("?", 1)[1])
            params = urllib.parse.parse_qs(query.query)
            email = params.get("email", [""])[0]
            code = params.get("code", [""])[0]
            if email and code:
                return email, code

        time.sleep(3)

    raise RuntimeError(f"等待激活邮件超时 ({max_wait}s)")


def activate_account(email: str, code: str):
    """激活账号"""
    resp = requests.get(
        f"{VARIFLIGHT_BASE}/api/v1/platform/auth/activate",
        params={"email": email, "code": code}, timeout=15)
    data = resp.json()
    if data.get("code") != 200:
        raise RuntimeError(f"激活失败: {data.get('message')}")


def login_variflight(username: str, password: str) -> str:
    """登录，返回 access_token"""
    resp = requests.post(f"{VARIFLIGHT_BASE}/api/v1/platform/auth/login",
                         data={"username": username, "password": password},
                         timeout=15)
    data = resp.json()
    if data.get("code") != 200:
        raise RuntimeError(f"登录失败: {data.get('message')}")
    return data["access_token"]


def create_api_key(token: str) -> str:
    """创建 API Key，返回 key 字符串"""
    resp = requests.post(f"{VARIFLIGHT_BASE}/api/v1/platform/api-keys/",
                         headers={"Authorization": f"Bearer {token}",
                                  "Content-Type": "application/json"},
                         json={"name": "auto"}, timeout=15)
    data = resp.json()
    if data.get("code") != 200:
        raise RuntimeError(f"创建 key 失败: {data.get('message')}")
    return data["data"]["api_key"]


def check_balance(token: str) -> int:
    """通过用户信息接口检查余额（不消耗 API 调用次数）"""
    resp = requests.get(f"{VARIFLIGHT_BASE}/api/v1/platform/auth/me",
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=15)
    data = resp.json()
    if data.get("code") == 200:
        return data["data"].get("total_balance", 0)
    return 0


def obtain_new_key(verbose: bool = True) -> str:
    """完整流程：获取一个新的可用 API Key"""
    def log(msg):
        if verbose:
            print(f"  {msg}", file=sys.stderr)

    log("[1/6] 创建临时邮箱...")
    session, sid, local = open_mailbox()

    log("[2/6] 注册飞常准账号...")
    username = vf_pwd = None
    rejected = []
    for domain in MAIL_DOMAINS:
        email = f"{local}@{domain}"
        try:
            username, vf_pwd = register_variflight(email)
            log(f"       邮箱: {email}")
            log(f"       用户: {username}")
            break
        except EmailDomainRejected as e:
            rejected.append(domain)
            log(f"       域名 {domain} 被拒绝 ({e})，尝试下一个...")

    if username is None:
        raise RuntimeError(
            f"所有邮箱域名均被飞常准拒绝: {', '.join(rejected)}。"
            "需要在 MAIL_DOMAINS 中补充新的可用域名。")

    log("[3/6] 等待激活邮件...")
    act_email, act_code = wait_for_activation_code(session, sid)

    log("[4/6] 激活账号...")
    activate_account(act_email, act_code)
    log("       激活成功")

    log("[5/6] 登录并创建 API Key...")
    token = login_variflight(username, vf_pwd)
    api_key = create_api_key(token)

    log("[6/6] 等待额度到账...")
    # 新账号额度到账可能有延迟，使用递增等待：5s, 10s, 15s, 20s, 25s, 30s, 30s, 30s, 30s, 30s
    # 总等待时间最长约 225 秒（~3.75 分钟）
    wait_intervals = [5, 10, 15, 20, 25, 30, 30, 30, 30, 30]
    for i, wait in enumerate(wait_intervals):
        balance = check_balance(token)
        if balance > 0:
            log(f"       余额: {balance} ✓")
            return api_key
        if i < len(wait_intervals) - 1:
            log(f"       余额暂为 0，{wait}s 后第 {i+2} 次检查...")
        time.sleep(wait)

    # 最后一次检查
    balance = check_balance(token)
    if balance > 0:
        log(f"       余额: {balance} ✓")
    else:
        log("       ⚠ 等待约 4 分钟后余额仍为 0，Key 可能暂时不可用")

    return api_key


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="飞常准 API Key 自动续杯 — 自动注册并获取新 Key")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="安静模式，只输出 key")
    args = parser.parse_args()

    try:
        key = obtain_new_key(verbose=not args.quiet)
        if args.quiet:
            print(key)
        else:
            print(f"\n  新 API Key: {key}\n")
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
