#!/usr/bin/env python3
"""
航变检测结果验证器 — 独立进程，持续验证主检测器的预测准确性

工作流程：
  1. 读取检测日志（主检测器每次运行追加记录）
  2. 航班实际起飞/到达后，重新查询获取真实结果
  3. 对比预测 vs 实际，评估准确性
  4. 不靠谱的预测 → 自动在 GitHub 创建 Issue，附上详细 context
  5. 后续根据 Issue 分析并优化检测逻辑

使用:
  python validate_detections.py --auto-renew
  python validate_detections.py --auto-renew --monitor --cycle 60
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from auto_renew_key import obtain_new_key

BJT = timezone(timedelta(hours=8))

DETECTION_LOG_FILE = ".detection_log.json"
LOG_MAX_AGE_DAYS = 7

# 航班起飞后等多久再验证（给足够时间让数据更新）
VALIDATION_WAIT_HOURS = 3

API_URL = "https://mcp.variflight.com/api/v1/mcp/data"
REQUEST_INTERVAL = 0.6

# 国内航班延误判定标准：撤轮挡时间比计划起飞晚 > 15分钟即为延误
OFFICIAL_DELAY_THRESHOLD = 15

# 创建 Issue 的准确性阈值
ISSUE_ACCURACY_THRESHOLDS = ("false_positive", "overpredicted")


def beijing_now() -> datetime:
    return datetime.now(BJT).replace(tzinfo=None)


def parse_time(time_str: str) -> datetime | None:
    if not time_str or not time_str.strip():
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(time_str.strip(), fmt)
        except ValueError:
            continue
    return None


# ============================================================
# 检测日志操作
# ============================================================

def load_detection_log(filepath: str) -> list:
    try:
        with open(filepath) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_detection_log(filepath: str, log: list):
    with open(filepath, "w") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def append_to_detection_log(filepath: str, hits: list) -> int:
    """由主检测器调用，追加本次检测到的机会到日志"""
    log = load_detection_log(filepath)
    now = beijing_now()
    existing_ids = {e["id"] for e in log}

    added = 0
    for hit in hits:
        entry_id = f"{hit['flight']}|{hit['plan_departure']}"
        if entry_id in existing_ids:
            continue

        log.append({
            "id": entry_id,
            "detected_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "flight_no": hit["flight"],
            "route": hit["route"],
            "dep_city": hit.get("dep_city", ""),
            "dep_code": (hit["route"].split(" → ")[0].strip()
                         if " → " in hit["route"] else ""),
            "arr_code": (hit["route"].split(" → ")[1].strip()
                         if " → " in hit["route"] else ""),
            "plan_departure": hit["plan_departure"],
            "plan_arrival": hit.get("plan_arrival", ""),
            "predicted_delay_min": hit["estimated_delay_min"],
            "aircraft": hit["aircraft"],
            "aircraft_type": hit.get("aircraft_type", ""),
            "certainty": hit.get("certainty", ""),
            "inbound_flight": hit.get("inbound_flight", ""),
            "inbound_route": hit.get("inbound_route", ""),
            "inbound_delay_min": hit.get("inbound_delay_min", 0),
            "inbound_state": hit.get("inbound_state", ""),
            "hub": hit.get("hub", ""),
            "hub_reliability": hit.get("hub_reliability", "medium"),
            "probability": hit.get("probability", 50),
            "is_priority": hit.get("is_priority", False),
            "validated": False,
            "validation": None,
        })
        added += 1

    # 清理超过 7 天的旧记录
    cutoff = now - timedelta(days=LOG_MAX_AGE_DAYS)
    log = [e for e in log
           if parse_time(e.get("detected_at", ""))
           and parse_time(e["detected_at"]) > cutoff]

    save_detection_log(filepath, log)
    return added


# ============================================================
# 简易 API 客户端（复用飞常准接口）
# ============================================================

class SimpleAPI:
    def __init__(self, api_key: str, auto_renew: bool = False):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "X-VARIFLIGHT-KEY": api_key,
            "Content-Type": "application/json",
        })
        self._auto_renew = auto_renew
        self._last_call = 0.0

    def search_flights(self, dep: str, arr: str, date: str) -> list:
        now = time.monotonic()
        wait = REQUEST_INTERVAL - (now - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

        body = {"endpoint": "flights",
                "params": {"dep": dep, "arr": arr, "date": date}}
        try:
            resp = self.session.post(API_URL, json=body, timeout=30)
            if resp.status_code == 403:
                try:
                    err = resp.json()
                except (json.JSONDecodeError, ValueError):
                    return []
                if (err.get("message") == "Insufficient balance"
                        and self._auto_renew):
                    new_key = obtain_new_key(verbose=True)
                    self.api_key = new_key
                    self.session.headers["X-VARIFLIGHT-KEY"] = new_key
                    time.sleep(10)
                    return self.search_flights(dep, arr, date)
                return []
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", []) if data.get("code") == 200 else []
        except Exception:
            return []


# ============================================================
# 验证逻辑
# ============================================================

def validate_entry(api: SimpleAPI, entry: dict) -> dict | None:
    """重新查询航班，对比预测与实际结果"""
    dep = entry.get("dep_code", "")
    arr = entry.get("arr_code", "")
    date = entry["plan_departure"][:10]

    if not dep or not arr:
        return None

    flights = api.search_flights(dep, arr, date)

    target = None
    for fl in flights:
        if fl.get("FlightNo") == entry["flight_no"]:
            target = fl
            break

    if not target:
        return None  # 航班数据未找到

    now = beijing_now()
    actual_state = target.get("FlightState", "")
    actual_dep = parse_time(target.get("FlightDeptimeDate", ""))
    actual_arr = parse_time(target.get("FlightArrtimeDate", ""))
    actual_aircraft = target.get("AircraftNumber", "").strip()
    actual_type = target.get("ftype", "")
    plan_dep = parse_time(entry["plan_departure"])

    # 检查是否到达终态
    is_terminal = actual_state in ("到达", "取消", "提前取消", "备降", "返航")

    # 如果未到终态且等待时间不够，跳过
    if not is_terminal and plan_dep:
        hours_since = (now - plan_dep).total_seconds() / 3600
        if hours_since < VALIDATION_WAIT_HOURS:
            return None  # 数据还不充分

    # 计算实际延误
    actual_delay_min = 0
    if actual_dep and plan_dep:
        actual_delay_min = round(
            (actual_dep - plan_dep).total_seconds() / 60)

    predicted_delay = entry["predicted_delay_min"]
    error = abs(predicted_delay - actual_delay_min)

    # 飞机调换检测
    original_aircraft = entry["aircraft"]
    aircraft_changed = bool(
        actual_aircraft and actual_aircraft != original_aircraft)
    type_changed = False
    if aircraft_changed and actual_type and entry.get("aircraft_type"):
        type_changed = actual_type != entry["aircraft_type"]

    # 准确性分级（国标：撤轮挡晚于计划>15分钟即延误）
    if actual_state in ("取消", "提前取消"):
        accuracy = "cancelled"
    elif actual_delay_min < OFFICIAL_DELAY_THRESHOLD:
        accuracy = "false_positive"
    elif actual_delay_min >= OFFICIAL_DELAY_THRESHOLD and error <= 30:
        accuracy = "good"
    elif actual_delay_min >= OFFICIAL_DELAY_THRESHOLD and error <= 60:
        accuracy = "fair"
    elif predicted_delay > actual_delay_min and error > 60:
        accuracy = "overpredicted"
    elif predicted_delay < actual_delay_min and error > 60:
        accuracy = "underpredicted"
    else:
        accuracy = "fair"

    root_cause = analyze_root_cause(
        entry, accuracy, aircraft_changed, type_changed,
        actual_delay_min)

    return {
        "checked_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "actual_dep_time": (actual_dep.strftime("%Y-%m-%d %H:%M")
                            if actual_dep else ""),
        "actual_arr_time": (actual_arr.strftime("%Y-%m-%d %H:%M")
                            if actual_arr else ""),
        "actual_delay_min": actual_delay_min,
        "actual_aircraft": actual_aircraft,
        "actual_aircraft_type": actual_type,
        "actual_state": actual_state,
        "aircraft_changed": aircraft_changed,
        "type_changed": type_changed,
        "prediction_error_min": error,
        "accuracy": accuracy,
        "root_cause": root_cause,
        "issue_number": None,
        "issue_url": None,
    }


def analyze_root_cause(entry: dict, accuracy: str,
                       aircraft_changed: bool, type_changed: bool,
                       actual_delay: int) -> str:
    """分析预测失败的根因"""
    reasons = []
    hub = entry.get("hub", "")
    hub_reliability = entry.get("hub_reliability", "medium")

    if accuracy == "false_positive":
        if aircraft_changed:
            swap_type = "机型更换" if type_changed else "同机型调换"
            reasons.append(
                f"飞机被{swap_type} "
                f"({entry['aircraft']}→实际机号), "
                f"前序延误通过调机解决")
            if hub_reliability == "low":
                reasons.append(
                    f"{hub}为大枢纽(可靠性=low)，机队充裕调机能力强，"
                    f"应降低纯前序延误预测的置信度")
            elif hub_reliability == "high":
                reasons.append(
                    f"{hub}为小机场(可靠性=high)但仍发生调机，"
                    f"该机场调机能力可能被低估")
        else:
            reasons.append(
                "飞机未调换但航班准时，"
                "可能过站时间估算偏大或前序延误被在途恢复")

    elif accuracy == "overpredicted":
        reasons.append(
            f"预测偏高: 预测{entry['predicted_delay_min']}分 "
            f"实际{actual_delay}分 "
            f"误差{abs(entry['predicted_delay_min'] - actual_delay)}分")
        if aircraft_changed:
            reasons.append("飞机被调换，部分延误被吸收")

    elif accuracy in ("good", "fair"):
        reasons.append("预测基本准确")

    elif accuracy == "cancelled":
        reasons.append("航班取消（非延误），预测场景变化")

    elif accuracy == "underpredicted":
        reasons.append(
            f"预测偏低: 预测{entry['predicted_delay_min']}分 "
            f"实际{actual_delay}分, 实际延误比预期更严重")

    return "; ".join(reasons) if reasons else "待分析"


# ============================================================
# GitHub Issue 创建
# ============================================================

def build_issue_title(entry: dict, validation: dict) -> str:
    labels = {
        "false_positive": "误报",
        "overpredicted": "偏高",
        "underpredicted": "偏低",
        "cancelled": "取消",
    }
    label = labels.get(validation["accuracy"], validation["accuracy"])
    return (f"[{label}] {entry['flight_no']} {entry['route']} "
            f"预测{entry['predicted_delay_min']}分"
            f"实际{validation['actual_delay_min']}分")


def build_issue_body(entry: dict, validation: dict) -> str:
    ac_info = ""
    if validation["aircraft_changed"]:
        swap_type = "机型更换" if validation.get("type_changed") else "同型调换"
        ac_info = (f" ({swap_type}: "
                   f"{entry.get('aircraft_type', '')}→"
                   f"{validation['actual_aircraft_type']})")

    body = f"""## 检测结果验证: {entry['flight_no']} {entry['route']}

