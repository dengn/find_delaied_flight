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
import sys
import time
from datetime import datetime, timedelta

import requests

from auto_renew_key import obtain_new_key

# ============================================================
# 配置
# ============================================================

API_URL = "https://mcp.variflight.com/api/v1/mcp/data"

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
                                    print(f"  [续杯] 新 Key 已生效: {new_key[:20]}...\n",
                                          file=sys.stderr)
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
    """运行延误检测"""
    api = VariFlightAPI(api_key, interval=interval, auto_renew=auto_renew)
    now = datetime.now()
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"\n{'='*70}")
    print(f"  南航航变机会检测器")
    print(f"  检测日期: {date}")
    print(f"  运行时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  买票窗口: 距起飞 >= {MIN_BOOKING_WINDOW_MINUTES} 分钟")
    print(f"  前序延误阈值: >= {SIGNIFICANT_DELAY_MINUTES} 分钟")
    print(f"{'='*70}\n")

    all_hits = []

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
        print(f"  (共发起 {api.call_count} 次 API 请求, "
              f"{api.error_count} 次失败)")
        return all_hits

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

    print(f"  (共发起 {api.call_count} 次 API 请求, "
          f"{api.error_count} 次失败)")
    return all_hits


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
  %(prog)s --auto-renew                      # 自动续杯，扫描全部枢纽
  %(prog)s --hub CAN                         # 只扫描广州枢纽
  %(prog)s --hub CAN --dest PKX,PVG,CTU      # 精简扫描指定航线
  %(prog)s --threshold 20                    # 降低前序延误阈值到20分钟
  %(prog)s --booking-window 60               # 降低买票窗口到1小时
  %(prog)s --json                            # JSON输出（便于程序处理）
        """,
    )
    parser.add_argument(
        "--key", "-k",
        default="sk-5BvX04jqSMsy42k4OJiekvRjNGxxBulxSf5vQbyCZIw",
        help="飞常准 API Key",
    )
    parser.add_argument(
        "--date", "-d",
        default=datetime.now().strftime("%Y-%m-%d"),
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

    args = parser.parse_args()

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

    hits = run_detection(
        args.key, args.date, hubs,
        verbose=args.verbose, interval=args.interval,
        auto_renew=args.auto_renew,
    )

    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))

    sys.exit(0 if not hits else 1)


if __name__ == "__main__":
    main()
