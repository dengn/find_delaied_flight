#!/usr/bin/env python3
"""
南航航变机会检测器
检测「前序飞机铁定来不及、但航司尚未发布航变通知」的南航航班。

核心逻辑：
  1. 扫描各枢纽的进港航班，找到前序飞机严重延误的情况
  2. 用飞机注册号匹配该飞机的后续出港CZ航班
  3. 数学计算：前序预计到达 + 最小过站时间 > 后续计划起飞 → 铁定延误
  4. 确认后续航班状态仍为"计划"（航司未通知航变）
  5. 确认距出发还有足够时间（可买里程票）

辅助信息（仅展示，不参与核心判定）：
  - 出发/到达机场天气
  - 机场整体延误态势
  - 飞常准AI预测
  - 历史准点率

使用飞常准 (VariFlight) API。
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from auto_renew_key import obtain_new_key

# ============================================================
# 配置
# ============================================================

API_URL = "https://mcp.variflight.com/api/v1/mcp/data"

# 北京时间 UTC+8
BJT = timezone(timedelta(hours=8))


def beijing_now() -> datetime:
    """返回当前北京时间（UTC+8），返回 naive datetime 以兼容 API 数据"""
    return datetime.now(BJT).replace(tzinfo=None)

# 南航主要枢纽及高频航线目的地
CZ_HUBS = {
    "CAN": [  # 广州白云 —— 南航最大枢纽
        "PKX", "PVG", "SHA", "CTU", "TFU", "CKG", "WUH", "CSX",
        "NKG", "HGH", "XIY", "KMG", "URC", "DLC", "SHE", "TAO",
        "XMN", "HAK", "SYX", "KWE", "NNG", "CGO", "TNA", "TSN",
        "HRB", "SJW", "ZUH", "LHW", "WNZ", "FOC", "KHN", "HET",
        "INC", "XNN", "MDG",
    ],
    "PKX": [  # 北京大兴 —— 南航北方枢纽
        "CAN", "SZX", "CTU", "TFU", "CKG", "WUH", "CSX", "HGH",
        "KMG", "URC", "DLC", "SHE", "XIY", "HAK", "SYX", "KWE",
        "NNG", "CGO", "XMN", "NKG",
    ],
    "URC": [  # 乌鲁木齐 —— 南航西部枢纽
        "CAN", "PKX", "CTU", "CSX", "XIY", "CGO", "WUH", "KMG",
        "CKG", "HGH", "NKG",
    ],
    "SZX": [  # 深圳 —— 南航重要基地
        "PKX", "CTU", "TFU", "CKG", "WUH", "CSX", "NKG", "XIY",
        "CGO", "DLC", "SHE", "TAO", "HRB",
    ],
}

# 南航二线基地及常见航线（天气智能扫描时动态启用）
CZ_SECONDARY_HUBS = {
    "WUH": ["CAN", "PKX", "SZX", "PVG", "HGH", "CKG", "CSX", "KMG", "XIY", "HAK"],
    "CSX": ["CAN", "PKX", "SZX", "PVG", "HGH", "CKG", "KMG", "NKG", "XIY", "HAK"],
    "DLC": ["CAN", "PKX", "SZX", "PVG", "CSX", "CKG", "WUH", "HGH", "XIY"],
    "CGO": ["CAN", "PKX", "SZX", "PVG", "HGH", "KMG", "CSX", "HAK", "XMN"],
    "NKG": ["CAN", "PKX", "SZX", "CKG", "CSX", "WUH", "KMG", "XIY", "HAK", "SYX"],
    "HGH": ["CAN", "PKX", "SZX", "CKG", "CSX", "WUH", "KMG", "XIY", "HAK"],
    "KMG": ["CAN", "PKX", "SZX", "PVG", "HGH", "CKG", "CSX", "NKG", "WUH"],
    "XIY": ["CAN", "PKX", "SZX", "PVG", "HGH", "CKG", "CSX", "KMG", "WUH"],
    "CKG": ["CAN", "PKX", "SZX", "PVG", "HGH", "NKG", "CSX", "KMG", "WUH", "HAK"],
    "HAK": ["CAN", "PKX", "SZX", "PVG", "HGH", "CKG", "CSX", "WUH", "CGO", "NKG"],
    "SHE": ["CAN", "PKX", "SZX", "PVG", "HGH", "CKG", "CSX", "KMG", "WUH"],
    "TAO": ["CAN", "PKX", "SZX", "PVG", "HGH", "CKG", "CSX", "KMG", "WUH"],
    "NNG": ["CAN", "PKX", "SZX", "PVG", "HGH", "CKG", "KMG", "HAK"],
    "KWE": ["CAN", "PKX", "SZX", "PVG", "HGH", "CKG", "CSX", "KMG", "NKG"],
    "HRB": ["CAN", "PKX", "SZX", "PVG", "HGH", "CKG", "CSX", "XIY"],
    "FOC": ["CAN", "PKX", "SZX", "PVG", "CKG", "CSX", "WUH", "NKG"],
    "SYX": ["CAN", "PKX", "SZX", "PVG", "HGH", "CKG", "CSX", "WUH", "CGO"],
    "TNA": ["CAN", "PKX", "SZX", "PVG", "HGH", "CKG", "CSX", "KMG"],
    "KHN": ["CAN", "PKX", "SZX", "PVG", "HGH", "CKG", "CSX", "KMG"],
    "LHW": ["CAN", "PKX", "SZX", "PVG", "CKG", "CSX", "XIY", "URC"],
    "HET": ["CAN", "PKX", "SZX", "PVG", "HGH", "CKG", "CSX"],
    "INC": ["CAN", "PKX", "SZX", "PVG", "CKG", "CSX", "XIY"],
    "WNZ": ["CAN", "PKX", "SZX", "HGH", "CKG", "CSX", "KMG"],
    "XMN": ["CAN", "PKX", "SZX", "PVG", "CKG", "CSX", "WUH", "NKG"],
    "TSN": ["CAN", "SZX", "PVG", "HGH", "CKG", "CSX", "KMG"],
}

# 易受天气影响的小型/高原/偏远机场（纳入天气预扫描范围）
WEATHER_SENSITIVE_AIRPORTS = [
    "LJG", "JHG", "KWL", "YIH", "ENH", "TEN", "DIG", "ZAT",  # 西南山区
    "KRL", "AKU", "HMI", "KRY", "HTN",                         # 新疆沙漠
    "MIG", "JZH", "DLU", "TCZ", "BPX", "LXA",                  # 高原机场
    "MDG", "YNJ", "YNT", "WEH", "RIZ",                         # 东北/沿海
    "ZHA", "BHY", "AEB", "HPG", "JIU",                          # 华南/华中小场
]

# 天气风险关键词
_WEATHER_HIGH_RISK = {"雷暴", "暴雨", "暴雪", "大暴雨", "冻雨", "沙尘暴", "大风"}
_WEATHER_MED_RISK = {"大雨", "大雪", "中雪", "雾", "浓雾", "中雨", "扬沙", "霾"}
_WEATHER_LOW_RISK = {"小雨", "小雪", "阵雨", "雨夹雪", "小到中雨", "小到中雪"}


def score_weather_risk(weather_data: dict) -> int:
    """为机场天气评估延误风险分数 (0-100)"""
    if not weather_data:
        return 0

    score = 0
    current = weather_data.get("current", {})
    weather_type = str(current.get("Type", ""))

    # 天气类型评分
    for kw in _WEATHER_HIGH_RISK:
        if kw in weather_type:
            score += 60
            break
    else:
        for kw in _WEATHER_MED_RISK:
            if kw in weather_type:
                score += 40
                break
        else:
            for kw in _WEATHER_LOW_RISK:
                if kw in weather_type:
                    score += 15
                    break

    # 能见度评分
    try:
        vis = int(current.get("Visib", 9999))
        if vis < 500:
            score += 40
        elif vis < 1000:
            score += 30
        elif vis < 3000:
            score += 20
        elif vis < 5000:
            score += 10
    except (ValueError, TypeError):
        pass

    # 风力评分
    wind_str = str(current.get("WindPower", ""))
    m = re.search(r"(\d+)", wind_str)
    if m:
        wind_val = int(m.group(1))
        if wind_val >= 10:
            score += 20
        elif wind_val >= 7:
            score += 10

    return min(score, 100)


def build_weather_smart_hubs(api, base_hubs: dict, verbose: bool = True) -> tuple:
    """
    基于天气预判构建智能扫描列表。
    返回 (augmented_hubs, weather_report) 元组。
    weather_report 是 {airport: (score, weather_summary)} 字典，供汇总邮件使用。
    """
    # 收集所有需要查天气的机场
    airports_to_check = set()
    for hub, dests in base_hubs.items():
        airports_to_check.add(hub)
        airports_to_check.update(dests)
    for hub, dests in CZ_SECONDARY_HUBS.items():
        airports_to_check.add(hub)
        airports_to_check.update(dests)
    airports_to_check.update(WEATHER_SENSITIVE_AIRPORTS)

    if verbose:
        print(f"\n  [天气预扫描] 正在检查 {len(airports_to_check)} 个机场天气...",
              file=sys.stderr)

    # 批量获取天气并评分
    weather_scores = {}  # {airport: (score, weather_data)}
    for airport in sorted(airports_to_check):
        weather = api.get_airport_weather(airport)
        sc = score_weather_risk(weather)
        if sc > 0:
            weather_scores[airport] = (sc, weather)

    # 生成可读报告
    weather_report = {}
    for ap, (sc, wd) in weather_scores.items():
        cur = wd.get("current", {})
        summary = (f"{cur.get('Type', '?')} "
                   f"能见度{cur.get('Visib', '?')}m "
                   f"{cur.get('WindDirection', '')}{cur.get('WindPower', '')}")
        weather_report[ap] = (sc, summary)

    # 打印天气分析
    if verbose:
        bad = sorted(weather_scores.items(), key=lambda x: -x[1][0])
        if bad:
            print("  [天气预扫描] 恶劣天气机场 (风险分>=20):", file=sys.stderr)
            for code, (sc, wd) in bad:
                if sc < 20:
                    break
                cur = wd.get("current", {})
                print(f"    {code}: {cur.get('Type','?')} "
                      f"能见度{cur.get('Visib','?')}m  "
                      f"风险分{sc}", file=sys.stderr)
        else:
            print("  [天气预扫描] 当前各机场天气良好", file=sys.stderr)

    # 构建扩展扫描列表
    smart_hubs = dict(base_hubs)  # 始终包含主枢纽

    added_count = 0
    max_add = 6  # 最多额外添加 6 个二线枢纽，控制 API 消耗

    # 按天气风险降序排列二线枢纽
    secondary_ranked = []
    for hub, dests in CZ_SECONDARY_HUBS.items():
        if hub in smart_hubs:
            continue
        hub_score = weather_scores.get(hub, (0,))[0]
        # 计算目的地中有多少天气差的
        bad_dest_scores = [weather_scores.get(d, (0,))[0] for d in dests]
        bad_dest_count = sum(1 for s in bad_dest_scores if s >= 30)
        # 综合评分：自身天气 + 目的地天气影响
        combined = hub_score + bad_dest_count * 15
        if combined >= 30:
            secondary_ranked.append((hub, dests, combined, hub_score, bad_dest_count))

    secondary_ranked.sort(key=lambda x: -x[2])

    for hub, dests, combined, hub_score, bad_dest_count in secondary_ranked:
        if added_count >= max_add:
            break
        smart_hubs[hub] = dests
        added_count += 1
        reason = []
        if hub_score >= 30:
            reason.append(f"本场天气差(分{hub_score})")
        if bad_dest_count > 0:
            reason.append(f"{bad_dest_count}个目的地天气差")
        if verbose:
            print(f"  [智能扫描] +{hub}  {'，'.join(reason)}", file=sys.stderr)

    # 对所有枢纽，将天气差的目的地排到前面（优先扫描）
    for hub in list(smart_hubs.keys()):
        dests = smart_hubs[hub]
        bad_first = sorted(dests,
                           key=lambda d: -weather_scores.get(d, (0,))[0])
        smart_hubs[hub] = bad_first

    if verbose:
        print(f"  [智能扫描] 最终扫描: {len(smart_hubs)} 个枢纽 "
              f"(主站{len(base_hubs)} + 天气新增{added_count})\n", file=sys.stderr)

    return smart_hubs, weather_report


# 最小过站时间（分钟）
MIN_TURNAROUND_NARROW = 45   # 窄体机
MIN_TURNAROUND_WIDE = 70     # 宽体机

# 宽体机型前缀
WIDEBODY_TYPES = {"A33", "A34", "A35", "A38", "B74", "B77", "B78", "B76"}

# 前序航班必须延误超过此阈值（分钟），才认为"明显延误"
SIGNIFICANT_DELAY_MINUTES = 30

# 后续航班距现在至少要有多少分钟，才有买票窗口
MIN_BOOKING_WINDOW_MINUTES = 120  # 2小时

# 请求间隔（秒）
REQUEST_INTERVAL = 0.6


# ============================================================
# API 调用
# ============================================================

class VariFlightAPI:
    def __init__(self, api_key: str, interval: float = REQUEST_INTERVAL,
                 auto_renew: bool = False):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "X-VARIFLIGHT-KEY": api_key,
            "Content-Type": "application/json",
        })
        self._interval = interval
        self._auto_renew = auto_renew
        self._renew_count = 0
        self._max_renew = 3
        self._last_call = 0.0
        self.call_count = 0
        self.error_count = 0

    def _call(self, endpoint: str, params: dict):
        """调用飞常准 API，带限速和重试"""
        body = {"endpoint": endpoint, "params": params}
        max_attempts = 4
        attempt = 0
        while attempt < max_attempts:
            now = time.monotonic()
            wait = self._interval - (now - self._last_call)
            if wait > 0:
                time.sleep(wait)

            try:
                self._last_call = time.monotonic()
                self.call_count += 1
                resp = self.session.post(API_URL, json=body, timeout=30)

                if resp.status_code == 403:
                    try:
                        err_data = resp.json()
                        if err_data.get("message") == "Insufficient balance":
                            if (self._auto_renew
                                    and self._renew_count < self._max_renew):
                                self._renew_count += 1
                                print(f"\n  [续杯 {self._renew_count}/{self._max_renew}]"
                                      f" API 余额不足，自动获取新 Key...",
                                      file=sys.stderr)
                                try:
                                    new_key = obtain_new_key(verbose=True)
                                    self.api_key = new_key
                                    self.session.headers["X-VARIFLIGHT-KEY"] = new_key
                                    self._balance_warned = False
                                    print(f"  [续杯] 新 Key 已生效: {new_key[:20]}...",
                                          file=sys.stderr)
                                    # 额度到账可能有延迟，多等一会再发请求
                                    print("  [续杯] 等待 10s 确保额度生效...\n",
                                          file=sys.stderr)
                                    time.sleep(10)
                                    attempt = 0
                                    continue
                                except Exception as e:
                                    print(f"  [续杯失败] {e}", file=sys.stderr)
                            if not getattr(self, '_balance_warned', False):
                                print("\n  [错误] API 余额不足 (Insufficient balance)，"
                                      "请充值后重试。", file=sys.stderr)
                                self._balance_warned = True
                            self.error_count += 1
                            return []
                    except (json.JSONDecodeError, ValueError):
                        pass
                    backoff = 3 * (2 ** attempt)
                    if attempt < max_attempts - 1:
                        print(f"  [限速] 等待 {backoff}s 后重试...",
                              file=sys.stderr)
                        time.sleep(backoff)
                        attempt += 1
                        continue
                    else:
                        self.error_count += 1
                        return []

                resp.raise_for_status()
                data = resp.json()
                if data.get("code") == 200:
                    return data.get("data", [])
                return []

            except requests.exceptions.ConnectionError:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    attempt += 1
                    continue
                self.error_count += 1
                return []
            except (requests.RequestException, json.JSONDecodeError) as e:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    attempt += 1
                    continue
                self.error_count += 1
                print(f"  [API 错误] {endpoint} {params}: {e}", file=sys.stderr)
                return []

    def search_flights(self, dep: str, arr: str, date: str) -> list:
        result = self._call("flights", {"dep": dep, "arr": arr, "date": date})
        return result if isinstance(result, list) else []

    def get_airport_weather(self, airport_code: str) -> dict:
        """获取机场天气"""
        result = self._call("futureAirportWeather",
                            {"code": airport_code, "type": "1"})
        return result if isinstance(result, dict) else {}


# ============================================================
# 工具函数
# ============================================================

def parse_time(time_str: str) -> datetime | None:
    if not time_str or not time_str.strip():
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(time_str.strip(), fmt)
        except ValueError:
            continue
    return None


def get_best_arrival_time(flight: dict) -> datetime | None:
    """获取航班最佳到达时间估计（实际 > 预计 > AI预测 > 计划）"""
    for key in ("FlightArrtimeDate", "FlightArrtimeReadyDate",
                "VeryZhunReadyArrtimeDate", "FlightArrtimePlanDate"):
        t = parse_time(flight.get(key, ""))
        if t:
            return t
    return None


def is_widebody(ftype: str) -> bool:
    return bool(ftype) and ftype[:3].upper() in WIDEBODY_TYPES


def get_min_turnaround(ftype: str) -> int:
    return MIN_TURNAROUND_WIDE if is_widebody(ftype) else MIN_TURNAROUND_NARROW


def parse_inline_weather(weather_str: str) -> str:
    """解析内嵌天气字符串为可读文本"""
    if not weather_str:
        return ""
    parts = weather_str.split("|")
    if len(parts) >= 4:
        return f"{parts[0]} 能见度{parts[1]}m {parts[2]}"
    return weather_str


def parse_ontime_rate(rate_str: str) -> float | None:
    if not rate_str:
        return None
    try:
        return float(rate_str.replace("%", ""))
    except ValueError:
        return None


def format_airport_weather(weather_data: dict) -> str:
    """格式化机场天气API返回数据"""
    current = weather_data.get("current", {})
    if not current:
        return "未知"
    return (f"{current.get('Type', '?')} "
            f"能见度{current.get('Visib', '?')}m "
            f"{current.get('WindDirection', '')}{current.get('WindPower', '')} "
            f"{current.get('Temperature', '?')}°C")


# ============================================================
# 机场态势分析（辅助信息）
# ============================================================

def analyze_airport_situation(all_flights: list) -> dict:
    """分析机场出港航班整体延误情况"""
    total = 0
    delayed = 0
    cancelled = 0
    delay_minutes_list = []

    for fl in all_flights:
        state = fl.get("FlightState", "")
        total += 1

        if state in ("取消", "提前取消"):
            cancelled += 1
            continue

        if state == "延误":
            delayed += 1
            continue

        plan_dep = parse_time(fl.get("FlightDeptimePlanDate", ""))
        actual_dep = parse_time(fl.get("FlightDeptimeDate", ""))
        if not actual_dep:
            actual_dep = parse_time(fl.get("FlightDeptimeReadyDate", ""))
        if plan_dep and actual_dep:
            diff = (actual_dep - plan_dep).total_seconds() / 60
            if diff > 15:
                delayed += 1
                delay_minutes_list.append(diff)

    delay_rate = delayed / total if total > 0 else 0
    avg_delay = (sum(delay_minutes_list) / len(delay_minutes_list)
                 if delay_minutes_list else 0)

    return {
        "total": total,
        "delayed": delayed,
        "cancelled": cancelled,
        "delay_rate": round(delay_rate, 3),
        "avg_delay_min": round(avg_delay),
    }


# ============================================================
# 核心：前序延误铁证分析
# ============================================================

def analyze_inbound_chain(departing: dict, inbound: dict,
                          now: datetime) -> dict | None:
    """
    硬核分析：前序飞机到不了 → 后续航班铁定延误。

    必须同时满足以下条件才会产出结果：
    1. 前序航班预计到达比计划晚 >= SIGNIFICANT_DELAY_MINUTES
    2. 前序到达 + 过站时间 > 后续计划出发 (数学上来不及)
    3. 后续航班状态仍为"计划"（没发航变通知）
    4. 后续航班离现在 >= MIN_BOOKING_WINDOW_MINUTES (有时间买票)
    """
    # ---- 后续航班信息 ----
    plan_dep = parse_time(departing.get("FlightDeptimePlanDate", ""))
    if not plan_dep:
        return None

    # 条件4: 买票窗口
    minutes_until_dep = (plan_dep - now).total_seconds() / 60
    if minutes_until_dep < MIN_BOOKING_WINDOW_MINUTES:
        return None

    # 条件3: 航司未通知航变
    state = departing.get("FlightState", "")
    if state in ("延误", "取消", "提前取消", "备降", "返航", "到达", "起飞"):
        return None
    # 检查预计出发时间是否已被大幅调整（说明已通知）
    ready_dep = parse_time(departing.get("FlightDeptimeReadyDate", ""))
    if ready_dep and plan_dep:
        adjust = (ready_dep - plan_dep).total_seconds() / 60
        if adjust >= SIGNIFICANT_DELAY_MINUTES:
            return None

    # ---- 前序航班信息 ----
    inbound_plan_arr = parse_time(inbound.get("FlightArrtimePlanDate", ""))
    inbound_est_arr = get_best_arrival_time(inbound)
    if not inbound_plan_arr or not inbound_est_arr:
        return None

    # 条件1: 前序航班明显延误
    inbound_delay = (inbound_est_arr - inbound_plan_arr).total_seconds() / 60
    if inbound_delay < SIGNIFICANT_DELAY_MINUTES:
        return None

    # 条件2: 数学上来不及
    turnaround = get_min_turnaround(departing.get("ftype", ""))
    earliest_possible_dep = inbound_est_arr + timedelta(minutes=turnaround)
    dep_delay = (earliest_possible_dep - plan_dep).total_seconds() / 60
    if dep_delay <= 0:
        return None  # 过站时间够，能赶上

    # ---- 全部条件满足，构建结果 ----
    inbound_state = inbound.get("FlightState", "")

    # 前序还没飞 → 延误更确定
    if inbound_state in ("计划", "延误"):
        certainty = "极高确定性（前序尚未起飞）"
    elif inbound_state == "起飞":
        certainty = "高确定性（前序在飞，预计到达已确定）"
    else:
        certainty = "高确定性（前序已到达，过站时间不足）"

    # 飞常准AI预测
    vz_dep = parse_time(departing.get("VeryZhunReadyDeptimeDate", ""))
    vz_delay_min = 0
    if vz_dep and plan_dep:
        vz_delay_min = max(0, round(
            (vz_dep - plan_dep).total_seconds() / 60))

    result = {
        # 后续航班（我们要买票的）
        "flight": departing.get("FlightNo"),
        "route": (f"{departing.get('FlightDepcode')}"
                  f" → {departing.get('FlightArrcode')}"),
        "dep_city": (f"{departing.get('FlightDep', '')}"
                     f" → {departing.get('FlightArr', '')}"),
        "plan_departure": departing.get("FlightDeptimePlanDate"),
        "current_state": state or "计划",
        "aircraft": departing.get("AircraftNumber"),
        "aircraft_type": departing.get("ftype", ""),
        "aircraft_model": departing.get("generic", ""),
        "terminal": departing.get("FlightHTerminal", ""),

        # 延误推算
        "estimated_delay_min": round(dep_delay),
        "earliest_possible_dep": earliest_possible_dep.strftime(
            "%Y-%m-%d %H:%M"),
        "certainty": certainty,
        "minutes_until_departure": round(minutes_until_dep),

        # 前序航班（导致延误的原因）
        "inbound_flight": inbound.get("FlightNo"),
        "inbound_route": (f"{inbound.get('FlightDepcode')}"
                          f" → {inbound.get('FlightArrcode')}"),
        "inbound_state": inbound_state,
        "inbound_plan_arrival": inbound.get("FlightArrtimePlanDate"),
        "inbound_est_arrival": inbound_est_arr.strftime("%Y-%m-%d %H:%M"),
        "inbound_delay_min": round(inbound_delay),
        "inbound_delay_reason": inbound.get("DelayReason", ""),
        "min_turnaround_min": turnaround,
    }

    # 辅助信息
    ontime = parse_ontime_rate(departing.get("OntimeRate", ""))
    if ontime is not None:
        result["ontime_rate"] = f"{ontime}%"
    if vz_delay_min > 0:
        result["veryzhun_predicted_delay_min"] = vz_delay_min
    dep_wx = departing.get("DepWeather", "")
    if dep_wx:
        result["dep_weather"] = parse_inline_weather(dep_wx)
    arr_wx = departing.get("ArrWeather", "")
    if arr_wx:
        result["arr_weather"] = parse_inline_weather(arr_wx)

    return result


# ============================================================
# 主流程
# ============================================================

def run_detection(api_key: str, date: str, hubs: dict,
                  verbose: bool = False, interval: float = REQUEST_INTERVAL,
                  auto_renew: bool = False):
    """运行延误检测，返回 (hits, summary) 元组"""
    api = VariFlightAPI(api_key, interval=interval, auto_renew=auto_renew)
    now = beijing_now()
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"\n{'='*70}")
    print(f"  南航航变机会检测器")
    print(f"  检测日期: {date}")
    print(f"  运行时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  买票窗口: 距起飞 >= {MIN_BOOKING_WINDOW_MINUTES} 分钟")
    print(f"  前序延误阈值: >= {SIGNIFICANT_DELAY_MINUTES} 分钟")
    print(f"{'='*70}\n")

    all_hits = []
    # 汇总信息
    summary = {
        "date": date,
        "run_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "hubs": {},
        "api_calls": 0,
        "api_errors": 0,
    }

    for hub, destinations in hubs.items():
        print(f"[枢纽] {hub} — 正在检索航班数据...")

        # ---- 获取机场天气（辅助展示）----
        weather_data = api.get_airport_weather(hub)
        weather_text = format_airport_weather(weather_data)
        print(f"  [天气] {hub}: {weather_text}")

        # ---- Step 1: 收集今天进港航班（找延误飞机）----
        inbound_flights = []
        print(f"  [Step 1] 扫描进港航班（找延误严重的飞机）...")
        for i, dest in enumerate(destinations):
            flights = api.search_flights(dest, hub, date)
            inbound_flights.extend(flights)
            if (i + 1) % 10 == 0:
                print(f"    进港扫描: {i+1}/{len(destinations)}  "
                      f"({len(inbound_flights)} 个航班)")

        print(f"    进港航班共 {len(inbound_flights)} 个")

        # 找出严重延误的进港航班，建立 机号→航班 映射
        # 只保留每架飞机最晚的那个进港航班（即直接前序）
        aircraft_inbound = {}
        delayed_aircraft = set()
        for fl in inbound_flights:
            ac = fl.get("AircraftNumber", "").strip()
            if not ac:
                continue
            est_arr = get_best_arrival_time(fl)
            plan_arr = parse_time(fl.get("FlightArrtimePlanDate", ""))
            if not est_arr:
                continue

            # 保留该机号最晚到达的进港航班
            if ac in aircraft_inbound:
                prev_arr = get_best_arrival_time(aircraft_inbound[ac])
                if prev_arr and est_arr <= prev_arr:
                    continue
            aircraft_inbound[ac] = fl

            # 标记延误飞机
            if plan_arr:
                delay = (est_arr - plan_arr).total_seconds() / 60
                if delay >= SIGNIFICANT_DELAY_MINUTES:
                    delayed_aircraft.add(ac)

        if verbose:
            print(f"    飞机映射: {len(aircraft_inbound)} 架, "
                  f"延误>=30分: {len(delayed_aircraft)} 架")

        if delayed_aircraft:
            print(f"  [发现] {len(delayed_aircraft)} 架飞机前序严重延误:")
            for ac in delayed_aircraft:
                fl = aircraft_inbound[ac]
                est_arr = get_best_arrival_time(fl)
                plan_arr = parse_time(fl.get("FlightArrtimePlanDate", ""))
                delay = round((est_arr - plan_arr).total_seconds() / 60)
                print(f"    {fl.get('FlightNo'):8s} "
                      f"{fl.get('FlightDepcode')}->{fl.get('FlightArrcode')} "
                      f"计划到{plan_arr.strftime('%H:%M')} "
                      f"预计到{est_arr.strftime('%H:%M')} "
                      f"晚{delay}分钟 "
                      f"状态:{fl.get('FlightState','')} "
                      f"机号:{ac}")

        # ---- Step 2: 收集出港CZ航班（找受害航班）----
        # 扫描今天 + 明天（跨天场景）
        print(f"  [Step 2] 扫描出港南航航班（找受害航班）...")
        departing_cz = []
        all_hub_departures = []
        dates_to_scan = [date]
        if date != tomorrow:
            dates_to_scan.append(tomorrow)

        for scan_date in dates_to_scan:
            for i, dest in enumerate(destinations):
                flights = api.search_flights(hub, dest, scan_date)
                all_hub_departures.extend(flights)
                for fl in flights:
                    fno = fl.get("FlightNo", "")
                    if fno.startswith("CZ"):
                        departing_cz.append(fl)
                if (i + 1) % 10 == 0:
                    label = "今天" if scan_date == date else "明天"
                    print(f"    {label}出港扫描: {i+1}/{len(destinations)}  "
                          f"(南航 {len(departing_cz)} 个)")

        print(f"    南航出港航班共 {len(departing_cz)} 个"
              f"（今天+明天）")

        if api.error_count > 0:
            print(f"  [注意] 有 {api.error_count} 次 API 请求失败，"
                  f"结果可能不完整")

        # 机场态势
        airport_sit = analyze_airport_situation(all_hub_departures)
        print(f"  [态势] {hub}: 延误率{airport_sit['delay_rate']*100:.0f}% "
              f"取消{airport_sit['cancelled']}班 "
              f"平均延误{airport_sit['avg_delay_min']}分钟")

        # ---- Step 3: 匹配延误飞机 → 后续CZ航班 ----
        print(f"  [Step 3] 匹配前序延误飞机的后续航班...")
        checked = 0
        for fl in departing_cz:
            ac = fl.get("AircraftNumber", "").strip()
            if not ac:
                continue

            # 快速过滤：只看前序延误的飞机
            if ac not in delayed_aircraft:
                continue

            # 前序航班到达的机场 == 后续航班出发的机场
            inbound = aircraft_inbound.get(ac)
            if not inbound:
                continue
            if inbound.get("FlightArrcode") != fl.get("FlightDepcode"):
                continue

            checked += 1
            hit = analyze_inbound_chain(fl, inbound, now)
            if hit:
                hit["hub"] = hub
                hit["airport_situation"] = airport_sit
                hit["hub_weather"] = weather_text
                all_hits.append(hit)

        if verbose:
            print(f"    检查了 {checked} 个航班")

        # 收集该枢纽汇总
        hub_hits = [h for h in all_hits if h.get("hub") == hub]
        summary["hubs"][hub] = {
            "weather": weather_text,
            "inbound_total": len(inbound_flights),
            "delayed_aircraft": len(delayed_aircraft),
            "delayed_details": [],
            "cz_departing": len(departing_cz),
            "airport_situation": airport_sit,
            "hits_count": len(hub_hits),
        }
        # 保存延误飞机详情
        for ac in delayed_aircraft:
            fl = aircraft_inbound[ac]
            est_arr = get_best_arrival_time(fl)
            plan_arr = parse_time(fl.get("FlightArrtimePlanDate", ""))
            delay = round((est_arr - plan_arr).total_seconds() / 60) if (est_arr and plan_arr) else 0
            summary["hubs"][hub]["delayed_details"].append({
                "flight": fl.get("FlightNo", ""),
                "route": f"{fl.get('FlightDepcode','')}->{fl.get('FlightArrcode','')}",
                "delay_min": delay,
                "aircraft": ac,
                "state": fl.get("FlightState", ""),
            })

        print()

    # ---- 输出结果 ----
    print(f"{'='*70}")
    if not all_hits:
        print("  未发现可操作的航变机会")
        print()
        print("  可能原因:")
        print("  - 当前延误的飞机的后续航班已发布航变通知")
        print("  - 前序延误严重但过站时间仍充足")
        print("  - 受影响航班距出发不足2小时（来不及买票）")
        print("  - 深夜时段隔夜过站充裕，适合白天飞行高峰期运行")
        print(f"{'='*70}\n")
        summary["api_calls"] = api.call_count
        summary["api_errors"] = api.error_count
        print(f"  (共发起 {api.call_count} 次 API 请求, "
              f"{api.error_count} 次失败)")
        return all_hits, summary

    # 按预估延误时间降序
    all_hits.sort(key=lambda h: h["estimated_delay_min"], reverse=True)

    print(f"  发现 {len(all_hits)} 个航变机会（航司未通知，可提前买里程票）:")
    print(f"{'='*70}\n")

    for i, hit in enumerate(all_hits, 1):
        delay = hit["estimated_delay_min"]
        mins_left = hit["minutes_until_departure"]
        hours_left = mins_left // 60
        mins_remain = mins_left % 60

        print(f"  ┌─[{i}] {hit['flight']}  "
              f"{hit['route']}  ({hit['dep_city']})")
        print(f"  │ 计划出发: {hit['plan_departure']}  "
              f"(距现在 {hours_left}时{mins_remain}分)")
        print(f"  │ 当前状态: {hit['current_state']}  ← 航司未通知航变!")
        print(f"  │ 机型: {hit['aircraft_type']}  "
              f"({hit.get('aircraft_model', '')})"
              f"  机号: {hit['aircraft']}")
        if hit.get("terminal"):
            print(f"  │ 航站楼: {hit['terminal']}")
        print(f"  │")
        print(f"  │ ⛔ 预估延误: ~{delay} 分钟")
        print(f"  │    最早可出发: {hit['earliest_possible_dep']}")
        print(f"  │    {hit['certainty']}")
        print(f"  │")
        print(f"  │ 前序航班: {hit['inbound_flight']}  "
              f"{hit['inbound_route']}  "
              f"状态: {hit['inbound_state']}")
        print(f"  │    计划到达: {hit['inbound_plan_arrival']}")
        print(f"  │    预计到达: {hit['inbound_est_arrival']}")
        print(f"  │    延误: {hit['inbound_delay_min']} 分钟", end="")
        if hit.get("inbound_delay_reason"):
            print(f"  原因: {hit['inbound_delay_reason']}", end="")
        print()
        print(f"  │    过站需: {hit['min_turnaround_min']} 分钟")

        # 辅助信息
        extras = []
        if hit.get("ontime_rate"):
            extras.append(f"历史准点率: {hit['ontime_rate']}")
        if hit.get("veryzhun_predicted_delay_min"):
            extras.append(
                f"飞常准AI预测延误: {hit['veryzhun_predicted_delay_min']}分钟")
        if hit.get("dep_weather"):
            extras.append(f"出发天气: {hit['dep_weather']}")
        if hit.get("arr_weather"):
            extras.append(f"到达天气: {hit['arr_weather']}")
        if extras:
            print(f"  │")
            for e in extras:
                print(f"  │ {e}")

        print(f"  └─────────────────────────────────────")
        print()

    summary["api_calls"] = api.call_count
    summary["api_errors"] = api.error_count
    print(f"  (共发起 {api.call_count} 次 API 请求, "
          f"{api.error_count} 次失败)")
    return all_hits, summary


# ============================================================
# 邮件通知 (Resend API)
# ============================================================

def build_email_html(hits: list) -> str:
    """构建 HTML 邮件正文"""
    rows = []
    for i, hit in enumerate(hits, 1):
        delay = hit["estimated_delay_min"]
        mins_left = hit["minutes_until_departure"]
        hours_left = mins_left // 60
        mins_remain = mins_left % 60

        row = f"""
        <tr style="border-bottom: 2px solid #e74c3c;">
          <td colspan="2" style="padding:12px; background:#fff3f3;">
            <h3 style="margin:0; color:#c0392b;">
              [{i}] {hit['flight']}  {hit['route']}  ({hit['dep_city']})
            </h3>
          </td>
        </tr>
        <tr><td style="padding:6px 12px; color:#666;">计划出发</td>
            <td style="padding:6px 12px;">{hit['plan_departure']}
            (距现在 {hours_left}时{mins_remain}分)</td></tr>
        <tr><td style="padding:6px 12px; color:#666;">当前状态</td>
            <td style="padding:6px 12px; font-weight:bold; color:#e74c3c;">
            {hit['current_state']} ← 航司未通知航变!</td></tr>
        <tr><td style="padding:6px 12px; color:#666;">机型 / 机号</td>
            <td style="padding:6px 12px;">{hit['aircraft_type']}
            ({hit.get('aircraft_model','')}) / {hit['aircraft']}</td></tr>
        <tr style="background:#fff8e1;">
          <td style="padding:6px 12px; color:#e65100; font-weight:bold;">
            预估延误</td>
          <td style="padding:6px 12px; color:#e65100; font-weight:bold;
              font-size:18px;">
            ~{delay} 分钟</td></tr>
        <tr><td style="padding:6px 12px; color:#666;">最早可出发</td>
            <td style="padding:6px 12px;">{hit['earliest_possible_dep']}</td></tr>
        <tr><td style="padding:6px 12px; color:#666;">确定性</td>
            <td style="padding:6px 12px;">{hit['certainty']}</td></tr>
        <tr style="background:#f5f5f5;">
          <td style="padding:6px 12px; color:#666;">前序航班</td>
          <td style="padding:6px 12px;">{hit['inbound_flight']}
            {hit['inbound_route']}  状态: {hit['inbound_state']}</td></tr>
        <tr style="background:#f5f5f5;">
          <td style="padding:6px 12px; color:#666;">前序延误</td>
          <td style="padding:6px 12px;">{hit['inbound_delay_min']}分钟
            (计划到{hit['inbound_plan_arrival']}
            → 预计{hit['inbound_est_arrival']})</td></tr>
        <tr><td colspan="2" style="padding:4px;"></td></tr>
        """
        rows.append(row)

    now_str = beijing_now().strftime("%Y-%m-%d %H:%M:%S")
    html = f"""
    <html><body style="font-family: 'Microsoft YaHei', Arial, sans-serif;">
    <div style="max-width:700px; margin:0 auto;">
      <div style="background:#c0392b; color:white; padding:16px; text-align:center;">
        <h2 style="margin:0;">南航航变机会提醒</h2>
        <p style="margin:4px 0 0; font-size:13px;">检测时间: {now_str}</p>
      </div>
      <div style="padding:12px; background:#fff3f3; text-align:center;">
        <span style="font-size:20px; font-weight:bold; color:#c0392b;">
          发现 {len(hits)} 个可操作机会!
        </span>
      </div>
      <table style="width:100%; border-collapse:collapse; font-size:14px;">
        {''.join(rows)}
      </table>
      <div style="padding:12px; background:#f9f9f9; color:#999; font-size:12px;
                  text-align:center;">
        南航航变机会检测器 — 持续监控中
      </div>
    </div>
    </body></html>
    """
    return html


def send_email(to_addr: str, resend_key: str, hits: list) -> bool:
    """通过 Resend API 发送航变提醒邮件（免授权码）"""
    subject = (f"[航变提醒] 发现 {len(hits)} 个机会! "
               f"{hits[0]['flight']} 预延{hits[0]['estimated_delay_min']}分钟")
    html = build_email_html(hits)

    # 纯文本备用
    text_lines = []
    for i, hit in enumerate(hits, 1):
        text_lines.append(
            f"[{i}] {hit['flight']} {hit['route']} "
            f"预估延误{hit['estimated_delay_min']}分钟 "
            f"当前状态:{hit['current_state']} "
            f"计划出发:{hit['plan_departure']}")

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": "Flight Alert <onboarding@resend.dev>",
                "to": [to_addr],
                "subject": subject,
                "html": html,
                "text": "\n".join(text_lines),
            },
            timeout=30,
        )
        if resp.status_code in (200, 201):
            print(f"  [邮件] 已通过 Resend 发送提醒到 {to_addr}", file=sys.stderr)
            return True
        else:
            print(f"  [邮件失败] Resend 返回 {resp.status_code}: {resp.text}",
                  file=sys.stderr)
            return False
    except Exception as e:
        print(f"  [邮件失败] {e}", file=sys.stderr)
        return False


def build_summary_email_html(summary: dict, hits: list) -> str:
    """构建每次运行的汇总邮件 HTML"""
    now_str = summary.get("run_time", beijing_now().strftime("%Y-%m-%d %H:%M:%S"))
    has_hits = len(hits) > 0

    # 顶部 banner 颜色：有机会红色，无机会蓝色
    banner_bg = "#c0392b" if has_hits else "#2c3e50"
    banner_text = (f"发现 {len(hits)} 个航变机会!" if has_hits
                   else "本轮未发现航变机会")

    # 各枢纽扫描概况
    hub_rows = []
    for hub, info in summary.get("hubs", {}).items():
        sit = info.get("airport_situation", {})
        delay_rate = sit.get("delay_rate", 0) * 100
        # 延误飞机列表
        delayed_list = ""
        for d in info.get("delayed_details", []):
            color = "#e74c3c" if d["delay_min"] >= 60 else "#e67e22"
            delayed_list += (
                f'<span style="display:inline-block; margin:2px 4px; '
                f'padding:2px 8px; background:{color}; color:white; '
                f'border-radius:3px; font-size:12px;">'
                f'{d["flight"]} {d["route"]} 晚{d["delay_min"]}分</span>'
            )
        if not delayed_list:
            delayed_list = '<span style="color:#27ae60;">无严重延误</span>'

        hub_rows.append(f"""
        <tr style="border-bottom:1px solid #eee;">
          <td style="padding:10px; font-weight:bold; font-size:16px;
                     vertical-align:top;">{hub}</td>
          <td style="padding:10px;">
            <div style="margin-bottom:4px;">
              <span style="color:#666;">天气:</span> {info.get('weather', 'N/A')}
            </div>
            <div style="margin-bottom:4px;">
              <span style="color:#666;">进港航班:</span> {info.get('inbound_total', 0)} 个
              &nbsp;|&nbsp;
              <span style="color:#666;">南航出港:</span> {info.get('cz_departing', 0)} 个
            </div>
            <div style="margin-bottom:4px;">
              <span style="color:#666;">机场态势:</span>
              延误率 {delay_rate:.0f}%
              &nbsp; 取消 {sit.get('cancelled', 0)} 班
              &nbsp; 平均延误 {sit.get('avg_delay_min', 0)} 分钟
            </div>
            <div style="margin-bottom:4px;">
              <span style="color:#666;">前序严重延误飞机:</span>
              {info.get('delayed_aircraft', 0)} 架
            </div>
            <div>{delayed_list}</div>
          </td>
        </tr>""")

    # 命中机会详情
    hit_section = ""
    if hits:
        hit_rows = []
        for i, hit in enumerate(hits, 1):
            delay = hit["estimated_delay_min"]
            mins_left = hit["minutes_until_departure"]
            hours_left = mins_left // 60
            mins_remain = mins_left % 60
            hit_rows.append(f"""
            <tr style="border-left:4px solid #e74c3c; background:#fff5f5;">
              <td style="padding:10px;" colspan="2">
                <div style="font-weight:bold; color:#c0392b; font-size:15px;">
                  [{i}] {hit['flight']} &nbsp; {hit['route']}
                </div>
                <div style="margin-top:4px; font-size:13px;">
                  计划出发 {hit['plan_departure']}
                  (距现在 {hours_left}时{mins_remain}分)
                  &nbsp;|&nbsp; 状态: {hit['current_state']}
                  <strong style="color:#e74c3c;"> ← 航司未通知航变</strong>
                </div>
                <div style="margin-top:4px; font-size:13px;">
                  预估延误 <strong style="color:#e74c3c; font-size:16px;">
                  ~{delay}分钟</strong>
                  &nbsp;|&nbsp; 前序: {hit['inbound_flight']}
                  {hit['inbound_route']}
                  晚{hit['inbound_delay_min']}分钟
                </div>
                <div style="margin-top:4px; font-size:12px; color:#888;">
                  机号 {hit['aircraft']} &nbsp; 机型 {hit['aircraft_type']}
                  &nbsp; 过站需 {hit['min_turnaround_min']}分钟
                </div>
              </td>
            </tr>""")
        hit_section = f"""
        <div style="margin-top:16px;">
          <h3 style="color:#c0392b; border-bottom:2px solid #e74c3c;
                     padding-bottom:6px;">
            航变机会详情
          </h3>
          <table style="width:100%; border-collapse:collapse; font-size:14px;">
            {''.join(hit_rows)}
          </table>
        </div>"""

    # 天气预扫描报告（智能扫描模式）
    weather_section = ""
    weather_prescan = summary.get("weather_prescan", {})
    if weather_prescan:
        # 筛选风险分>=20 的机场
        risky = sorted(weather_prescan.items(), key=lambda x: -x[1][0])
        risky = [(ap, sc, desc) for ap, (sc, desc) in risky if sc >= 20]
        if risky:
            wx_chips = []
            for ap, sc, desc in risky:
                if sc >= 50:
                    bg = "#c0392b"
                elif sc >= 30:
                    bg = "#e67e22"
                else:
                    bg = "#f39c12"
                wx_chips.append(
                    f'<span style="display:inline-block; margin:2px 4px; '
                    f'padding:3px 10px; background:{bg}; color:white; '
                    f'border-radius:3px; font-size:12px;">'
                    f'{ap} 分{sc} {desc}</span>')
            weather_section = f"""
      <div style="padding:16px;">
        <h3 style="color:#e67e22; border-bottom:2px solid #f39c12;
                   padding-bottom:6px;">
          天气预扫描 (恶劣天气机场)
        </h3>
        <div style="font-size:13px; color:#666; margin-bottom:8px;">
          基于天气风险评分动态扩展了扫描范围，以下机场天气可能导致航班延误：
        </div>
        <div>{''.join(wx_chips)}</div>
      </div>"""

    html = f"""
    <html><body style="font-family: 'Microsoft YaHei', Arial, sans-serif;
                       background:#f5f5f5; padding:20px;">
    <div style="max-width:700px; margin:0 auto; background:white;
                border-radius:8px; overflow:hidden;
                box-shadow:0 2px 8px rgba(0,0,0,0.1);">
      <div style="background:{banner_bg}; color:white; padding:20px;
                  text-align:center;">
        <h2 style="margin:0;">南航航变检测报告</h2>
        <p style="margin:8px 0 0; font-size:14px; opacity:0.9;">
          {now_str} &nbsp; | &nbsp; 检测日期: {summary.get('date', 'N/A')}
        </p>
      </div>

      <div style="padding:16px; text-align:center;
                  background:{'#fff3f3' if has_hits else '#f0f7ff'};">
        <span style="font-size:22px; font-weight:bold;
                     color:{banner_bg};">
          {banner_text}
        </span>
      </div>

      {weather_section}

      <div style="padding:16px;">
        <h3 style="color:#2c3e50; border-bottom:2px solid #3498db;
                   padding-bottom:6px;">
          各枢纽扫描概况
        </h3>
        <table style="width:100%; border-collapse:collapse; font-size:14px;">
          {''.join(hub_rows)}
        </table>
      </div>

      {hit_section}

      <div style="padding:12px 16px; background:#f9f9f9; color:#999;
                  font-size:12px; text-align:center; border-top:1px solid #eee;">
        API 请求 {summary.get('api_calls', 0)} 次
        (失败 {summary.get('api_errors', 0)} 次)
        &nbsp;|&nbsp; 南航航变机会检测器
      </div>
    </div>
    </body></html>
    """
    return html


def send_summary_email(to_addr: str, resend_key: str,
                       summary: dict, hits: list) -> bool:
    """每次运行后发送汇总邮件"""
    n_hits = len(hits)
    date = summary.get("date", "")
    run_time = summary.get("run_time", "")
    hub_names = ", ".join(summary.get("hubs", {}).keys())

    if n_hits > 0:
        subject = (f"[航变报告] {date} 发现 {n_hits} 个机会! "
                   f"({hub_names})")
    else:
        subject = f"[航变报告] {date} {run_time} 未发现机会 ({hub_names})"

    html = build_summary_email_html(summary, hits)

    # 纯文本备用
    text_lines = [f"南航航变检测报告 {run_time}", f"检测日期: {date}", ""]
    for hub, info in summary.get("hubs", {}).items():
        sit = info.get("airport_situation", {})
        text_lines.append(
            f"[{hub}] 进港{info.get('inbound_total',0)}班 "
            f"南航出港{info.get('cz_departing',0)}班 "
            f"延误飞机{info.get('delayed_aircraft',0)}架 "
            f"延误率{sit.get('delay_rate',0)*100:.0f}% "
            f"天气:{info.get('weather','N/A')}")
    text_lines.append("")
    if hits:
        text_lines.append(f"发现 {n_hits} 个航变机会:")
        for i, h in enumerate(hits, 1):
            text_lines.append(
                f"  [{i}] {h['flight']} {h['route']} "
                f"预延{h['estimated_delay_min']}分 "
                f"状态:{h['current_state']} "
                f"出发:{h['plan_departure']}")
    else:
        text_lines.append("本轮未发现可操作的航变机会。")

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": "Flight Alert <onboarding@resend.dev>",
                "to": [to_addr],
                "subject": subject,
                "html": html,
                "text": "\n".join(text_lines),
            },
            timeout=30,
        )
        if resp.status_code in (200, 201):
            print(f"  [邮件] 已发送汇总报告到 {to_addr}", file=sys.stderr)
            return True
        else:
            print(f"  [邮件失败] Resend 返回 {resp.status_code}: {resp.text}",
                  file=sys.stderr)
            return False
    except Exception as e:
        print(f"  [邮件失败] {e}", file=sys.stderr)
        return False


# ============================================================
# 去重缓存（跨运行持久化）
# ============================================================

DEDUP_HOURS = 6  # 同一航班 6 小时内不重复通知


def make_hit_key(hit: dict) -> str:
    """生成航班唯一标识"""
    return f"{hit['flight']}|{hit['plan_departure']}|{hit['aircraft']}"


def load_dedup_cache(filepath: str) -> dict:
    """从 JSON 文件加载去重记录"""
    try:
        with open(filepath) as f:
            data = json.load(f)
        now = beijing_now()
        # 清理过期记录
        return {
            k: v for k, v in data.items()
            if (now - datetime.fromisoformat(v)).total_seconds()
            < DEDUP_HOURS * 3600
        }
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


def save_dedup_cache(filepath: str, cache: dict):
    """保存去重记录到 JSON 文件"""
    with open(filepath, "w") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def filter_new_hits(hits: list, cache: dict) -> list:
    """过滤已通知过的航班，返回新发现"""
    return [h for h in hits if make_hit_key(h) not in cache]


def mark_notified(hits: list, cache: dict) -> dict:
    """标记航班为已通知"""
    now = beijing_now().isoformat()
    for h in hits:
        cache[make_hit_key(h)] = now
    return cache


# ============================================================
# 持续监控
# ============================================================

def monitor_loop(args, hubs: dict):
    """持续监控主循环"""
    import signal

    cycle_min = args.cycle
    resend_key = args.resend_key

    # 去重缓存：优先用文件持久化，否则纯内存
    dedup_file = args.dedup_file
    if dedup_file:
        notified = load_dedup_cache(dedup_file)
    else:
        notified = {}

    stop_flag = [False]

    def handle_signal(signum, frame):
        stop_flag[0] = True
        print("\n  [监控] 收到停止信号，完成当前周期后退出...", file=sys.stderr)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    cycle_count = 0
    total_hits_found = 0

    print(f"\n{'='*70}")
    print(f"  南航航变机会 — 持续监控模式")
    print(f"  监控周期: 每 {cycle_min} 分钟")
    print(f"  通知邮箱: {args.email}")
    print(f"  活跃时段: {args.active_start}:00 - {args.active_end}:00")
    print(f"  通知方式: Resend API")
    print(f"  按 Ctrl+C 安全退出")
    print(f"{'='*70}\n")

    while not stop_flag[0]:
        cycle_count += 1
        now = beijing_now()

        # 检查活跃时段
        current_hour = now.hour
        if not (args.active_start <= current_hour < args.active_end):
            next_active = now.replace(hour=args.active_start, minute=0,
                                      second=0)
            if current_hour >= args.active_end:
                next_active += timedelta(days=1)
            wait_sec = (next_active - now).total_seconds()
            wait_hr = wait_sec / 3600
            print(f"  [{now.strftime('%H:%M')}] 非活跃时段 "
                  f"({args.active_start}:00-{args.active_end}:00), "
                  f"休眠 {wait_hr:.1f} 小时后自动恢复...")
            while wait_sec > 0 and not stop_flag[0]:
                time.sleep(min(wait_sec, 60))
                wait_sec -= 60
            continue

        print(f"\n  [{now.strftime('%H:%M:%S')}] === 第 {cycle_count} 轮监控 ===")

        date = now.strftime("%Y-%m-%d")

        # 清理过期去重记录
        expired = [
            k for k, v in notified.items()
            if (now - datetime.fromisoformat(v)).total_seconds()
            > DEDUP_HOURS * 3600
        ]
        for k in expired:
            del notified[k]

        # 执行检测
        summary = {}
        try:
            hits, summary = run_detection(
                args.key, date, hubs,
                verbose=args.verbose, interval=args.interval,
                auto_renew=args.auto_renew,
            )
        except Exception as e:
            print(f"  [监控异常] {e}", file=sys.stderr)
            hits = []

        # 每轮都发汇总邮件
        if args.email and resend_key and summary:
            send_summary_email(args.email, resend_key, summary, hits)

        # 过滤已通知的
        new_hits = filter_new_hits(hits, notified)

        if new_hits:
            total_hits_found += len(new_hits)
            print(f"\n  [新发现] {len(new_hits)} 个新机会 "
                  f"(本轮共{len(hits)}个, "
                  f"已通知过{len(hits)-len(new_hits)}个)")

            if args.email and resend_key:
                ok = send_email(args.email, resend_key, new_hits)
                if ok:
                    notified = mark_notified(new_hits, notified)
            else:
                print(f"  [注意] 未配置 Resend Key (--resend-key)，跳过邮件",
                      file=sys.stderr)
                notified = mark_notified(new_hits, notified)
        else:
            if hits:
                print(f"  [本轮] {len(hits)} 个机会均已通知过，不重复发送")
            else:
                print(f"  [本轮] 未发现新机会")

        # 持久化去重缓存
        if dedup_file:
            save_dedup_cache(dedup_file, notified)

        print(f"  [统计] 已运行 {cycle_count} 轮, "
              f"累计发现 {total_hits_found} 个新机会, "
              f"去重池 {len(notified)} 条")

        if stop_flag[0]:
            break

        print(f"  [休眠] {cycle_min} 分钟后进行下一轮检测...")
        wait_sec = cycle_min * 60
        while wait_sec > 0 and not stop_flag[0]:
            time.sleep(min(wait_sec, 10))
            wait_sec -= 10

    # 退出前保存
    if dedup_file:
        save_dedup_cache(dedup_file, notified)

    print(f"\n  [监控结束] 共运行 {cycle_count} 轮, "
          f"发现 {total_hits_found} 个新机会")


# ============================================================
# CLI 入口
# ============================================================

def main():
    global SIGNIFICANT_DELAY_MINUTES, MIN_TURNAROUND_NARROW, \
        MIN_TURNAROUND_WIDE, MIN_BOOKING_WINDOW_MINUTES

    parser = argparse.ArgumentParser(
        description="南航航变机会检测器 — 找到铁定延误但未通知的航班，提前购买里程票",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
原理:
  前序飞机严重延误 → 到达后需要过站时间 → 数学上赶不上后续航班
  但航司尚未发布航变通知 → 此时可以买里程票 → 等航变后免费改签/退票

示例:
  %(prog)s --auto-renew                          # 单次扫描
  %(prog)s --hub CAN --dest PKX,PVG,CTU          # 指定枢纽和航线

  # 单次扫描 + 邮件通知:
  %(prog)s --auto-renew --email 634897859@qq.com --resend-key re_xxx

  # 持续监控模式:
  %(prog)s --monitor --email 634897859@qq.com --resend-key re_xxx --auto-renew
  %(prog)s --monitor --cycle 10 --active-start 8 --active-end 22  # 自定义

  # GitHub CI 模式 (使用环境变量 + 去重文件):
  RESEND_API_KEY=re_xxx python %(prog)s --auto-renew --email 634897859@qq.com \\
    --dedup-file .notified_cache.json --json
        """,
    )
    parser.add_argument(
        "--key", "-k",
        default="sk-5BvX04jqSMsy42k4OJiekvRjNGxxBulxSf5vQbyCZIw",
        help="飞常准 API Key",
    )
    parser.add_argument(
        "--date", "-d",
        default=beijing_now().strftime("%Y-%m-%d"),
        help="查询日期 YYYY-MM-DD (默认今天)",
    )
    parser.add_argument(
        "--hub",
        action="append",
        help="枢纽机场代码，可多次使用 (默认: CAN, PKX, URC, SZX)",
    )
    parser.add_argument(
        "--dest",
        help="自定义目的地列表，逗号分隔",
    )
    parser.add_argument(
        "--turnaround",
        type=int,
        default=None,
        help="自定义最小过站时间(分钟)",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=SIGNIFICANT_DELAY_MINUTES,
        help=f"前序延误阈值(分钟)，默认 {SIGNIFICANT_DELAY_MINUTES}",
    )
    parser.add_argument(
        "--booking-window",
        type=int,
        default=MIN_BOOKING_WINDOW_MINUTES,
        help=f"最小买票窗口(分钟)，默认 {MIN_BOOKING_WINDOW_MINUTES}",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=REQUEST_INTERVAL,
        help=f"API 请求间隔秒数，默认 {REQUEST_INTERVAL}",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="显示详细调试信息",
    )
    parser.add_argument(
        "--auto-renew",
        action="store_true",
        help="余额不足时自动获取新 Key",
    )
    parser.add_argument(
        "--smart-scan",
        action="store_true",
        help="天气智能扫描：先预判各机场天气，自动加入天气差的二线机场",
    )

    # ---- 通知相关 ----
    notify_group = parser.add_argument_group("邮件通知 (Resend API)")
    notify_group.add_argument(
        "--email",
        default=None,
        help="通知邮箱地址 (例: 634897859@qq.com)",
    )
    notify_group.add_argument(
        "--resend-key",
        default=None,
        help="Resend API Key (也可通过环境变量 RESEND_API_KEY 设置)",
    )
    notify_group.add_argument(
        "--dedup-file",
        default=None,
        help="去重缓存文件路径 (CI模式用，跨运行持久化去重记录)",
    )

    # ---- 持续监控相关 ----
    monitor_group = parser.add_argument_group("持续监控模式")
    monitor_group.add_argument(
        "--monitor", "-m",
        action="store_true",
        help="启用持续监控模式，周期性扫描并邮件通知",
    )
    monitor_group.add_argument(
        "--cycle",
        type=int,
        default=15,
        help="监控周期(分钟)，默认 15",
    )
    monitor_group.add_argument(
        "--active-start",
        type=int,
        default=7,
        help="活跃监控开始时间(整点, 0-23)，默认 7",
    )
    monitor_group.add_argument(
        "--active-end",
        type=int,
        default=23,
        help="活跃监控结束时间(整点, 0-23)，默认 23",
    )

    args = parser.parse_args()

    # Resend Key: CLI 参数 > 环境变量
    if not args.resend_key:
        args.resend_key = os.environ.get("RESEND_API_KEY")

    SIGNIFICANT_DELAY_MINUTES = args.threshold
    MIN_BOOKING_WINDOW_MINUTES = args.booking_window
    if args.turnaround is not None:
        MIN_TURNAROUND_NARROW = args.turnaround
        MIN_TURNAROUND_WIDE = args.turnaround

    if args.hub:
        if args.dest:
            destinations = [d.strip().upper() for d in args.dest.split(",")]
            hubs = {h.upper(): destinations for h in args.hub}
        else:
            hubs = {}
            for h in args.hub:
                h = h.upper()
                hubs[h] = CZ_HUBS.get(h, list(CZ_HUBS.get("CAN", [])))
    else:
        hubs = CZ_HUBS

    # ---- 天气智能扫描：预判天气 → 动态扩展扫描范围 ----
    weather_report = {}
    if args.smart_scan:
        prescan_api = VariFlightAPI(args.key, interval=args.interval,
                                    auto_renew=args.auto_renew)
        hubs, weather_report = build_weather_smart_hubs(
            prescan_api, hubs, verbose=True)

    # ---- 持续监控模式 ----
    if args.monitor:
        if args.email and not args.resend_key:
            print("[错误] 邮件通知需要 Resend API Key", file=sys.stderr)
            print("  方式1: --resend-key re_xxxx", file=sys.stderr)
            print("  方式2: export RESEND_API_KEY=re_xxxx", file=sys.stderr)
            print("  获取: https://resend.com (免费 100封/天)",
                  file=sys.stderr)
            sys.exit(1)
        monitor_loop(args, hubs)
        sys.exit(0)

    # ---- 单次运行模式 ----
    # 加载去重缓存（CI 模式）
    dedup_file = args.dedup_file
    dedup_cache = load_dedup_cache(dedup_file) if dedup_file else {}

    hits, summary = run_detection(
        args.key, args.date, hubs,
        verbose=args.verbose, interval=args.interval,
        auto_renew=args.auto_renew,
    )

    # 将天气预扫描报告加入汇总
    if weather_report:
        summary["weather_prescan"] = weather_report

    # 每次运行都发汇总邮件
    if args.email and args.resend_key:
        print(f"\n  [通知] 发送汇总报告到 {args.email}...")
        send_summary_email(args.email, args.resend_key, summary, hits)

        # 如果有新机会，更新去重缓存
        if hits and dedup_file:
            new_hits = filter_new_hits(hits, dedup_cache)
            if new_hits:
                dedup_cache = mark_notified(new_hits, dedup_cache)
                save_dedup_cache(dedup_file, dedup_cache)

    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))

    sys.exit(0 if not hits else 1)


if __name__ == "__main__":
    main()
