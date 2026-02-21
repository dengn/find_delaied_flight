#!/usr/bin/env python3
"""
飞常准 API Key 自动续杯工具
自动注册新账号 → 邮箱激活 → 创建 API Key，实现无限续杯。
使用 mail.tm 临时邮箱服务接收激活邮件。
"""

import json
import random
import string
import sys
import time

import requests

VARIFLIGHT_BASE = "https://mcp.variflight.com"
MAILTM_BASE = "https://api.mail.tm"


def get_mail_domain() -> str:
    """获取 mail.tm 当前可用域名"""
    resp = requests.get(f"{MAILTM_BASE}/domains", timeout=15)
    resp.raise_for_status()
    data = resp.json()
    # 兼容两种响应格式: 列表 或 hydra collection
    if isinstance(data, list):
        domains = data
    else:
        domains = data.get("hydra:member", [])
    for d in domains:
        if d.get("isActive"):
            return d["domain"]
    raise RuntimeError("mail.tm 没有可用域名")


def create_temp_email(domain: str) -> tuple[str, str, str]:
    """创建临时邮箱，返回 (地址, 密码, token)"""
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    addr = f"vfbot_{rand}@{domain}"
    pwd = "TempMail@2026x"

    resp = requests.post(f"{MAILTM_BASE}/accounts",
                         json={"address": addr, "password": pwd}, timeout=15)
    resp.raise_for_status()

    # 获取 token
    resp = requests.post(f"{MAILTM_BASE}/token",
                         json={"address": addr, "password": pwd}, timeout=15)
    resp.raise_for_status()
    token = resp.json()["token"]
    return addr, pwd, token


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
        raise RuntimeError(f"注册失败: {data.get('message')}")

    return username, password


def wait_for_activation_code(mail_token: str, max_wait: int = 60) -> tuple[str, str]:
    """轮询邮箱等待激活邮件，返回 (email, code)"""
    headers = {"Authorization": f"Bearer {mail_token}"}
    start = time.time()

    while time.time() - start < max_wait:
        resp = requests.get(f"{MAILTM_BASE}/messages",
                            headers=headers, timeout=15)
        raw = resp.json()
        # 兼容列表或 hydra collection
        messages = raw if isinstance(raw, list) else raw.get("hydra:member", [])
        if len(messages) > 0:
            # 读取第一封邮件
            msg_id = messages[0]["id"]
            resp = requests.get(f"{MAILTM_BASE}/messages/{msg_id}",
                                headers=headers, timeout=15)
            text = resp.json().get("text", "")
            # 从文本中提取激活链接
            for line in text.split("\n"):
                if "activate?" in line:
                    line = line.strip()
                    # 解析 email 和 code 参数
                    import urllib.parse
                    parsed = urllib.parse.urlparse(line)
                    params = urllib.parse.parse_qs(parsed.query)
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

    log("[1/6] 获取临时邮箱域名...")
    domain = get_mail_domain()

    log(f"[2/6] 创建临时邮箱 (*@{domain})...")
    email, mail_pwd, mail_token = create_temp_email(domain)
    log(f"       邮箱: {email}")

    log("[3/6] 注册飞常准账号...")
    username, vf_pwd = register_variflight(email)
    log(f"       用户: {username}")

    log("[4/6] 等待激活邮件...")
    act_email, act_code = wait_for_activation_code(mail_token)
    activate_account(act_email, act_code)
    log("       激活成功")

    log("[5/6] 登录并创建 API Key...")
    token = login_variflight(username, vf_pwd)
    api_key = create_api_key(token)

    log("[6/6] 等待额度到账...")
    for i in range(6):
        balance = check_balance(token)
        if balance > 0:
            log(f"       余额: {balance} ✓")
            break
        time.sleep(3)
    else:
        log(f"       余额仍为 0，可能需要更长时间到账")

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
