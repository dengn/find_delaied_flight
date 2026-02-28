# 南航航变机会检测器 — 完整实现文档

## 项目概述

这是一个**南航航班延误机会检测系统**，核心思路极其精巧：

> 当一架飞机的前序航班严重延误时，它的后续航班**数学上铁定来不及**起飞——但航司往往还没发布航变通知。这个时间窗口里，你可以用里程票出票，等航变通知一出，免费改签或全额退票。

整个系统从 API 发现、逆向注册、自动续杯、延误链分析、天气智能扫描到 CI 自动化监控，形成了一个完整的闭环。

---

## 目录

1. [最精彩的部分：API 发现与自动续杯](#1-最精彩的部分api-发现与自动续杯)
2. [核心检测算法：前序延误链分析](#2-核心检测算法前序延误链分析)
3. [天气智能扫描系统](#3-天气智能扫描系统)
4. [南航里程票搜索器（BA.com 抓包）](#4-南航里程票搜索器bacom-抓包)
5. [邮件通知系统](#5-邮件通知系统)
6. [CI/CD 自动化监控](#6-cicd-自动化监控)
7. [项目演进历程](#7-项目演进历程)
8. [架构总览](#8-架构总览)

---

## 1. 最精彩的部分：API 发现与自动续杯

### 1.1 API 是怎么找到的

国内航班实时数据最权威的来源是**飞常准 (VariFlight)**。但飞常准并没有公开宣传其 MCP API。

**发现过程：**

1. **搜索切入点**：飞常准近年推出了面向 AI 应用的 MCP (Model Context Protocol) 数据接口，通过搜索 "飞常准 API"、"VariFlight MCP" 等关键词，发现了其开放平台的存在。

2. **抓包验证**：通过网络抓包分析飞常准相关请求，确认了 API 的真实端点：
   ```
   https://mcp.variflight.com/api/v1/mcp/data
   ```

3. **接口逆向**：分析请求格式，发现它是一个统一入口设计——所有查询通过 POST 请求发送，`endpoint` 字段区分功能：
   ```json
   {
     "endpoint": "flights",
     "params": {"dep": "CAN", "arr": "PKX", "date": "2026-02-27"}
   }
   ```
   认证方式为 Header 中的 `X-VARIFLIGHT-KEY`。

4. **关键接口梳理**：最终确认两个核心可用接口：
   - `flights` — 查询两个机场间的航班列表（含实时状态、机号、预计时间等）
   - `futureAirportWeather` — 查询机场天气（含天气类型、能见度、风力等）

### 1.2 自动续杯——这才是真正的神来之笔

飞常准的免费 API Key 有调用额度限制。一次全量扫描 4 个枢纽 × 数十个目的地，API 消耗量巨大。额度用完怎么办？

**答案：全自动注册新账号、获取新 Key，无限续杯。**

实现在 `auto_renew_key.py` 中，完整流程如下：

```
┌──────────────────────────────────────────────────────┐
│                  自动续杯流程                          │
├──────────────────────────────────────────────────────┤
│                                                      │
│  [1/6] 获取临时邮箱域名                                │
│        ↓  调用 mail.tm API 获取可用域名                │
│                                                      │
│  [2/6] 创建临时邮箱                                    │
│        ↓  vfbot_随机字符@域名 + 密码                   │
│        ↓  获取 mail.tm token（用于收信）                │
│                                                      │
│  [3/6] 注册飞常准账号                                  │
│        ↓  POST /api/v1/platform/auth/register         │
│        ↓  用临时邮箱注册，用户名 bot_随机字符            │
│                                                      │
│  [4/6] 等待并提取激活码                                 │
│        ↓  轮询 mail.tm 收件箱（最多等 60 秒）           │
│        ↓  从激活链接中解析 email + code 参数            │
│        ↓  GET /api/v1/platform/auth/activate          │
│                                                      │
│  [5/6] 登录并创建 API Key                              │
│        ↓  POST /api/v1/platform/auth/login            │
│        ↓  POST /api/v1/platform/api-keys/             │
│                                                      │
│  [6/6] 等待额度到账                                    │
│        ↓  递增等待：5s, 10s, 15s, 20s, 25s, 30s...    │
│        ↓  GET /api/v1/platform/auth/me 检查余额        │
│        ↓  最长等待约 4 分钟                             │
│                                                      │
│  ✅ 返回新的 API Key                                   │
└──────────────────────────────────────────────────────┘
```

**关键实现细节：**

- **临时邮箱服务**：使用 [mail.tm](https://mail.tm) 的公开 API，完全免费、无需注册
- **激活码提取**：从邮件正文中搜索包含 `activate?` 的行，用 `urllib.parse` 解析 URL 参数
- **额度到账延迟处理**：新账号额度不是即时到账的。使用递增等待策略（5→10→15→20→25→30s），总等待最长约 4 分钟
- **与主程序无缝集成**：当检测运行中遇到 `403 Insufficient balance` 错误时，自动触发续杯，最多续 3 次

```python
# find_delayed_flights.py 中的自动续杯触发
if resp.status_code == 403:
    err_data = resp.json()
    if err_data.get("message") == "Insufficient balance":
        if self._auto_renew and self._renew_count < self._max_renew:
            new_key = obtain_new_key(verbose=True)
            self.api_key = new_key
            self.session.headers["X-VARIFLIGHT-KEY"] = new_key
            time.sleep(10)  # 等待额度生效
            continue  # 用新 key 重试请求
```

这样的设计意味着：**程序永远不会因为 API 额度耗尽而停止**。

### 1.3 API 调用的工程细节

除了自动续杯，API 调用层面还有大量工程打磨：

```python
class VariFlightAPI:
    # 限速：每次请求间隔 0.6 秒（避免触发风控）
    REQUEST_INTERVAL = 0.6

    # 重试：指数退避（3s, 6s, 12s, 24s）
    # 最多重试 4 次

    # 连接异常：自动重试 3 次，间隔 1s, 2s, 4s

    # 错误统计：记录 call_count 和 error_count，写入汇总报告
```

---

## 2. 核心检测算法：前序延误链分析

### 2.1 三步匹配法

整个检测的核心逻辑分三步：

```
Step 1: 扫描进港航班 → 找到哪些飞机严重延误
        ↓
        建立 {机号 → 最晚进港航班} 映射
        标记延误 ≥30 分钟 或 超时未起飞的飞机

Step 2: 扫描出港CZ航班 → 找到使用同一架飞机的后续航班
        ↓
        扫描今天 + 明天（处理跨天航班）
        只保留南航 (CZ 开头) 航班

Step 3: 数学链分析 → 判定铁定延误 + 航司未通知
        ↓
        analyze_inbound_chain() 函数
```

### 2.2 核心判定逻辑 `analyze_inbound_chain()`

这是整个系统最核心的函数，必须**同时满足四个条件**才会产出结果：

```
条件 1: 前序航班明显延误
        预计到达 - 计划到达 ≥ 30 分钟
        或：已过计划起飞时间但尚未实际起飞（overdue）

条件 2: 数学上赶不上
        前序预计到达 + 最小过站时间 > 后续计划出发
        ┌─────────────────────────────────────────┐
        │ 窄体机 (A320/B737等): 过站 45 分钟        │
        │ 宽体机 (A330/A350/B787等): 过站 70 分钟   │
        └─────────────────────────────────────────┘

条件 3: 航司尚未通知航变
        后续航班状态 = "计划"（不是 "延误"/"取消" 等）
        且预计出发时间未被大幅调整（调整 < 30 分钟）

条件 4: 有足够买票时间
        距后续航班计划出发 ≥ 120 分钟（2 小时）
```

**为什么要 4 个条件同时满足？**

- 条件 1 确保延误是真实的
- 条件 2 确保延误会传导（不是过站时间够用的情况）
- 条件 3 确保信息差还在（航司通知后就没有套利空间了）
- 条件 4 确保可操作（太近了来不及出票）

### 2.3 到达时间多源融合

飞常准的数据有多个时间字段，系统按优先级选取最佳到达时间：

```python
def get_best_arrival_time(flight):
    # 优先级从高到低:
    # 1. FlightArrtimeDate      — 实际到达时间（最准确）
    # 2. FlightArrtimeReadyDate — 预计到达时间（空管数据）
    # 3. VeryZhunReadyArrtimeDate — 飞常准 AI 预测
    # 4. FlightArrtimePlanDate  — 计划到达时间（最不准确）
```

### 2.4 前序超时未起飞检测（高优先级）

这是后来新增的一个重要检测逻辑。当前序航班已经超过计划起飞时间但仍未实际起飞时，情况比普通延误更严重：

```python
def is_overdue_not_departed(flight, now):
    """
    计划起飞时间已过 + 没有实际起飞记录 + 状态不是"起飞"/"到达"
    → 这架飞机还在地上！
    """
```

此时系统会重新估算最乐观到达时间 = 当前时刻 + 原定航程时长（假设立刻起飞），并标记为**最高优先级**。

### 2.5 结果排序与确定性等级

```
优先级 1: 前序超时未起飞 → "极高确定性 — 前序已超计划起飞时间 X 分钟仍未起飞!"
优先级 2: 前序尚未起飞   → "极高确定性（前序尚未起飞）"
优先级 3: 前序在飞       → "高确定性（前序在飞，预计到达已确定）"
优先级 4: 前序已到达     → "高确定性（前序已到达，过站时间不足）"

排序: is_priority(超时未起飞) 优先 → 然后按 estimated_delay_min 降序
```

---

## 3. 天气智能扫描系统

### 3.1 问题背景

南航有 4 个主枢纽（CAN/PKX/URC/SZX），但还有 20+ 个二线基地。全部扫描 API 消耗太大，不扫描又会漏掉机会。

**解决方案：先看天气，天气差的机场才值得扫描。**

### 3.2 天气风险评分模型

```python
# 天气类型评分
高风险 (60分): 雷暴、暴雨、暴雪、冻雨、沙尘暴、大风
中风险 (40分): 大雨、大雪、中雪、雾、浓雾、中雨、扬沙、霾
低风险 (15分): 小雨、小雪、阵雨、雨夹雪

# 能见度评分
< 500m:  +40 分
< 1000m: +30 分
< 3000m: +20 分
< 5000m: +10 分

# 风力评分
≥ 10 级: +20 分
≥ 7 级:  +10 分

# 总分上限 100 分
```

### 3.3 动态扩展扫描范围

```
1. 预扫描所有相关机场天气（50+ 个机场）
2. 为每个二线枢纽计算综合评分：
   综合分 = 自身天气风险分 + 目的地中天气差的数量 × 15
3. 综合分 ≥ 30 的二线枢纽加入扫描（最多额外增加 6 个）
4. 所有枢纽的目的地按天气风险降序排列（坏天气优先扫描）
```

### 3.4 监控的特殊机场

系统还维护了一个**天气敏感机场**列表，包括：
- 西南山区机场（丽江、版纳、宜昌、恩施等）
- 新疆沙漠机场（库尔勒、阿克苏、哈密等）
- 高原机场（九寨沟、大理、邦达、拉萨等）
- 沿海/东北易受天气影响机场

这些机场即使不是南航枢纽，也纳入天气预扫描范围。

---

## 4. 南航里程票搜索器（BA.com 抓包）

### 4.1 背景

2025 年 7 月，英国航空 (BA) 与南航开通了 Avios 里程互兑。BA 官网可以搜索南航的里程票。

### 4.2 实现方式

使用 **Playwright** 浏览器自动化，而非简单的 HTTP 请求，因为 BA 网站有复杂的 JS 渲染和反爬保护。

```
┌─────────────────────────────────────────────────┐
│  CZ Award Searcher 工作流程                       │
├─────────────────────────────────────────────────┤
│                                                 │
│  1. 启动 Chromium (Playwright)                   │
│     - 伪装 User-Agent                            │
│     - 禁用 AutomationControlled 特征              │
│                                                 │
│  2. 注册 Network Interceptor                     │
│     - 监听所有包含 avios/reward/award 的响应        │
│     - 捕获 JSON 格式的 API 响应                    │
│                                                 │
│  3. 构造 BA 奖励搜索 URL                          │
│     - 拼接出发地、目的地、日期、舱位等参数            │
│     - 导航到搜索页面                               │
│                                                 │
│  4. 处理页面交互                                   │
│     - 自动接受 Cookie 弹窗                         │
│     - 检测是否需要登录（支持自动登录）                │
│     - 等待搜索结果加载                              │
│                                                 │
│  5. 双重数据提取                                   │
│     ├─ 方式A: DOM 解析（多种选择器尝试）             │
│     │  提取航班号、Avios 价格、时间、经停等            │
│     └─ 方式B: 网络拦截数据（从 API 响应中解析）       │
│                                                 │
│  6. 过滤 & 去重 & 输出                             │
│     - 只保留 CZ 执飞的航班                          │
│     - 按航班号+时间去重                              │
│     - 输出 JSON + 格式化表格                        │
└─────────────────────────────────────────────────┘
```

### 4.3 关键的抓包拦截

```python
async def _intercept_response(self, response):
    """拦截网络响应，捕获 API 返回的里程票数据"""
    url = response.url
    keywords = ["avios", "reward", "redeem", "award",
                "flightsearch", "flight-search", "offer", "availability"]
    if any(kw in url.lower() for kw in keywords):
        if "application/json" in response.headers.get("content-type", ""):
            body = await response.json()
            self.intercepted_api_calls.append({...})
```

这段代码的精妙之处在于：不需要知道 BA 的具体 API 地址——只要响应 URL 包含里程票相关关键词且是 JSON 格式，就全部捕获，然后再从中提取有用信息。

### 4.4 Chromium 自动检测

考虑到不同环境的兼容性，系统实现了多路径 Chromium 检测：

```python
# 1. Playwright 缓存目录 (~/.cache/ms-playwright)
#    → chromium-*/chrome-linux/chrome
#    → chromium-*/chrome-linux64/chrome

# 2. 系统路径
#    → chromium-browser, chromium, google-chrome, google-chrome-stable
```

---

## 5. 邮件通知系统

### 5.1 为什么选 Resend

传统 SMTP 需要邮箱授权码，配置复杂。[Resend](https://resend.com) 提供免费的 API 邮件服务（100 封/天），只需一个 API Key：

```python
requests.post("https://api.resend.com/emails", json={
    "from": "Flight Alert <onboarding@resend.dev>",
    "to": [to_addr],
    "subject": subject,
    "html": html,
    "text": plaintext,  # 纯文本备用
})
```

### 5.2 双邮件策略

每次运行发送两种邮件：

1. **汇总报告**（每次都发）
   - 各枢纽天气概况
   - 进港/出港航班数量
   - 机场延误态势（延误率、取消数、平均延误）
   - 南航集团严重延误进港航班列表（彩色标签）
   - 天气预扫描恶劣天气机场

2. **航变机会提醒**（仅发现新机会时发）
   - 详细的延误分析
   - 原定 vs 预估时刻对比
   - 前序航班信息和延误原因
   - "查询里程票" 直达链接

### 5.3 去重机制

避免同一个航班在多轮扫描中重复通知：

```python
# 唯一标识: 航班号|计划出发时间|机号
key = f"{hit['flight']}|{hit['plan_departure']}|{hit['aircraft']}"

# 6 小时内不重复通知
DEDUP_HOURS = 6

# 持久化到 JSON 文件（CI 模式下跨运行保留）
```

---

## 6. CI/CD 自动化监控

### 6.1 GitHub Actions 配置

```yaml
schedule:
  # 北京时间 07:00-22:30，每 30 分钟运行
  - cron: '0,30 23,0-14 * * *'  # 转换为 UTC
```

完整流程：
```
Checkout → Setup Python 3.11 → pip install
  → 恢复去重缓存 (actions/cache)
  → python find_delayed_flights.py \
      --auto-renew \        # API 额度不足自动续杯
      --smart-scan \        # 天气智能扫描
      --email xxx \         # 邮件通知
      --dedup-file xxx \    # 去重缓存持久化
      --json                # JSON 输出
```

### 6.2 关键设计

- **25 分钟超时**：防止某次运行卡死
- **|| true**：无论检测结果如何（exit 0 无机会，exit 1 有机会），都继续执行后续步骤保存缓存
- **去重缓存跨运行持久化**：通过 `actions/cache` 实现
- **支持手动触发**：`workflow_dispatch` 可指定特定枢纽测试

---

## 7. 项目演进历程

从 git 历史可以看出项目的演进脉络，每一步都在解决实际遇到的问题：

```
v1  基础版本
    d9ccfde Add CZ flight delay detector using VariFlight API
    → 最初版本，基本的 API 调用 + 延误检测

v2  自动续杯
    2c17a0f Add auto API key renewal via temp email registration
    → 解决 API 额度限制问题！临时邮箱 + 自动注册，无限续杯

v3  多信号评估
    1364454 Enhance detector with multi-signal delay risk assessment
    → 引入多种延误信号源

v4  核心重写
    22d6046 Rewrite core logic: focus on ironclad inbound delay evidence
    → 重写为"铁证"逻辑：只看前序飞机来不来得及，不做概率猜测

v5  持续监控
    26aa142 Add continuous monitoring mode with email notification
    → 本地持续运行模式

v6  CI 自动化
    54d37e9 Add GitHub Actions CI monitoring and switch to Resend API
    → 上云！GitHub Actions 每 30 分钟自动扫描

v7  时区修复
    1e3eb1f Fix timezone: use Beijing time (UTC+8) throughout
    → 服务器是 UTC，航班是北京时间，必须统一

v8  汇总邮件
    ea0a2eb Add summary email for every run
    → 每次都发汇总报告，不只是有机会时才通知

v9  天气智能扫描
    6bfad29 feat: add weather-driven smart scan
    → 先看天气再决定扫描范围，API 消耗减半但覆盖不减

v10 邮件优化
    8f262d6 feat: improve email report
    → 南航集团过滤、航班详情、里程票直达链接

v11 里程票搜索
    4691695 feat: add CZ award ticket searcher via ba.com Playwright scraper
    → BA.com 抓包搜索南航里程票可用性

v12 超时检测
    e03b817 feat: add inbound overdue detection
    → 前序超时未起飞检测，最高优先级标记
```

---

## 8. 架构总览

### 8.1 文件结构

```
find_delaied_flight/
├── find_delayed_flights.py    # 主程序（~2000 行）
│   ├── 配置常量 & 枢纽定义
│   ├── 天气评分模型
│   ├── 智能扫描构建器
│   ├── VariFlightAPI 类（含自动续杯）
│   ├── 时间解析工具函数
│   ├── 核心: analyze_inbound_chain()
│   ├── run_detection() 主流程
│   ├── 邮件构建 & 发送（Resend API）
│   ├── 去重缓存系统
│   ├── 持续监控循环
│   └── CLI 入口 & 参数解析
│
├── auto_renew_key.py          # API Key 自动续杯（~220 行）
│   ├── mail.tm 临时邮箱操作
│   ├── 飞常准注册/激活/登录
│   ├── API Key 创建 & 余额检查
│   └── obtain_new_key() 完整流程
│
├── search_cz_awards.py        # 里程票搜索器（~900 行）
│   ├── Playwright 浏览器自动化
│   ├── 网络拦截器
│   ├── BA.com 页面解析
│   └── 结果输出
│
├── requirements.txt           # 依赖: requests, playwright
├── .github/workflows/
│   └── monitor.yml            # GitHub Actions CI 配置
└── .gitignore
```

### 8.2 数据流

```
                    ┌─────────────────┐
                    │   天气预扫描      │
                    │ (50+ 个机场)     │
                    └────────┬────────┘
                             │ 动态决定扫描范围
                             ▼
┌──────────┐    ┌──────────────────────────┐    ┌──────────────┐
│ VariFlight│◄──│     主扫描流程              │──►│  结果排序      │
│   API     │    │ Step1: 进港航班扫描        │    │ & 优先级标记   │
│           │    │ Step2: 出港CZ航班扫描      │    └──────┬───────┘
│ (自动续杯) │    │ Step3: 延误链匹配分析      │           │
└──────────┘    └──────────────────────────┘           │
                                                       ▼
                                              ┌─────────────────┐
                    ┌────────────────┐        │    去重过滤       │
                    │  Resend API    │◄───────│ (6小时窗口)       │
                    │  邮件发送       │        └─────────────────┘
                    └────────────────┘
                      │           │
                      ▼           ▼
               汇总报告邮件   航变机会提醒邮件
```

### 8.3 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 数据源 | 飞常准 MCP API | 国内最全的实时航班数据 |
| 额度方案 | 自动续杯 | 免费无限使用 |
| 检测逻辑 | 纯数学推导 | 不做概率猜测，只输出"铁定延误"的结果 |
| 天气扫描 | 动态评分扩展 | 平衡 API 消耗和覆盖范围 |
| 邮件服务 | Resend API | 免费100封/天，无需 SMTP 配置 |
| CI 平台 | GitHub Actions | 免费、稳定、支持定时任务 |
| 里程票搜索 | Playwright 抓包 | BA 有 JS 渲染和反爬，必须用真实浏览器 |
| 去重策略 | 文件缓存 + 6 小时 TTL | 跨 CI 运行持久化 |

---

## 总结

这个项目最精彩的地方在于两个核心创新：

1. **API 发现与无限续杯**：通过搜索和抓包找到了飞常准的 MCP API，然后利用临时邮箱服务实现了全自动注册→激活→获取 Key 的流程，彻底解决了 API 额度限制。这不是简单的"薅羊毛"，而是一个完整的自动化工程——包括邮件轮询、激活码解析、额度到账等待等细节处理。

2. **前序延误链的数学推导**：不依赖任何"预测"或"概率"，纯粹基于物理约束（飞机在 A 地，要飞到 B 地，还需要过站时间）做出确定性判断。当所有条件同时满足时，延误是**必然发生**的——这个信息差就是套利空间。

两者结合，再加上天气智能扫描、CI 自动化、邮件通知等工程化包装，形成了一个真正可以 7×16 小时无人值守运行的航变机会检测系统。
