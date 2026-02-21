#!/usr/bin/env python3
"""
南航航班延误智能检测器
综合多维信号检测「一定会延误但还未发布通知」的南航航班。

检测信号：
  1. 前序航班延误 — 飞机还没到，后续航班铁定晚点
  2. 天气恶劣 — 出发/到达机场能见度低、暴雨雷暴大风等
  3. 机场整体态势 — 该机场当天航班大面积延误（流控/停机坪关闭等）
  4. 飞常准AI预测 — VeryZhun模型已预测延误但官方未通知
  5. 历史准点率 — 该航班长期准点率低，叠加其他信号则风险更高

使用飞常准 (VariFlight) API 获取航班数据。
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

# 判定"明显晚到"的阈值（分钟）
SIGNIFICANT_DELAY_MINUTES = 30

# 请求间隔（秒）
REQUEST_INTERVAL = 0.6

# ---- 天气风险关键词 ----
WEATHER_SEVERE = {"雷暴", "暴雨", "暴雪", "大暴雨", "冻雨", "冰雹", "台风",
                  "大雾", "浓雾", "沙尘暴"}
WEATHER_MODERATE = {"雷阵雨", "大雨", "大雪", "雨夹雪", "中雨", "中雪",
                    "雾", "扬沙", "霾"}

# 能见度阈值（米）
VIS_SEVERE = 800     # 低于此值 → 高风险（可能关闭跑道）
VIS_MODERATE = 1500  # 低于此值 → 中风险

# 风力阈值（级）
WIND_SEVERE = 8
WIND_MODERATE = 6

# 机场延误率阈值
AIRPORT_DELAY_RATE_HIGH = 0.30   # 30%以上航班延误 → 机场大面积延误
AIRPORT_DELAY_RATE_MODERATE = 0.15


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

    def _call(self, endpoint: str, params: dict) -> dict:
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

    def search_flight_by_number(self, fnum: str, date: str) -> list:
        result = self._call("flight", {"fnum": fnum, "date": date})
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and not result.get("error_code"):
            return [result]
        return []

    def get_airport_weather(self, airport_code: str) -> dict:
        """获取机场未来3天天气"""
        result = self._call("futureAirportWeather",
                            {"code": airport_code, "type": "1"})
        return result if isinstance(result, dict) else {}


# ============================================================
# 时间解析工具
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
    for key in ("FlightArrtimeDate", "FlightArrtimeReadyDate",
                "VeryZhunReadyArrtimeDate", "FlightArrtimePlanDate"):
        t = parse_time(flight.get(key, ""))
        if t:
            return t
    return None


def get_best_departure_time(flight: dict) -> datetime | None:
    for key in ("FlightDeptimeDate", "FlightDeptimeReadyDate",
                "VeryZhunReadyDeptimeDate", "FlightDeptimePlanDate"):
        t = parse_time(flight.get(key, ""))
        if t:
            return t
    return None


def is_widebody(ftype: str) -> bool:
    return bool(ftype) and ftype[:3].upper() in WIDEBODY_TYPES


def get_min_turnaround(ftype: str) -> int:
    return MIN_TURNAROUND_WIDE if is_widebody(ftype) else MIN_TURNAROUND_NARROW


# ============================================================
# 信号 1: 天气风险评估
# ============================================================

def parse_inline_weather(weather_str: str) -> dict | None:
    """解析航班数据中内嵌的天气字符串 (格式: '多云|9999|3级|26|22')"""
    if not weather_str:
        return None
    parts = weather_str.split("|")
    if len(parts) < 4:
        return None
    result = {"type": parts[0]}
    # 能见度
    try:
        result["visibility"] = int(parts[1])
    except (ValueError, IndexError):
        result["visibility"] = 9999
    # 风力
    try:
        result["wind_level"] = int(parts[2].replace("级", ""))
    except (ValueError, IndexError):
        result["wind_level"] = 0
    return result


def assess_weather_risk(weather: dict | None) -> tuple[int, list[str]]:
    """
    评估天气风险。
    返回 (风险分, 原因列表)
    风险分: 0=无风险, 1-3=轻微, 4-6=中等, 7-10=严重
    """
    if not weather:
        return 0, []
    score = 0
    reasons = []
    wtype = weather.get("type", "")
    vis = weather.get("visibility", 9999)
    wind = weather.get("wind_level", 0)

    # 天气类型
    for kw in WEATHER_SEVERE:
        if kw in wtype:
            score += 7
            reasons.append(f"恶劣天气({wtype})")
            break
    else:
        for kw in WEATHER_MODERATE:
            if kw in wtype:
                score += 4
                reasons.append(f"不良天气({wtype})")
                break

    # 能见度
    if vis < VIS_SEVERE:
        score += 6
        reasons.append(f"极低能见度({vis}m)")
    elif vis < VIS_MODERATE:
        score += 3
        reasons.append(f"低能见度({vis}m)")

    # 风力
    if wind >= WIND_SEVERE:
        score += 5
        reasons.append(f"大风({wind}级)")
    elif wind >= WIND_MODERATE:
        score += 2
        reasons.append(f"较大风({wind}级)")

    return min(score, 10), reasons


def assess_airport_weather(weather_data: dict) -> tuple[int, list[str]]:
    """评估机场天气API返回的详细天气数据"""
    if not weather_data:
        return 0, []
    current = weather_data.get("current", {})
    if not current:
        return 0, []

    score = 0
    reasons = []
    wtype = current.get("Type", "")
    vis_str = current.get("Visib", "9999")
    wind_str = current.get("WindPower", "0级")

    try:
        vis = int(vis_str)
    except ValueError:
        vis = 9999
    try:
        wind = int(wind_str.replace("级", ""))
    except ValueError:
        wind = 0

    weather_info = {"type": wtype, "visibility": vis, "wind_level": wind}
    return assess_weather_risk(weather_info)


# ============================================================
# 信号 2: 机场整体态势分析
# ============================================================

def analyze_airport_situation(all_flights: list) -> dict:
    """
    分析机场所有航班的整体态势。
    返回: {
        total: 总航班数,
        delayed: 延误航班数,
        cancelled: 取消航班数,
        delay_rate: 延误率,
        avg_delay_min: 平均延误分钟,
        situation: "正常" | "轻微拥堵" | "大面积延误"
    }
    """
    total = 0
    delayed = 0
    cancelled = 0
    delay_minutes = []

    for fl in all_flights:
        state = fl.get("FlightState", "")
        total += 1

        if state in ("取消", "提前取消"):
            cancelled += 1
            continue

        if state == "延误":
            delayed += 1

        # 计算实际延误（已出发/到达的航班）
        plan_dep = parse_time(fl.get("FlightDeptimePlanDate", ""))
        actual_dep = parse_time(fl.get("FlightDeptimeDate", ""))
        if not actual_dep:
            actual_dep = parse_time(fl.get("FlightDeptimeReadyDate", ""))

        if plan_dep and actual_dep:
            diff = (actual_dep - plan_dep).total_seconds() / 60
            if diff > 15:  # 15分钟以上算延误
                delayed += 1 if state != "延误" else 0  # 避免重复计数
                delay_minutes.append(diff)

    delay_rate = delayed / total if total > 0 else 0
    avg_delay = sum(delay_minutes) / len(delay_minutes) if delay_minutes else 0

    if delay_rate >= AIRPORT_DELAY_RATE_HIGH:
        situation = "大面积延误"
    elif delay_rate >= AIRPORT_DELAY_RATE_MODERATE:
        situation = "轻微拥堵"
    else:
        situation = "正常"

    return {
        "total": total,
        "delayed": delayed,
        "cancelled": cancelled,
        "delay_rate": round(delay_rate, 3),
        "avg_delay_min": round(avg_delay),
        "situation": situation,
    }


# ============================================================
# 信号 3: 飞常准AI预测
# ============================================================

def check_veryzhun_prediction(flight: dict) -> tuple[int, str | None]:
    """
    检查飞常准AI对该航班的延误预测。
    返回 (预测延误分钟, 说明)
    """
    plan_dep = parse_time(flight.get("FlightDeptimePlanDate", ""))
    vz_dep = parse_time(flight.get("VeryZhunReadyDeptimeDate", ""))
    if not plan_dep or not vz_dep:
        return 0, None
    diff = (vz_dep - plan_dep).total_seconds() / 60
    if diff >= 15:
        return round(diff), f"飞常准AI预测延误{round(diff)}分钟"
    return 0, None


# ============================================================
# 信号 4: 历史准点率
# ============================================================

def parse_ontime_rate(rate_str: str) -> float | None:
    """解析准点率字符串 '86.67%' -> 86.67"""
    if not rate_str:
        return None
    try:
        return float(rate_str.replace("%", ""))
    except ValueError:
        return None


# ============================================================
# 综合延误风险判定
# ============================================================

def flight_not_yet_delayed(flight: dict) -> bool:
    """判断航班是否尚未发布航延通知"""
    state = flight.get("FlightState", "")
    if state in ("延误", "取消", "提前取消", "备降", "返航", "到达", "起飞"):
        return False

    plan_dep = parse_time(flight.get("FlightDeptimePlanDate", ""))
    ready_dep = parse_time(flight.get("FlightDeptimeReadyDate", ""))
    if plan_dep and ready_dep:
        diff = (ready_dep - plan_dep).total_seconds() / 60
        if diff >= SIGNIFICANT_DELAY_MINUTES:
            return False

    return True


def compute_risk_score(signals: dict) -> int:
    """
    综合各信号计算延误风险总分 (0-100)。
    """
    score = 0

    # 前序延误 — 权重最高 (最多40分)
    inbound_delay = signals.get("inbound_delay_min", 0)
    if inbound_delay > 0:
        dep_delay = signals.get("estimated_dep_delay_min", 0)
        score += min(40, 15 + dep_delay)

    # 天气 — 出发+到达 (最多25分)
    dep_weather_score = signals.get("dep_weather_score", 0)
    arr_weather_score = signals.get("arr_weather_score", 0)
    score += min(25, (dep_weather_score + arr_weather_score) * 2)

    # 机场态势 (最多15分)
    delay_rate = signals.get("airport_delay_rate", 0)
    if delay_rate >= AIRPORT_DELAY_RATE_HIGH:
        score += 15
    elif delay_rate >= AIRPORT_DELAY_RATE_MODERATE:
        score += 8

    # 飞常准AI预测 (最多15分)
    vz_delay = signals.get("veryzhun_delay_min", 0)
    if vz_delay >= 60:
        score += 15
    elif vz_delay >= 30:
        score += 10
    elif vz_delay >= 15:
        score += 5

    # 历史准点率 (最多5分)
    ontime = signals.get("ontime_rate")
    if ontime is not None and ontime < 70:
        score += 5
    elif ontime is not None and ontime < 80:
        score += 3

    return min(100, score)


def analyze_comprehensive_risk(departing: dict, inbound: dict | None,
                               airport_situation: dict,
                               dep_weather_score: int,
                               dep_weather_reasons: list,
                               arr_weather_score: int,
                               arr_weather_reasons: list) -> dict | None:
    """
    综合多维信号分析延误风险。
    """
    if not flight_not_yet_delayed(departing):
        return None

    plan_dep = parse_time(departing.get("FlightDeptimePlanDate", ""))
    if not plan_dep:
        return None

    signals = {}
    risk_reasons = []

    # ---- 信号1: 前序航班延误 ----
    inbound_delay_min = 0
    estimated_dep_delay = 0
    if inbound:
        inbound_arr = get_best_arrival_time(inbound)
        inbound_plan_arr = parse_time(inbound.get("FlightArrtimePlanDate", ""))
        if inbound_arr and inbound_plan_arr:
            inbound_delay_min = (inbound_arr - inbound_plan_arr).total_seconds() / 60
            if inbound_delay_min >= SIGNIFICANT_DELAY_MINUTES:
                turnaround = get_min_turnaround(departing.get("ftype", ""))
                earliest_dep = inbound_arr + timedelta(minutes=turnaround)
                estimated_dep_delay = (earliest_dep - plan_dep).total_seconds() / 60
                if estimated_dep_delay > 0:
                    risk_reasons.append(
                        f"前序{inbound.get('FlightNo')}延误{round(inbound_delay_min)}分钟"
                        f"→预估晚{round(estimated_dep_delay)}分钟")
                else:
                    inbound_delay_min = 0  # 过站时间够，不算风险

    signals["inbound_delay_min"] = round(max(0, inbound_delay_min))
    signals["estimated_dep_delay_min"] = round(max(0, estimated_dep_delay))

    # ---- 信号2: 天气 ----
    signals["dep_weather_score"] = dep_weather_score
    signals["arr_weather_score"] = arr_weather_score
    if dep_weather_reasons:
        risk_reasons.append(f"出发机场: {', '.join(dep_weather_reasons)}")
    # 到达机场天气从航班数据获取
    arr_wx = parse_inline_weather(departing.get("ArrWeather", ""))
    arr_s, arr_r = assess_weather_risk(arr_wx)
    if arr_s > signals["arr_weather_score"]:
        signals["arr_weather_score"] = arr_s
    if arr_r:
        risk_reasons.append(f"到达机场: {', '.join(arr_r)}")

    # ---- 信号3: 飞常准AI预测 ----
    vz_delay, vz_reason = check_veryzhun_prediction(departing)
    signals["veryzhun_delay_min"] = vz_delay
    if vz_reason:
        risk_reasons.append(vz_reason)

    # ---- 信号4: 机场态势 ----
    signals["airport_delay_rate"] = airport_situation.get("delay_rate", 0)
    if airport_situation.get("situation") == "大面积延误":
        risk_reasons.append(
            f"机场大面积延误(延误率{airport_situation['delay_rate']*100:.0f}%)")
    elif airport_situation.get("situation") == "轻微拥堵":
        risk_reasons.append(
            f"机场轻微拥堵(延误率{airport_situation['delay_rate']*100:.0f}%)")

    # ---- 信号5: 历史准点率 ----
    ontime = parse_ontime_rate(departing.get("OntimeRate", ""))
    signals["ontime_rate"] = ontime
    if ontime is not None and ontime < 80:
        risk_reasons.append(f"历史准点率仅{ontime}%")

    # ---- 计算综合分 ----
    total_score = compute_risk_score(signals)

    # 过滤低风险（至少要有一个实质信号）
    if total_score < 15:
        return None

    # 风险等级
    if total_score >= 70:
        level = "极高"
    elif total_score >= 50:
        level = "高"
    elif total_score >= 30:
        level = "中"
    else:
        level = "低"

    result = {
        "departing_flight": departing.get("FlightNo"),
        "departing_route": f"{departing.get('FlightDepcode')}->{departing.get('FlightArrcode')}",
        "plan_departure": departing.get("FlightDeptimePlanDate"),
        "aircraft": departing.get("AircraftNumber"),
        "aircraft_type": departing.get("ftype", ""),
        "current_state": departing.get("FlightState", "计划"),
        "risk_score": total_score,
        "risk_level": level,
        "risk_reasons": risk_reasons,
        "signals": signals,
    }

    # 前序航班详情
    if inbound and inbound_delay_min >= SIGNIFICANT_DELAY_MINUTES:
        result["inbound_flight"] = inbound.get("FlightNo")
        result["inbound_route"] = (f"{inbound.get('FlightDepcode')}"
                                   f"->{inbound.get('FlightArrcode')}")
        result["inbound_state"] = inbound.get("FlightState", "")
        result["inbound_plan_arrival"] = inbound.get("FlightArrtimePlanDate")
        inbound_arr = get_best_arrival_time(inbound)
        if inbound_arr:
            result["inbound_est_arrival"] = inbound_arr.strftime(
                "%Y-%m-%d %H:%M:%S")
        result["inbound_delay_min"] = round(inbound_delay_min)
        turnaround = get_min_turnaround(departing.get("ftype", ""))
        result["min_turnaround_min"] = turnaround
        if estimated_dep_delay > 0:
            earliest = inbound_arr + timedelta(minutes=turnaround)
            result["earliest_possible_dep"] = earliest.strftime(
                "%Y-%m-%d %H:%M:%S")
            result["estimated_dep_delay_min"] = round(estimated_dep_delay)

    # 天气/准点率信息
    dep_wx = parse_inline_weather(departing.get("DepWeather", ""))
    if dep_wx:
        result["dep_weather"] = departing.get("DepWeather")
    arr_wx_str = departing.get("ArrWeather", "")
    if arr_wx_str:
        result["arr_weather"] = arr_wx_str
    if ontime is not None:
        result["ontime_rate"] = f"{ontime}%"
    if vz_delay > 0:
        result["veryzhun_predicted_delay"] = f"{vz_delay}分钟"

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

    print(f"\n{'='*70}")
    print(f"  南航航班延误智能检测器")
    print(f"  检测日期: {date}")
    print(f"  运行时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  检测信号: 前序延误 | 天气 | 机场态势 | AI预测 | 准点率")
    print(f"{'='*70}\n")

    all_risks = []

    for hub, destinations in hubs.items():
        print(f"[枢纽] {hub} — 正在检索航班数据...")

        # ---- 获取机场天气 (1次API调用) ----
        weather_data = api.get_airport_weather(hub)
        hub_weather_score, hub_weather_reasons = assess_airport_weather(
            weather_data)
        if hub_weather_reasons:
            print(f"  [天气] {hub}: {', '.join(hub_weather_reasons)}")
        elif verbose:
            current = weather_data.get("current", {})
            print(f"  [天气] {hub}: {current.get('Type', '未知')}"
                  f" 能见度{current.get('Visib', '?')}m"
                  f" {current.get('WindDirection', '')}{current.get('WindPower', '')}")

        departing_flights = []
        inbound_flights = []
        all_hub_departures = []  # 所有出港航班（不限南航，用于机场态势）

        # ---- 查询航线 ----
        route_tasks = []
        for dest in destinations:
            route_tasks.append(("out", hub, dest))
        for dest in destinations:
            route_tasks.append(("in", dest, hub))

        total = len(route_tasks)
        print(f"  查询 {total} 条航线...")

        for i, (direction, dep, arr) in enumerate(route_tasks):
            flights = api.search_flights(dep, arr, date)

            if direction == "out":
                all_hub_departures.extend(flights)
                for fl in flights:
                    if fl.get("FlightNo", "").startswith("CZ"):
                        departing_flights.append(fl)
            else:
                inbound_flights.extend(flights)

            done = i + 1
            if done % 10 == 0 or done == total:
                print(f"  进度: {done}/{total}  "
                      f"(南航出港: {len(departing_flights)}, "
                      f"进港: {len(inbound_flights)}, "
                      f"API调用: {api.call_count})")

        print(f"  找到 {len(departing_flights)} 个南航出港航班, "
              f"{len(inbound_flights)} 个进港航班")

        if api.error_count > 0:
            print(f"  [注意] 有 {api.error_count} 次 API 请求失败，"
                  f"结果可能不完整")

        # ---- 机场整体态势 ----
        airport_sit = analyze_airport_situation(all_hub_departures)
        sit_icon = {"正常": "✓", "轻微拥堵": "⚠", "大面积延误": "✗"}
        print(f"  [态势] {hub}: {airport_sit['situation']} "
              f"{sit_icon.get(airport_sit['situation'], '')}"
              f"  (延误率{airport_sit['delay_rate']*100:.0f}%"
              f" 取消{airport_sit['cancelled']}班"
              f" 平均延误{airport_sit['avg_delay_min']}分钟)")

        # ---- 建立飞机注册号 -> 进港航班映射 ----
        aircraft_inbound = {}
        for fl in inbound_flights:
            ac = fl.get("AircraftNumber", "").strip()
            if not ac:
                continue
            arr_time = get_best_arrival_time(fl)
            if not arr_time:
                continue
            if ac not in aircraft_inbound:
                aircraft_inbound[ac] = fl
            else:
                existing_arr = get_best_arrival_time(aircraft_inbound[ac])
                if existing_arr and arr_time > existing_arr:
                    aircraft_inbound[ac] = fl

        if verbose:
            print(f"  已建立 {len(aircraft_inbound)} 架飞机的进港映射")

        # ---- 收集目的地机场天气（从航班数据中提取，不额外调API） ----
        dest_weather_cache = {}
        for fl in departing_flights:
            arr_code = fl.get("FlightArrcode", "")
            if arr_code and arr_code not in dest_weather_cache:
                arr_wx = parse_inline_weather(fl.get("ArrWeather", ""))
                if arr_wx:
                    dest_weather_cache[arr_code] = assess_weather_risk(arr_wx)

        # ---- 分析每个待出发CZ航班 ----
        candidates = 0
        for fl in departing_flights:
            state = fl.get("FlightState", "")
            if state in ("到达", "起飞"):
                continue

            ac = fl.get("AircraftNumber", "").strip()
            inbound = None
            if ac:
                inbound = aircraft_inbound.get(ac)
                if inbound and inbound.get("FlightArrcode") != fl.get("FlightDepcode"):
                    inbound = None

            candidates += 1

            # 出发机场天气：优先用航班内嵌数据，否则用API天气
            dep_wx = parse_inline_weather(fl.get("DepWeather", ""))
            if dep_wx:
                dws, dwr = assess_weather_risk(dep_wx)
            else:
                dws, dwr = hub_weather_score, hub_weather_reasons

            # 到达机场天气
            arr_code = fl.get("FlightArrcode", "")
            aws, awr = dest_weather_cache.get(arr_code, (0, []))

            risk = analyze_comprehensive_risk(
                fl, inbound, airport_sit, dws, dwr, aws, awr)
            if risk:
                risk["hub"] = hub
                all_risks.append(risk)

        if verbose:
            print(f"  分析了 {candidates} 个待出发航班")

        print()

    # ---- 输出结果 ----
    print(f"{'='*70}")
    if not all_risks:
        print("  未发现高风险延误航班 ✓")
        print(f"{'='*70}\n")
        print(f"  (共发起 {api.call_count} 次 API 请求, "
              f"{api.error_count} 次失败)")
        return all_risks

    # 按风险分降序
    all_risks.sort(key=lambda r: r["risk_score"], reverse=True)

    print(f"  发现 {len(all_risks)} 个高风险延误航班（未发布延误通知）:")
    print(f"{'='*70}\n")

    for i, risk in enumerate(all_risks, 1):
        level_badge = {"极高": "🔴", "高": "🟠", "中": "🟡", "低": "🟢"}
        badge = level_badge.get(risk["risk_level"], "")

        print(f"  [{i}] {badge} {risk['departing_flight']}  "
              f"{risk['departing_route']}  "
              f"风险: {risk['risk_level']}({risk['risk_score']}分)")
        print(f"      机型: {risk['aircraft_type']}  "
              f"机号: {risk.get('aircraft', 'N/A')}  "
              f"状态: {risk['current_state']}")
        print(f"      计划出发: {risk['plan_departure']}")

        # 风险原因
        if risk["risk_reasons"]:
            print(f"      ---- 风险信号 ----")
            for reason in risk["risk_reasons"]:
                print(f"      • {reason}")

        # 前序航班详情
        if risk.get("inbound_flight"):
            print(f"      ---- 前序航班 ----")
            print(f"      {risk['inbound_flight']}  "
                  f"{risk.get('inbound_route', '')}  "
                  f"状态: {risk.get('inbound_state', '')}")
            print(f"      计划到达: {risk.get('inbound_plan_arrival')}"
                  f"  预计到达: {risk.get('inbound_est_arrival', '')}")
            print(f"      前序延误: {risk.get('inbound_delay_min', 0)} 分钟"
                  f"  →  最早可出发: "
                  f"{risk.get('earliest_possible_dep', 'N/A')}")

        # 补充信息
        extras = []
        if risk.get("ontime_rate"):
            extras.append(f"准点率{risk['ontime_rate']}")
        if risk.get("veryzhun_predicted_delay"):
            extras.append(f"AI预测延误{risk['veryzhun_predicted_delay']}")
        if risk.get("dep_weather"):
            extras.append(f"出发天气: {risk['dep_weather']}")
        if extras:
            print(f"      ---- 参考 ----")
            for e in extras:
                print(f"      {e}")

        print()

    print(f"  (共发起 {api.call_count} 次 API 请求, "
          f"{api.error_count} 次失败)")
    return all_risks


# ============================================================
# CLI 入口
# ============================================================

def main():
    global SIGNIFICANT_DELAY_MINUTES, MIN_TURNAROUND_NARROW, MIN_TURNAROUND_WIDE

    parser = argparse.ArgumentParser(
        description="南航航班延误智能检测器 — 多维信号综合预判延误",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --auto-renew                           # 自动续杯模式
  %(prog)s --hub CAN --dest PKX,PVG,CTU           # 精简扫描
  %(prog)s --threshold 20                         # 降低延误阈值
  %(prog)s --json                                 # JSON输出
        """,
    )
    parser.add_argument(
        "--key", "-k",
        default="sk-OjsivkOdec5Bti_WYiG9Ga1sP_BRQJGlJ9R1d2FaRuQ",
        help="飞常准 API Key (默认使用内置 key)",
    )
    parser.add_argument(
        "--date", "-d",
        default=datetime.now().strftime("%Y-%m-%d"),
        help="查询日期，格式 YYYY-MM-DD (默认今天)",
    )
    parser.add_argument(
        "--hub",
        action="append",
        help="指定枢纽机场代码，可多次使用 (默认: CAN, PKX, URC, SZX)",
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
        default=30,
        help="前序航班延误阈值(分钟)，默认 30",
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
        help="余额不足时自动注册新账号获取 Key（无限续杯）",
    )

    args = parser.parse_args()

    SIGNIFICANT_DELAY_MINUTES = args.threshold
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

    risks = run_detection(
        args.key, args.date, hubs,
        verbose=args.verbose, interval=args.interval,
        auto_renew=args.auto_renew,
    )

    if args.json:
        print(json.dumps(risks, ensure_ascii=False, indent=2))

    sys.exit(0 if not risks else 1)


if __name__ == "__main__":
    main()