### 预测信息
| 项目 | 值 |
|------|-----|
| 航班 | {entry['flight_no']} {entry['route']} ({entry.get('dep_city', '')}) |
| 检测时间 | {entry['detected_at']} |
| 预测延误 | ~{entry['predicted_delay_min']} 分钟 |
| 确定性 | {entry.get('certainty', '')} |
| 前序航班 | {entry.get('inbound_flight', '')} {entry.get('inbound_route', '')} 延误{entry.get('inbound_delay_min', 0)}分 状态:{entry.get('inbound_state', '')} |
| 机号/机型 | {entry['aircraft']} ({entry.get('aircraft_type', '')}) |
| 枢纽 | {entry.get('hub', '')} |

### 实际结果
| 项目 | 值 |
|------|-----|
| 实际起飞 | {validation.get('actual_dep_time') or '未知'} |
| 实际延误 | {validation['actual_delay_min']} 分钟 |
| 最终机号 | {validation['actual_aircraft']} ({validation['actual_aircraft_type']}){ac_info} |
| 最终状态 | {validation['actual_state']} |
| 预测误差 | {validation['prediction_error_min']} 分钟 |
| 准确性 | **{validation['accuracy']}** |

### 根因分析

{validation['root_cause']}

### 改进建议

"""
    # 根据不同类型给出具体改进建议
    if validation["accuracy"] == "false_positive":
        if validation["aircraft_changed"]:
            body += """1. **大枢纽调机概率高** — CAN/PKX/SZX 等大枢纽机队充裕，前序延误不一定传导
