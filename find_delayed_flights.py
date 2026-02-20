#!/usr/bin/env python3
"""
南航前序航班延误检测器
检测前序航班已经明显晚到、但后续航班还未发布航延通知的南航航班。

使用飞常准 (VariFlight) API 获取航班数据。
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta

import requests

# ============================================================
# 配置
# ============================================================

API_URL = "https://mcp.variflight.com/api/v1/mcp/data"

# 南航主要枢纽及高频航线目的地（精简版，优先覆盖高频航线）
# key = 枢纽机场, value = 常飞目的地列表
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

# 最小过站时间（分钟）: 飞机到达后需要的最少地面周转时间
MIN_TURNAROUND_NARROW = 45   # 窄体机
MIN_TURNAROUND_WIDE = 70     # 宽体机

# 宽体机型前缀
WIDEBODY_TYPES = {"A33", "A34", "A35", "A38", "B74", "B77", "B78", "B76"}

# 判定"明显晚到"的阈值：前序航班预计比计划晚到多少分钟算"明显"
SIGNIFICANT_DELAY_MINUTES = 30

# 请求间隔（秒），用于避免触发 API 限速
REQUEST_INTERVAL = 0.6


# ============================================================
# API 调用
# ============================================================

class VariFlightAPI:
    def __init__(self, api_key: str, interval: float = REQUEST_INTERVAL):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "X-VARIFLIGHT-KEY": api_key,
            "Content-Type": "application/json",
        })
        self._interval = interval
        self._last_call = 0.0
        self.call_count = 0
        self.error_count = 0

    def _call(self, endpoint: str, params: dict) -> dict:
        """调用飞常准 API，带限速和重试"""
        body = {"endpoint": endpoint, "params": params}
        for attempt in range(4):
            # 限速
            now = time.monotonic()
            wait = self._interval - (now - self._last_call)
            if wait > 0:
                time.sleep(wait)

            try:
                self._last_call = time.monotonic()
                self.call_count += 1
                resp = self.session.post(API_URL, json=body, timeout=30)

                if resp.status_code == 403:
                    # 检查是否余额不足
                    try:
                        err_data = resp.json()
                        if err_data.get("message") == "Insufficient balance":
                            if not getattr(self, '_balance_warned', False):
                                print("\n  [错误] API 余额不足 (Insufficient balance)，"
                                      "请充值后重试。", file=sys.stderr)
                                self._balance_warned = True
                            self.error_count += 1
                            return []
                    except (json.JSONDecodeError, ValueError):
                        pass
                    # 触发限速，指数退避
                    backoff = 3 * (2 ** attempt)
                    if attempt < 3:
                        print(f"  [限速] 等待 {backoff}s 后重试...",
                              file=sys.stderr)
                        time.sleep(backoff)
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
                    continue
                self.error_count += 1
                return []
            except (requests.RequestException, json.JSONDecodeError) as e:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                self.error_count += 1
                print(f"  [API 错误] {endpoint} {params}: {e}", file=sys.stderr)
                return []

    def search_flights(self, dep: str, arr: str, date: str) -> list:
        """查询两个机场之间的航班"""
        result = self._call("flights", {"dep": dep, "arr": arr, "date": date})
        if isinstance(result, list):
            return result
        return []

    def search_flight_by_number(self, fnum: str, date: str) -> list:
        """按航班号查询"""
        result = self._call("flight", {"fnum": fnum, "date": date})
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and result.get("error_code"):
            return []
        if isinstance(result, dict):
            return [result]
        return []


# ============================================================
# 核心分析逻辑
# ============================================================

def parse_time(time_str: str) -> datetime | None:
    """解析时间字符串"""
    if not time_str or not time_str.strip():
        return None
    try:
        return datetime.strptime(time_str.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return datetime.strptime(time_str.strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            return None


def get_best_arrival_time(flight: dict) -> datetime | None:
    """获取航班的最佳到达时间估计（实际 > 预计 > 飞常准预测 > 计划）"""
    for key in ["FlightArrtimeDate", "FlightArrtimeReadyDate",
                "VeryZhunReadyArrtimeDate", "FlightArrtimePlanDate"]:
        t = parse_time(flight.get(key, ""))
        if t:
            return t
    return None


def get_best_departure_time(flight: dict) -> datetime | None:
    """获取航班的最佳出发时间估计"""
    for key in ["FlightDeptimeDate", "FlightDeptimeReadyDate",
                "VeryZhunReadyDeptimeDate", "FlightDeptimePlanDate"]:
        t = parse_time(flight.get(key, ""))
        if t:
            return t
    return None


def is_widebody(ftype: str) -> bool:
    """判断是否宽体机"""
    if not ftype:
        return False
    return ftype[:3].upper() in WIDEBODY_TYPES


def get_min_turnaround(ftype: str) -> int:
    """获取最小过站时间"""
    return MIN_TURNAROUND_WIDE if is_widebody(ftype) else MIN_TURNAROUND_NARROW


def flight_not_yet_delayed(flight: dict) -> bool:
    """判断航班是否尚未发布航延通知"""
    state = flight.get("FlightState", "")
    state_num = flight.get("FlightStateNum")

    # 已经标记为延误/取消/备降的不算
    if state in ("延误", "取消", "提前取消", "备降", "返航"):
        return False

    # 已经到达的不需要关注
    if state == "到达":
        return False

    # 起飞状态：已经飞了，不再关注
    if state == "起飞":
        return False

    # 检查出发时间是否已被调整（说明可能已发通知）
    plan_dep = parse_time(flight.get("FlightDeptimePlanDate", ""))
    ready_dep = parse_time(flight.get("FlightDeptimeReadyDate", ""))
    if plan_dep and ready_dep:
        diff = (ready_dep - plan_dep).total_seconds() / 60
        if diff >= SIGNIFICANT_DELAY_MINUTES:
            return False

    return True


def analyze_delay_risk(departing: dict, inbound: dict) -> dict | None:
    """
    分析延误风险。
    departing: 待出发的CZ航班
    inbound: 该飞机的前序到达航班
    返回风险分析结果，如果无风险返回 None
    """
    plan_dep = parse_time(departing.get("FlightDeptimePlanDate", ""))
    if not plan_dep:
        return None

    inbound_arr = get_best_arrival_time(inbound)
    inbound_plan_arr = parse_time(inbound.get("FlightArrtimePlanDate", ""))
    if not inbound_arr or not inbound_plan_arr:
        return None

    # 前序航班延误了多少分钟
    inbound_delay_min = (inbound_arr - inbound_plan_arr).total_seconds() / 60

    # 前序航班没有明显延误，跳过
    if inbound_delay_min < SIGNIFICANT_DELAY_MINUTES:
        return None

    # 计算过站时间是否充足
    turnaround = get_min_turnaround(departing.get("ftype", ""))
    earliest_possible_dep = inbound_arr + timedelta(minutes=turnaround)
    dep_delay_min = (earliest_possible_dep - plan_dep).total_seconds() / 60

    # 如果过站后仍然能按时出发，不算风险
    if dep_delay_min <= 0:
        return None

    # 确认当前航班未发布延误通知
    if not flight_not_yet_delayed(departing):
        return None

    inbound_state = inbound.get("FlightState", "")
    return {
        "departing_flight": departing.get("FlightNo"),
        "departing_route": f"{departing.get('FlightDepcode')}->{departing.get('FlightArrcode')}",
        "plan_departure": departing.get("FlightDeptimePlanDate"),
        "aircraft": departing.get("AircraftNumber"),
        "aircraft_type": departing.get("ftype", ""),
        "current_state": departing.get("FlightState", "计划"),
        "inbound_flight": inbound.get("FlightNo"),
        "inbound_route": f"{inbound.get('FlightDepcode')}->{inbound.get('FlightArrcode')}",
        "inbound_plan_arrival": inbound.get("FlightArrtimePlanDate"),
        "inbound_est_arrival": inbound_arr.strftime("%Y-%m-%d %H:%M:%S"),
        "inbound_state": inbound_state,
        "inbound_delay_min": round(inbound_delay_min),
        "min_turnaround_min": turnaround,
        "earliest_possible_dep": earliest_possible_dep.strftime("%Y-%m-%d %H:%M:%S"),
        "estimated_dep_delay_min": round(dep_delay_min),
    }


# ============================================================
# 主流程
# ============================================================

def run_detection(api_key: str, date: str, hubs: dict,
                  verbose: bool = False, interval: float = REQUEST_INTERVAL):
    """运行延误检测"""
    api = VariFlightAPI(api_key, interval=interval)
    now = datetime.now()

    print(f"\n{'='*70}")
    print(f"  南航前序航班延误检测器")
    print(f"  检测日期: {date}")
    print(f"  运行时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}\n")

    all_risks = []

    for hub, destinations in hubs.items():
        print(f"[枢纽] {hub} — 正在检索航班数据...")

        departing_flights = []   # 从hub出发的CZ航班
        inbound_flights = []     # 到达hub的所有航班

        # 构建查询列表: 出港 + 进港
        route_tasks = []
        for dest in destinations:
            route_tasks.append(("out", hub, dest))
        for dest in destinations:
            route_tasks.append(("in", dest, hub))

        total = len(route_tasks)
        print(f"  查询 {total} 条航线 (逐条请求以避免限速)...")

        # 逐条顺序请求
        for i, (direction, dep, arr) in enumerate(route_tasks):
            flights = api.search_flights(dep, arr, date)

            if direction == "out":
                for fl in flights:
                    if fl.get("FlightNo", "").startswith("CZ"):
                        departing_flights.append(fl)
            else:
                inbound_flights.extend(flights)

            # 进度显示
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

        # ---- 建立飞机注册号 -> 进港航班映射 ----
        aircraft_inbound = {}
        for fl in inbound_flights:
            ac = fl.get("AircraftNumber", "").strip()
            if not ac:
                continue
            arr_time = get_best_arrival_time(fl)
            if not arr_time:
                continue
            # 保留最晚到达的进港航班（即直接前序）
            if ac not in aircraft_inbound:
                aircraft_inbound[ac] = fl
            else:
                existing_arr = get_best_arrival_time(aircraft_inbound[ac])
                if existing_arr and arr_time > existing_arr:
                    aircraft_inbound[ac] = fl

        if verbose:
            print(f"  已建立 {len(aircraft_inbound)} 架飞机的进港映射")

        # ---- 分析每个待出发CZ航班 ----
        candidates = 0
        for fl in departing_flights:
            ac = fl.get("AircraftNumber", "").strip()
            if not ac:
                continue

            # 只关注还没出发的航班
            state = fl.get("FlightState", "")
            if state in ("到达", "起飞"):
                continue

            inbound = aircraft_inbound.get(ac)
            if not inbound:
                continue

            # 确保前序航班到达的机场和当前航班出发的机场一致
            if inbound.get("FlightArrcode") != fl.get("FlightDepcode"):
                continue

            candidates += 1
            risk = analyze_delay_risk(fl, inbound)
            if risk:
                risk["hub"] = hub
                all_risks.append(risk)

        if verbose:
            print(f"  分析了 {candidates} 个待出发航班")

        print()

    # ---- 输出结果 ----
    print(f"{'='*70}")
    if not all_risks:
        print("  未发现前序延误但未通知的南航航班 ✓")
        print(f"{'='*70}\n")
        print(f"  (共发起 {api.call_count} 次 API 请求, "
              f"{api.error_count} 次失败)")
        return all_risks

    # 按预估延误时间降序排列
    all_risks.sort(key=lambda r: r["estimated_dep_delay_min"], reverse=True)

    print(f"  发现 {len(all_risks)} 个疑似前序延误但未发布通知的航班:")
    print(f"{'='*70}\n")

    for i, risk in enumerate(all_risks, 1):
        print(f"  [{i}] {risk['departing_flight']}  "
              f"{risk['departing_route']}  "
              f"机型: {risk['aircraft_type']}  "
              f"机号: {risk['aircraft']}")
        print(f"      计划出发: {risk['plan_departure']}")
        print(f"      当前状态: {risk['current_state']}")
        print(f"      ----")
        print(f"      前序航班: {risk['inbound_flight']}  "
              f"{risk['inbound_route']}  "
              f"状态: {risk['inbound_state']}")
        print(f"      前序计划到达: {risk['inbound_plan_arrival']}")
        print(f"      前序预计到达: {risk['inbound_est_arrival']}")
        print(f"      前序延误: {risk['inbound_delay_min']} 分钟")
        print(f"      ----")
        print(f"      最小过站时间: {risk['min_turnaround_min']} 分钟")
        print(f"      最早可能出发: {risk['earliest_possible_dep']}")
        print(f"      预估出发延误: ~{risk['estimated_dep_delay_min']} 分钟")
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
        description="南航前序航班延误检测器 — 找出前序已晚但航延通知未发的航班",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s                                      # 使用默认配置检测今天
  %(prog)s --date 2026-02-21                    # 检测指定日期
  %(prog)s --hub CAN                            # 仅检测广州枢纽
  %(prog)s --hub CAN --dest PVG,PKX,CTU         # 自定义目的地
  %(prog)s --threshold 20                       # 降低延误阈值到20分钟
  %(prog)s --interval 1.0                       # 加大请求间隔避免限速
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
        help="自定义目的地列表，逗号分隔 (覆盖默认目的地，与 --hub 配合使用)",
    )
    parser.add_argument(
        "--turnaround",
        type=int,
        default=None,
        help="自定义最小过站时间(分钟)，覆盖默认值",
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

    args = parser.parse_args()

    # 更新全局阈值
    SIGNIFICANT_DELAY_MINUTES = args.threshold
    if args.turnaround is not None:
        MIN_TURNAROUND_NARROW = args.turnaround
        MIN_TURNAROUND_WIDE = args.turnaround

    # 确定枢纽和目的地
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

    # 运行检测
    risks = run_detection(
        args.key, args.date, hubs,
        verbose=args.verbose, interval=args.interval,
    )

    if args.json:
        print(json.dumps(risks, ensure_ascii=False, indent=2))

    sys.exit(0 if not risks else 1)


if __name__ == "__main__":
    main()