2. **增加"调机概率因子"** — 根据枢纽大小、机队规模加权，大枢纽降低置信度
3. **跟踪系统已实现调换检测** — 但预测阶段就应提前标注"调机风险高"
4. **考虑同机型可用运力** — 查询该枢纽同机型可用飞机数量，评估调机可能性
"""
        else:
            body += """1. **过站时间估算可能偏大** — 实际操作可能比最小过站时间更快
2. **前序延误可能在途恢复** — 空中加速、缩短航路等因素未纳入
3. **考虑引入历史延误传导率** — 统计同航线前序延误的实际传导比例
4. **参考预计到达时间的更新频率** — 如果预计到达时间在持续修正，应动态更新
"""
    elif validation["accuracy"] == "overpredicted":
        body += """1. **预测偏高** — 实际延误远小于预测，可能是过站时间余量过大
2. **考虑在途恢复因素** — 航班可能空中加速弥补部分延误
"""
        if validation["aircraft_changed"]:
            body += "3. **飞机被调换** — 调机部分吸收了延误，应考虑调机可能性\n"

    body += f"""
### 相关代码

- `find_delayed_flights.py:analyze_inbound_chain()` — 核心判定逻辑（条件1-4）
- `find_delayed_flights.py:find_predecessor()` — 前序航班时间先后匹配
- `find_delayed_flights.py:get_min_turnaround()` — 最小过站时间
- `find_delayed_flights.py:check_tracked_flights()` — 飞机调换跟踪

<details>
<summary>原始检测数据 (JSON)</summary>

```json
{json.dumps(entry, ensure_ascii=False, indent=2)}
```

</details>

<details>
<summary>验证结果 (JSON)</summary>

```json
{json.dumps(validation, ensure_ascii=False, indent=2)}
```

</details>
"""
    return body


def create_github_issue(entry: dict, validation: dict,
                        repo: str = None) -> tuple[int | None, str | None]:
    """通过 gh CLI 创建 Issue"""
    title = build_issue_title(entry, validation)
    body = build_issue_body(entry, validation)

    # 标签
    labels = ["auto-validation"]
    accuracy = validation["accuracy"]
    if accuracy == "false_positive":
        labels.append("false-positive")
    elif accuracy in ("overpredicted", "underpredicted"):
        labels.append("prediction-accuracy")
    if validation.get("aircraft_changed"):
        labels.append("aircraft-swap")

    try:
        cmd = ["gh", "issue", "create",
               "--title", title,
               "--body", body]
        for label in labels:
            cmd.extend(["--label", label])
        if repo:
            cmd.extend(["--repo", repo])

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30)

        if result.returncode == 0:
            issue_url = result.stdout.strip()
            match = re.search(r'/issues/(\d+)', issue_url)
            issue_number = int(match.group(1)) if match else None
            print(f"  [Issue] 已创建: {issue_url}", file=sys.stderr)
            return issue_number, issue_url
        else:
            # gh CLI 报错（可能 label 不存在等），降级输出
            print(f"  [Issue] gh 创建失败: {result.stderr.strip()}",
                  file=sys.stderr)
            # 尝试不带 label 重试
            cmd_no_label = ["gh", "issue", "create",
                            "--title", title, "--body", body]
            if repo:
                cmd_no_label.extend(["--repo", repo])
            result2 = subprocess.run(
                cmd_no_label, capture_output=True, text=True, timeout=30)
            if result2.returncode == 0:
                issue_url = result2.stdout.strip()
                match = re.search(r'/issues/(\d+)', issue_url)
                issue_number = int(match.group(1)) if match else None
                print(f"  [Issue] 已创建 (无标签): {issue_url}",
                      file=sys.stderr)
                return issue_number, issue_url
            print(f"  [Issue] 重试也失败: {result2.stderr.strip()}",
                  file=sys.stderr)
            return None, None

    except FileNotFoundError:
        print("  [Issue] gh CLI 未安装，仅打印 Issue 内容:",
              file=sys.stderr)
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"标题: {title}", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)
        print(body, file=sys.stderr)
        print(f"{'='*60}\n", file=sys.stderr)
        return None, None
    except Exception as e:
        print(f"  [Issue 失败] {e}", file=sys.stderr)
        return None, None


# ============================================================
# 验证结果邮件通知
# ============================================================

def build_validation_email_html(results: list) -> str:
    """构建验证结果汇总邮件 HTML"""
    now_str = beijing_now().strftime("%Y-%m-%d %H:%M")
    total = len(results)
    good = sum(1 for r in results if r["accuracy"] in ("good", "fair"))
    bad = sum(1 for r in results if r["accuracy"] == "false_positive")
    over = sum(1 for r in results if r["accuracy"] == "overpredicted")
    under = sum(1 for r in results if r["accuracy"] == "underpredicted")
    cancelled = sum(1 for r in results if r["accuracy"] == "cancelled")
    swapped = sum(1 for r in results if r.get("aircraft_changed"))

    accuracy_rate = round(good / total * 100) if total > 0 else 0
    banner_bg = "#27ae60" if accuracy_rate >= 70 else (
        "#e67e22" if accuracy_rate >= 40 else "#c0392b")

    rows_html = ""
    for r in results:
        entry = r["entry"]
        val = r["validation"]
        acc = val["accuracy"]

        # 准确性颜色和标签
        acc_colors = {
            "good": ("#27ae60", "准确"), "fair": ("#2ecc71", "基本准确"),
            "false_positive": ("#c0392b", "误报"),
            "overpredicted": ("#e67e22", "偏高"),
            "underpredicted": ("#3498db", "偏低"),
            "cancelled": ("#95a5a6", "取消"),
        }
        acc_color, acc_label = acc_colors.get(acc, ("#95a5a6", acc))

        swap_tag = ""
        if val.get("aircraft_changed"):
            swap_type = "机型更换" if val.get("type_changed") else "同型调换"
            swap_tag = (f' <span style="background:#9b59b6; color:white; '
                        f'padding:1px 6px; border-radius:3px; '
                        f'font-size:11px;">{swap_type}</span>')

        issue_link = ""
        if val.get("issue_url"):
            issue_link = (f' <a href="{val["issue_url"]}" '
                          f'style="color:#3498db; font-size:11px;">'
                          f'Issue #{val.get("issue_number", "")}</a>')

        prob = entry.get("probability", "")
        prob_str = f"{prob}%" if prob else ""

        rows_html += f"""
        <tr style="border-bottom:1px solid #eee;">
          <td style="padding:8px; font-weight:bold;">
            {entry['flight_no']}<br/>
            <span style="font-weight:normal; color:#666; font-size:12px;">
            {entry['route']}</span>
          </td>
          <td style="padding:8px; text-align:center;">
            <span style="background:{acc_color}; color:white;
              padding:3px 10px; border-radius:10px; font-size:12px;
              font-weight:bold;">{acc_label}</span>
            {swap_tag}{issue_link}
          </td>
          <td style="padding:8px; text-align:center; font-size:13px;">
            {prob_str}
          </td>
          <td style="padding:8px; text-align:center; font-size:13px;">
            预测 {entry['predicted_delay_min']}分<br/>
            实际 {val['actual_delay_min']}分<br/>
            <span style="color:{acc_color};">误差 {val['prediction_error_min']}分</span>
          </td>
          <td style="padding:8px; font-size:12px; color:#666;">
            {val.get('root_cause', '')}
          </td>
        </tr>"""

    return f"""
    <div style="font-family:Arial,sans-serif; max-width:800px; margin:auto;">
      <div style="background:{banner_bg}; color:white; padding:16px 20px;
              border-radius:8px 8px 0 0;">
        <h2 style="margin:0;">预测准确性验证报告</h2>
        <p style="margin:6px 0 0; opacity:0.9;">{now_str}</p>
      </div>

      <div style="background:#f8f9fa; padding:16px 20px; display:flex;
              gap:20px; flex-wrap:wrap;">
        <div style="text-align:center; flex:1;">
          <div style="font-size:28px; font-weight:bold; color:{banner_bg};">
            {accuracy_rate}%</div>
          <div style="color:#666; font-size:13px;">准确率</div>
        </div>
        <div style="text-align:center; flex:1;">
          <div style="font-size:28px; font-weight:bold;">{total}</div>
          <div style="color:#666; font-size:13px;">验证总数</div>
        </div>
        <div style="text-align:center; flex:1;">
          <div style="font-size:28px; font-weight:bold; color:#27ae60;">
            {good}</div>
          <div style="color:#666; font-size:13px;">准确</div>
        </div>
        <div style="text-align:center; flex:1;">
          <div style="font-size:28px; font-weight:bold; color:#c0392b;">
            {bad}</div>
          <div style="color:#666; font-size:13px;">误报</div>
        </div>
        <div style="text-align:center; flex:1;">
          <div style="font-size:28px; font-weight:bold; color:#9b59b6;">
            {swapped}</div>
          <div style="color:#666; font-size:13px;">飞机调换</div>
        </div>
      </div>

      <table style="width:100%; border-collapse:collapse; margin-top:8px;">
        <thead>
          <tr style="background:#34495e; color:white;">
            <th style="padding:10px; text-align:left;">航班</th>
            <th style="padding:10px; text-align:center;">结果</th>
            <th style="padding:10px; text-align:center;">概率</th>
            <th style="padding:10px; text-align:center;">延误对比</th>
            <th style="padding:10px; text-align:left;">根因</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>

      <div style="padding:12px 20px; background:#ecf0f1; color:#666;
              font-size:12px; border-radius:0 0 8px 8px; margin-top:4px;">
        准确 {good} | 误报 {bad} | 偏高 {over} | 偏低 {under}
        | 取消 {cancelled} | 飞机调换 {swapped}
      </div>
    </div>"""


def send_validation_email(to_addr: str, resend_key: str,
                          results: list) -> bool:
    """发送验证结果邮件"""
    if not results:
        return False

    total = len(results)
    good = sum(1 for r in results if r["accuracy"] in ("good", "fair"))
    bad = sum(1 for r in results if r["accuracy"] == "false_positive")
    rate = round(good / total * 100) if total > 0 else 0

    subject = (f"[准确性报告] 验证{total}条 "
               f"准确率{rate}% 误报{bad}条")

    html = build_validation_email_html(results)

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": "Flight Monitor <onboarding@resend.dev>",
                "to": [to_addr],
                "subject": subject,
                "html": html,
            },
            timeout=30,
        )
        if resp.status_code in (200, 201):
            print(f"  [邮件] 验证报告已发送到 {to_addr}", file=sys.stderr)
            return True
        else:
            print(f"  [邮件失败] Resend 返回 {resp.status_code}: {resp.text}",
                  file=sys.stderr)
            return False
    except Exception as e:
        print(f"  [邮件失败] {e}", file=sys.stderr)
        return False


# ============================================================
# 主验证流程
# ============================================================

def run_validation(api_key: str, log_file: str, repo: str = None,
                   auto_renew: bool = False, verbose: bool = False,
                   create_issues: bool = True,
                   email: str = None,
                   resend_key: str = None) -> tuple[int, int]:
    """
    执行一轮验证。
    返回 (validated_count, issues_created)。
    """
    log = load_detection_log(log_file)
    if not log:
        if verbose:
            print("  [验证器] 检测日志为空，无需验证", file=sys.stderr)
        return 0, 0

    now = beijing_now()
    api = SimpleAPI(api_key, auto_renew=auto_renew)
    validation_results = []  # 收集本轮验证结果，用于发邮件

    validated_count = 0
    issues_created = 0
    good_count = 0
    pending_count = 0

    for entry in log:
        if entry.get("validated"):
            continue

        plan_dep = parse_time(entry.get("plan_departure", ""))
        if not plan_dep:
            continue

        # 距计划起飞不到 VALIDATION_WAIT_HOURS → 跳过
        hours_since = (now - plan_dep).total_seconds() / 3600
        if hours_since < VALIDATION_WAIT_HOURS:
            pending_count += 1
            if verbose:
                print(f"  [等待] {entry['flight_no']} "
                      f"距起飞 {hours_since:.1f}h, "
                      f"需等 {VALIDATION_WAIT_HOURS}h 后验证",
                      file=sys.stderr)
            continue

        print(f"  [验证] {entry['flight_no']} {entry['route']} "
              f"(预测延误{entry['predicted_delay_min']}分)...",
              file=sys.stderr)

        validation = validate_entry(api, entry)
        if validation is None:
            if verbose:
                print(f"    数据不充分，稍后重试", file=sys.stderr)
            continue

        entry["validated"] = True
        entry["validation"] = validation
        validated_count += 1
        validation_results.append({
            "entry": entry,
            "validation": validation,
            "accuracy": validation["accuracy"],
        })

        accuracy = validation["accuracy"]
        error = validation["prediction_error_min"]

        # 彩色状态显示
        if accuracy in ("good", "fair"):
            status = "✅"
            good_count += 1
        elif accuracy == "false_positive":
            status = "❌ 误报"
        elif accuracy == "overpredicted":
            status = "⚠️ 偏高"
        elif accuracy == "cancelled":
            status = "🚫 取消"
        else:
            status = f"📊 {accuracy}"

        print(f"    {status} — "
              f"预测{entry['predicted_delay_min']}分 "
              f"实际{validation['actual_delay_min']}分 "
              f"误差{error}分"
              f"{' 飞机调换!' if validation['aircraft_changed'] else ''}",
              file=sys.stderr)

        # 创建 Issue（只针对不靠谱的预测）
        if (create_issues
                and accuracy in ISSUE_ACCURACY_THRESHOLDS):
            issue_num, issue_url = create_github_issue(
                entry, validation, repo)
            if issue_url:
                validation["issue_number"] = issue_num
                validation["issue_url"] = issue_url
                issues_created += 1

    # 保存更新后的日志
    save_detection_log(log_file, log)

    # 汇总
    total = len(log)
    already_validated = sum(1 for e in log if e.get("validated"))
    print(f"\n  [验证摘要] "
          f"总记录 {total} | "
          f"已验证 {already_validated} | "
          f"待验证 {pending_count} | "
          f"本次验证 {validated_count} "
          f"(准确 {good_count}, Issue {issues_created})",
          file=sys.stderr)

    # 发送验证结果邮件
    if validation_results and email and resend_key:
        send_validation_email(email, resend_key, validation_results)

    return validated_count, issues_created


# ============================================================
# 每日回测
# ============================================================

def run_daily_review(api_key: str, log_file: str,
                     auto_renew: bool = False, verbose: bool = False,
                     email: str = None, resend_key: str = None,
                     repo: str = None,
                     create_issues: bool = True):
    """
    每日回测：回顾前一天所有检测结果，验证预测准确性并发送日报。
    应在每天早上6点执行 — 此时前一天所有航班都已到达。
    """
    now = beijing_now()
    yesterday = (now - timedelta(days=1)).date()

    # ---- 诊断信息 ----
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  [日报] 每日回测开始", file=sys.stderr)
    print(f"  [日报] 当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)",
          file=sys.stderr)
    print(f"  [日报] 回测日期: {yesterday}", file=sys.stderr)
    print(f"  [日报] 日志文件: {log_file}", file=sys.stderr)
    print(f"  [日报] 日志文件存在: {os.path.exists(log_file)}", file=sys.stderr)
    print(f"  [日报] 邮箱配置: {'已设置' if email else '未设置'} ({email or '-'})",
          file=sys.stderr)
    print(f"  [日报] Resend Key: {'已设置' if resend_key else '未设置'}",
          file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    log = load_detection_log(log_file)
    print(f"  [日报] 日志总记录数: {len(log)}", file=sys.stderr)

    if log and verbose:
        # 打印所有记录的日期分布，帮助诊断
        date_counts = {}
        for entry in log:
            det_time = parse_time(entry.get("detected_at", ""))
            if det_time:
                d = str(det_time.date())
                date_counts[d] = date_counts.get(d, 0) + 1
        print(f"  [日报] 日志日期分布: {date_counts}", file=sys.stderr)

    if not log:
        print("  [日报] 检测日志为空", file=sys.stderr)
        # 日志为空也发邮件，让用户知道系统在运行
        if email and resend_key:
            print("  [日报] 发送空日报邮件...", file=sys.stderr)
            _send_daily_report_email(email, resend_key, yesterday, [], [])
        else:
            print("  [日报] 邮件未配置，跳过发送", file=sys.stderr)
        return

    # 筛选前一天检测到的记录
    yesterday_entries = []
    for entry in log:
        det_time = parse_time(entry.get("detected_at", ""))
        if det_time and det_time.date() == yesterday:
            yesterday_entries.append(entry)

    if not yesterday_entries:
        print(f"  [日报] {yesterday} 无检测记录", file=sys.stderr)
        # 即使无记录也发一封空日报，让用户知道系统在正常运行
        if email and resend_key:
            print("  [日报] 发送空日报邮件...", file=sys.stderr)
            _send_daily_report_email(email, resend_key, yesterday, [], [])
        else:
            print("  [日报] 邮件未配置，跳过发送", file=sys.stderr)
        return

    print(f"  [日报] {yesterday} 共 {len(yesterday_entries)} 条检测记录",
          file=sys.stderr)

    api = SimpleAPI(api_key, auto_renew=auto_renew)
    results = []
    issues_created = 0

    for entry in yesterday_entries:
        # 已验证的直接用已有结果
        if entry.get("validated") and entry.get("validation"):
            results.append({
                "entry": entry,
                "validation": entry["validation"],
                "accuracy": entry["validation"]["accuracy"],
            })
            if verbose:
                print(f"  [已验证] {entry['flight_no']} {entry['route']} "
                      f"→ {entry['validation']['accuracy']}",
                      file=sys.stderr)
            continue

        # 未验证的现在验证（早上6点，前一天航班都已落地）
        print(f"  [验证] {entry['flight_no']} {entry['route']} "
              f"(预测延误{entry['predicted_delay_min']}分)...",
              file=sys.stderr)

        validation = validate_entry(api, entry)
        if validation is None:
            # 到了第二天早上还查不到，标记为数据缺失
            validation = {
                "checked_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "actual_dep_time": "",
                "actual_arr_time": "",
                "actual_delay_min": 0,
                "actual_aircraft": "",
                "actual_aircraft_type": "",
                "actual_state": "数据缺失",
                "aircraft_changed": False,
                "type_changed": False,
                "prediction_error_min": 0,
                "accuracy": "no_data",
                "root_cause": "航班数据未找到或API未返回结果",
                "issue_number": None,
                "issue_url": None,
            }
            print(f"    数据缺失，无法验证", file=sys.stderr)

        entry["validated"] = True
        entry["validation"] = validation
        results.append({
            "entry": entry,
            "validation": validation,
            "accuracy": validation["accuracy"],
        })

        acc = validation["accuracy"]
        if acc in ("good", "fair"):
            print(f"    准确 — 实际{validation['actual_delay_min']}分",
                  file=sys.stderr)
        elif acc == "false_positive":
            print(f"    误报 — 实际{validation['actual_delay_min']}分",
                  file=sys.stderr)
        elif acc == "no_data":
            pass  # already printed
        else:
            print(f"    {acc} — 实际{validation['actual_delay_min']}分",
                  file=sys.stderr)

        # 对误报/偏高的创建 Issue
        if (create_issues
                and acc in ISSUE_ACCURACY_THRESHOLDS):
            issue_num, issue_url = create_github_issue(
                entry, validation, repo)
            if issue_url:
                validation["issue_number"] = issue_num
                validation["issue_url"] = issue_url
                issues_created += 1

    # 保存更新后的日志
    save_detection_log(log_file, log)

    # 统计
    total = len(results)
    valid_results = [r for r in results if r["accuracy"] != "no_data"]
    good = sum(1 for r in valid_results
               if r["accuracy"] in ("good", "fair"))
    bad = sum(1 for r in valid_results
              if r["accuracy"] == "false_positive")
    no_data = sum(1 for r in results if r["accuracy"] == "no_data")
    rate = round(good / len(valid_results) * 100) if valid_results else 0

    print(f"\n  [日报汇总] {yesterday}",
          file=sys.stderr)
    print(f"  总检测 {total} | 有数据 {len(valid_results)} "
          f"| 数据缺失 {no_data}",
          file=sys.stderr)
    print(f"  准确 {good} | 误报 {bad} | 准确率 {rate}% "
          f"| Issue {issues_created}",
          file=sys.stderr)

    # 发送日报邮件
    if email and resend_key:
        _send_daily_report_email(
            email, resend_key, yesterday, results, yesterday_entries)


def _build_daily_report_html(review_date, results: list,
                              entries: list) -> str:
    """构建每日回测报告邮件 HTML"""
    now_str = beijing_now().strftime("%Y-%m-%d %H:%M")
    date_str = str(review_date)
    total = len(results)

    valid_results = [r for r in results if r["accuracy"] != "no_data"]
    good = sum(1 for r in valid_results
               if r["accuracy"] in ("good", "fair"))
    bad = sum(1 for r in valid_results
              if r["accuracy"] == "false_positive")
    over = sum(1 for r in valid_results
               if r["accuracy"] == "overpredicted")
    under = sum(1 for r in valid_results
                if r["accuracy"] == "underpredicted")
    cancelled = sum(1 for r in valid_results
                    if r["accuracy"] == "cancelled")
    no_data = sum(1 for r in results if r["accuracy"] == "no_data")
    swapped = sum(1 for r in valid_results
                  if r.get("validation", {}).get("aircraft_changed"))
    rate = round(good / len(valid_results) * 100) if valid_results else 0

    # 真正出现航变的（延误>15分钟 或 取消）
    real_change = sum(1 for r in valid_results
                      if r["accuracy"] in ("good", "fair",
                                            "underpredicted"))

    banner_bg = "#27ae60" if rate >= 70 else (
        "#e67e22" if rate >= 40 else "#c0392b")

    if total == 0:
        return f"""
        <div style="font-family:Arial,sans-serif; max-width:700px; margin:auto;">
          <div style="background:#34495e; color:white; padding:16px 20px;
                  border-radius:8px 8px 0 0;">
            <h2 style="margin:0;">每日回测报告</h2>
            <p style="margin:6px 0 0; opacity:0.9;">{date_str} | 生成于 {now_str}</p>
          </div>
          <div style="padding:30px 20px; text-align:center; color:#666;
                  background:#f8f9fa; border-radius:0 0 8px 8px;">
            <p style="font-size:16px;">昨天没有检测到任何航变机会</p>
            <p style="font-size:13px;">系统运行正常，持续监控中</p>
          </div>
        </div>"""

    # 按结果分组：先显示真航变，再误报，最后数据缺失
    sort_order = {"good": 0, "fair": 1, "underpredicted": 2,
                  "overpredicted": 3, "cancelled": 4,
                  "false_positive": 5, "no_data": 6}
    sorted_results = sorted(results,
                            key=lambda r: sort_order.get(r["accuracy"], 9))

    rows_html = ""
    for r in sorted_results:
        entry = r["entry"]
        val = r.get("validation", {})
        acc = r["accuracy"]

        acc_map = {
            "good": ("#27ae60", "预测准确"),
            "fair": ("#2ecc71", "基本准确"),
            "false_positive": ("#c0392b", "误报-未航变"),
            "overpredicted": ("#e67e22", "预测偏高"),
            "underpredicted": ("#3498db", "预测偏低"),
            "cancelled": ("#95a5a6", "航班取消"),
            "no_data": ("#bdc3c7", "数据缺失"),
        }
        acc_color, acc_label = acc_map.get(acc, ("#95a5a6", acc))

        # 实际结果描述
        actual_delay = val.get("actual_delay_min", 0)
        actual_state = val.get("actual_state", "")
        if acc == "no_data":
            actual_desc = "无数据"
        elif actual_state in ("取消", "提前取消"):
            actual_desc = "已取消"
        elif actual_delay > OFFICIAL_DELAY_THRESHOLD:
            actual_desc = f"延误{actual_delay}分钟"
        elif actual_delay > 0:
            actual_desc = f"轻微延误{actual_delay}分"
        else:
            actual_desc = "正常/准点"

        # 飞机调换标记
        swap_html = ""
        if val.get("aircraft_changed"):
            swap_type = "机型更换" if val.get("type_changed") else "同型调换"
            swap_html = (f'<br/><span style="background:#9b59b6; '
                         f'color:white; padding:1px 5px; '
                         f'border-radius:3px; font-size:10px;">'
                         f'{swap_type}</span>')

        prob = entry.get("probability", "")
        prob_str = f"{prob}%" if prob else "-"
        pred_delay = entry.get("predicted_delay_min", 0)
        error = val.get("prediction_error_min", 0)

        dep_time = entry.get("plan_departure", "")
        if dep_time and len(dep_time) >= 16:
            dep_time = dep_time[11:16]  # HH:MM

        rows_html += f"""
        <tr style="border-bottom:1px solid #eee;">
          <td style="padding:8px;">
            <b>{entry['flight_no']}</b><br/>
            <span style="color:#666; font-size:12px;">
              {entry.get('dep_city', entry['route'])}</span><br/>
            <span style="color:#999; font-size:11px;">
              计划 {dep_time}</span>
          </td>
          <td style="padding:8px; text-align:center;">
            {prob_str}
          </td>
          <td style="padding:8px; text-align:center;">
            预测{pred_delay}分
          </td>
          <td style="padding:8px; text-align:center;">
            <b>{actual_desc}</b>{swap_html}
          </td>
          <td style="padding:8px; text-align:center;">
            <span style="background:{acc_color}; color:white;
              padding:3px 8px; border-radius:10px; font-size:12px;">
              {acc_label}</span>
          </td>
          <td style="padding:8px; font-size:12px; color:#666;">
            {val.get('root_cause', '') if acc != 'no_data' else '-'}
          </td>
        </tr>"""

    return f"""
    <div style="font-family:Arial,sans-serif; max-width:900px; margin:auto;">
      <div style="background:#2c3e50; color:white; padding:16px 20px;
              border-radius:8px 8px 0 0;">
        <h2 style="margin:0;">每日回测报告</h2>
        <p style="margin:6px 0 0; opacity:0.9;">
          {date_str} | 生成于 {now_str}</p>
      </div>

      <div style="background:#f8f9fa; padding:16px 20px; display:flex;
              gap:16px; flex-wrap:wrap;">
        <div style="text-align:center; flex:1; min-width:70px;">
          <div style="font-size:28px; font-weight:bold;">{total}</div>
          <div style="color:#666; font-size:12px;">检测总数</div>
        </div>
        <div style="text-align:center; flex:1; min-width:70px;">
          <div style="font-size:28px; font-weight:bold; color:#e74c3c;">
            {real_change}</div>
          <div style="color:#666; font-size:12px;">真实航变</div>
        </div>
        <div style="text-align:center; flex:1; min-width:70px;">
          <div style="font-size:28px; font-weight:bold; color:#c0392b;">
            {bad}</div>
          <div style="color:#666; font-size:12px;">误报</div>
        </div>
        <div style="text-align:center; flex:1; min-width:70px;">
          <div style="font-size:28px; font-weight:bold; color:{banner_bg};">
            {rate}%</div>
          <div style="color:#666; font-size:12px;">准确率</div>
        </div>
        <div style="text-align:center; flex:1; min-width:70px;">
          <div style="font-size:28px; font-weight:bold; color:#9b59b6;">
            {swapped}</div>
          <div style="color:#666; font-size:12px;">飞机调换</div>
        </div>
      </div>

      <table style="width:100%; border-collapse:collapse; margin-top:4px;">
        <thead>
          <tr style="background:#34495e; color:white;">
            <th style="padding:10px; text-align:left;">航班</th>
            <th style="padding:10px; text-align:center;">概率</th>
            <th style="padding:10px; text-align:center;">预测</th>
            <th style="padding:10px; text-align:center;">实际</th>
            <th style="padding:10px; text-align:center;">结果</th>
            <th style="padding:10px; text-align:left;">分析</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>

      <div style="padding:12px 20px; background:#ecf0f1; color:#555;
              font-size:12px; border-radius:0 0 8px 8px; margin-top:4px;">
        <b>统计:</b>
        准确 {good} | 误报 {bad} | 偏高 {over} | 偏低 {under}
        | 取消 {cancelled} | 数据缺失 {no_data} | 飞机调换 {swapped}
      </div>
    </div>"""


def _send_daily_report_email(to_addr: str, resend_key: str,
                              review_date, results: list,
                              entries: list) -> bool:
    """发送每日回测报告邮件"""
    date_str = str(review_date)
    total = len(results)
    valid_results = [r for r in results if r.get("accuracy") != "no_data"]
    good = sum(1 for r in valid_results
               if r.get("accuracy") in ("good", "fair"))
    bad = sum(1 for r in valid_results
              if r.get("accuracy") == "false_positive")
    rate = round(good / len(valid_results) * 100) if valid_results else 0

    if total == 0:
        subject = f"[日报] {date_str} 无检测记录"
    else:
        subject = (f"[日报] {date_str} "
                   f"检测{total}条 准确率{rate}% 误报{bad}条")

    html = _build_daily_report_html(review_date, results, entries)

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": "Flight Monitor <onboarding@resend.dev>",
                "to": [to_addr],
                "subject": subject,
                "html": html,
            },
            timeout=30,
        )
        if resp.status_code in (200, 201):
            print(f"  [日报邮件] 已发送到 {to_addr}", file=sys.stderr)
            return True
        else:
            print(f"  [日报邮件失败] Resend {resp.status_code}: {resp.text}",
                  file=sys.stderr)
            return False
    except Exception as e:
        print(f"  [日报邮件失败] {e}", file=sys.stderr)
        return False


# ============================================================
# CLI 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="航变检测结果验证器 — 验证预测准确性并自动提 Issue",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
工作原理:
  主检测器每次运行后，将检测到的航变机会追加到检测日志。
  本验证器独立运行，等航班实际起飞后重新查询，对比预测与实际。
  如果预测不准（误报、偏差过大），自动创建 GitHub Issue 记录问题。

示例:
  %(prog)s --auto-renew                        # 单次验证
  %(prog)s --auto-renew --no-issues             # 只验证不创建 Issue
  %(prog)s --auto-renew --monitor --cycle 60    # 持续验证（每60分钟）
  %(prog)s --log .detection_log.json -v         # 指定日志 + 详细模式
        """,
    )
    parser.add_argument(
        "--key", "-k",
        default="sk-5BvX04jqSMsy42k4OJiekvRjNGxxBulxSf5vQbyCZIw",
        help="飞常准 API Key",
    )
    parser.add_argument(
        "--log",
        default=DETECTION_LOG_FILE,
        help=f"检测日志文件 (默认: {DETECTION_LOG_FILE})",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="GitHub 仓库 (owner/repo)，留空自动检测",
    )
    parser.add_argument(
        "--auto-renew",
        action="store_true",
        help="API 余额不足时自动获取新 Key",
    )
    parser.add_argument(
        "--no-issues",
        action="store_true",
        help="只验证不创建 GitHub Issue",
    )
    parser.add_argument(
        "--daily-review",
        action="store_true",
        help="每日回测模式: 回顾前一天所有检测结果并发送日报",
    )
    parser.add_argument(
        "--monitor",
        action="store_true",
        help="持续运行模式，定期验证",
    )
    parser.add_argument(
        "--cycle",
        type=int,
        default=60,
        help="持续模式验证间隔(分钟)，默认 60",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="详细输出",
    )
    parser.add_argument(
        "--email",
        default=None,
        help="验证结果通知邮箱",
    )
    parser.add_argument(
        "--resend-key",
        default=None,
        help="Resend API Key (也可通过环境变量 RESEND_API_KEY 设置)",
    )

    args = parser.parse_args()

    # Resend Key: CLI > 环境变量
    if not args.resend_key:
        args.resend_key = os.environ.get("RESEND_API_KEY")

    if args.daily_review:
        run_daily_review(
            args.key, args.log,
            auto_renew=args.auto_renew,
            verbose=args.verbose,
            email=args.email,
            resend_key=args.resend_key,
            repo=args.repo,
            create_issues=not args.no_issues,
        )
    elif args.monitor:
        print(f"\n  [验证器] 持续验证模式 — "
              f"每 {args.cycle} 分钟检查一次",
              file=sys.stderr)
        while True:
            try:
                run_validation(
                    args.key, args.log, repo=args.repo,
                    auto_renew=args.auto_renew,
                    verbose=args.verbose,
                    create_issues=not args.no_issues,
                    email=args.email,
                    resend_key=args.resend_key,
                )
            except Exception as e:
                print(f"  [验证器异常] {e}", file=sys.stderr)
            print(f"\n  [验证器] 休眠 {args.cycle} 分钟...\n",
                  file=sys.stderr)
            time.sleep(args.cycle * 60)
    else:
        run_validation(
            args.key, args.log, repo=args.repo,
            auto_renew=args.auto_renew,
            verbose=args.verbose,
            create_issues=not args.no_issues,
            email=args.email,
            resend_key=args.resend_key,
        )


if __name__ == "__main__":
    main()
