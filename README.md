# 中国标准爬虫工具集

国家标准（GB）与行业标准（HB）全量数据爬取及 PDF 下载工具。支持断点续爬、验证码自动识别、多标准类型分类采集，数据存储于 SQLite 数据库。

---

## 项目结构

```
standards-crawler/
├── pyproject.toml          # 项目配置与依赖声明（uv/pip 兼容）
├── uv.lock                 # uv 锁定文件（精确版本锁定）
├── .python-version         # Python 版本声明
├── .gitignore              # Git 忽略规则
├── run_crawler.py          # 统一入口脚本
├── scripts/
│   ├── gb_crawler.py       # 国家标准爬虫
│   └── hb_crawler.py       # 行业标准爬虫
├── standards.db            # SQLite 数据库（运行后自动生成）
├── download/
│   ├── gb_standards/       # 国家标准 PDF 存放目录
│   └── hb_standards/       # 行业标准 PDF 存放目录
├── logs/
│   ├── gb_crawler.log      # 国家标准爬虫日志
│   └── hb_crawler.log      # 行业标准爬虫日志
└── README.md
```

---

## 功能特性

### 国家标准爬虫（gb_crawler）

- **数据来源**：[国家标准全文公开系统](https://openstd.samr.gov.cn)
- **三种标准类型**：
  - 强制性国家标准（p1=1）
  - 推荐性国家标准（p1=2）
  - 指导性技术文件（p1=3）
- **列表爬取**：解析服务端渲染的 HTML 分页页面，提取标准号、标准名称、状态、发布日期、实施日期等字段
- **PDF 下载**：支持两种下载方式，自动判断标准可用方式
  - **直接下载**：通过 `c.gb688.cn/bzgk/gb/viewGb` 接口直接获取 PDF 文件流（标准允许下载时优先使用）
  - **预览方式下载**：通过 `c.gb688.cn/bzgk/gb/showGb` 获取预览页面结构，下载图块资源并重组为 PDF（标准仅允许预览时使用，需加 `--include-preview` 参数）
- **下载选项**：默认仅下载可直接下载的国标（`allow_download=1`），使用 `--include-preview` 可包含仅可预览的国标
- **验证码识别**：
  1. 获取验证码图片：`c.gb688.cn/bzgk/gb/gc`
  2. cv2 图像预处理：灰度化 → 二值化（阈值190）→ 反色
  3. ddddocr OCR 识别验证码文字
  4. 提交验证：`c.gb688.cn/bzgk/gb/verifyCode`
  5. 最多重试 10 次，验证通过后在同一会话中下载 PDF
- **详情页检测**：自动检测标准是否允许下载/预览，不可下载的标准不会重复尝试
- **断点续爬**：通过 `gb_crawl_progress` 表记录已爬取的页码，中断后自动跳过已完成的页面
- **下载进度跟踪**：`gb_download_progress` 表记录每个标准的下载状态和重试次数

### 行业标准爬虫（hb_crawler）

- **数据来源**：[行业标准信息服务平台](https://hbba.sacinfo.org.cn)
- **列表爬取**：调用 REST API 接口 `/stdQueryList`，返回 JSON 格式数据
- **PDF 下载流程**：
  1. 访问标准在线查看页面，检查是否公开
  2. 获取验证码图片
  3. 使用 `ddddocr` 识别验证码
  4. 提交验证码获取临时下载码
  5. 使用下载码下载 PDF 文件
- **三种下载状态**：
  - `success` — 下载成功
  - `not_public` — 标准尚未公开，页面显示"尚未公开"
  - `no_file` — 标准无 PDF 文件可供下载（如部分 SH/T 类标准重定向到首页）
- **断点续爬**：通过 `hb_crawl_progress` 表记录已爬取页码
- **下载进度跟踪**：`hb_download_progress` 表记录下载状态，已标记为 `not_public`、`no_file`、`success` 的标准不会重复下载

### 通用特性

- **断点续爬**：所有爬虫均支持中断后从上次位置继续，无需重新开始
- **优雅中断**：支持 Ctrl+C 中断，自动保存当前进度，下次运行从断点继续
- **请求延迟**：内置随机请求间隔，避免对目标服务器造成过大压力
- **重试机制**：网络请求失败时自动重试，最大重试次数可配置
- **文件命名**：PDF 文件以 `<标准号>-<标准名称>.pdf` 格式保存，特殊字符自动替换
- **日志记录**：同时输出到控制台和日志文件，便于排查问题

---

## 环境要求

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) （推荐）或 pip

---

## 安装

### 使用 uv（推荐）

```bash
# 克隆项目
cd standards-crawler

# uv 自动根据 .python-version 创建虚拟环境并安装依赖
uv sync
```

### 使用 pip

```bash
# 克隆项目
cd standards-crawler

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

# 安装依赖
pip install -e .
```

---

## 使用方法

### 统一入口（推荐）

`run_crawler.py` 提供了统一的命令行入口，支持灵活的参数组合：

```bash
# 使用 uv run（自动激活虚拟环境）
uv run python run_crawler.py stats

# ===== 国家标准 =====
uv run python run_crawler.py gb --crawl              # 爬取全部类型国家标准列表
uv run python run_crawler.py gb --crawl --type 1     # 只爬取强制性国家标准
uv run python run_crawler.py gb --crawl --type 2     # 只爬取推荐性国家标准
uv run python run_crawler.py gb --crawl --type 3     # 只爬取指导性技术文件
uv run python run_crawler.py gb --download           # 下载国家标准 PDF（默认仅可下载标准）
uv run python run_crawler.py gb --download --include-preview  # 下载国家标准 PDF（含仅可预览标准）
uv run python run_crawler.py gb --all                # 爬取+下载国家标准
uv run python run_crawler.py gb --all --include-preview     # 爬取+下载（含仅可预览标准）

# ===== 行业标准 =====
uv run python run_crawler.py hb --crawl              # 爬取行业标准列表
uv run python run_crawler.py hb --download           # 下载行业标准 PDF
uv run python run_crawler.py hb --all                # 爬取+下载行业标准

# ===== 全部标准 =====
uv run python run_crawler.py all --crawl             # 爬取所有标准列表
uv run python run_crawler.py all --download          # 下载所有标准 PDF（国标默认仅可下载）
uv run python run_crawler.py all --download --include-preview  # 下载所有标准 PDF（国标含仅可预览）
uv run python run_crawler.py all --all               # 爬取+下载所有标准

# 查看统计信息
uv run python run_crawler.py stats
```

### 单独运行爬虫

也可以直接运行各个爬虫脚本：

```bash
# 国家标准爬虫
uv run python scripts/gb_crawler.py --crawl          # 爬取列表
uv run python scripts/gb_crawler.py --download       # 下载 PDF（默认仅可下载标准）
uv run python scripts/gb_crawler.py --download --include-preview  # 下载 PDF（含仅可预览标准）
uv run python scripts/gb_crawler.py --type 2         # 只爬取推荐性
uv run python scripts/gb_crawler.py --all            # 全部操作
uv run python scripts/gb_crawler.py --stats          # 统计信息

# 行业标准爬虫
uv run python scripts/hb_crawler.py --crawl          # 爬取列表
uv run python scripts/hb_crawler.py --download       # 下载 PDF
uv run python scripts/hb_crawler.py --all            # 全部操作
uv run python scripts/hb_crawler.py --stats          # 统计信息
```

---

## 数据库结构

所有数据存储在项目根目录的 `standards.db`（SQLite）中，包含以下数据表：

### gb_standards（国家标准）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键自增 |
| std_type | TEXT | 标准类型（强制性国家标准/推荐性国家标准/指导性技术文件） |
| standard_no | TEXT | 标准号（唯一） |
| is_adopted | TEXT | 是否采标 |
| standard_name | TEXT | 标准名称 |
| status | TEXT | 标准状态 |
| publish_date | TEXT | 发布日期 |
| implement_date | TEXT | 实施日期 |
| detail_url | TEXT | 详情页 URL |
| hcno | TEXT | 标准编号哈希（用于下载） |
| allow_download | INTEGER | 是否允许直接下载（0/1） |
| allow_preview | INTEGER | 是否允许预览下载（0/1） |
| local_file | TEXT | 本地文件名 |
| created_at | TIMESTAMP | 记录创建时间 |

### hb_standards（行业标准）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键自增 |
| standard_no | TEXT | 标准号（唯一） |
| standard_name | TEXT | 标准名称 |
| industry | TEXT | 行业分类 |
| status | TEXT | 标准状态 |
| approve_date | TEXT | 批准日期 |
| implement_date | TEXT | 实施日期 |
| detail_url | TEXT | 详情页 URL |
| pk | TEXT | 主键标识（用于下载） |
| local_file | TEXT | 本地文件名 |
| charge_dept | TEXT | 主管部门 |
| revise_std_codes | TEXT | 替代标准号 |
| created_at | TIMESTAMP | 记录创建时间 |

### 进度跟踪表

- `gb_crawl_progress` — 国家标准爬取进度（按类型+页码）
- `hb_crawl_progress` — 行业标准爬取进度（按页码）
- `gb_download_progress` — 国家标准下载进度
- `hb_download_progress` — 行业标准下载进度

---

## 配置参数

可在各爬虫脚本顶部修改以下配置：

### 国家标准爬虫配置

| 参数 | 默认值 | 说明 |
|------|--------|------|
| PAGE_SIZE | 50 | 每页记录数 |
| REQUEST_DELAY | (1, 3) | 请求间隔范围（秒） |
| MAX_RETRIES | 3 | 列表请求最大重试次数 |
| CAPTCHA_MAX_RETRIES | 10 | 验证码识别最大重试次数 |
| DOWNLOAD_MAX_RETRIES | 5 | PDF 下载最大重试次数 |
| CONSECUTIVE_FAIL_LIMIT | 20 | 连续失败早停阈值 |

### 行业标准爬虫配置

| 参数 | 默认值 | 说明 |
|------|--------|------|
| PAGE_SIZE | 100 | 每页记录数 |
| REQUEST_DELAY | (0.5, 2) | 请求间隔范围（秒） |
| MAX_RETRIES | 5 | 列表请求最大重试次数 |
| CAPTCHA_MAX_RETRIES | 10 | 验证码识别最大重试次数 |
| DOWNLOAD_MAX_RETRIES | 5 | PDF 下载最大重试次数 |

---

## 已知限制

1. **国家标准 PDF 下载**：国家标准 PDF 下载服务器 `c.gb688.cn` 仅支持 HTTP 协议，可能存在网络访问限制，从境外 IP 或部分网络环境下无法直接访问。如遇此问题，建议在大陆网络环境下运行。

2. **行业标准"尚未公开"**：大量行业标准标记为"尚未公开"，无法下载其 PDF 文件。爬虫会自动检测并标记为 `not_public` 状态，不会反复重试。

3. **行业标准"无文件"**：部分行业标准（如部分 SH/T 类标准）虽然页面可访问，但实际上没有 PDF 文件可供下载，点击下载会重定向回首页。爬虫会检测此情况并标记为 `no_file` 状态。

4. **验证码识别率**：`ddddocr` 对验证码的识别率并非 100%，但通过多次重试机制（默认最多 10 次）和 cv2 图像预处理（灰度化+二值化+反色），可以覆盖绝大多数情况。

5. **国标预览方式**：部分国家标准仅允许在线预览，不允许直接下载。默认下载时仅下载可直接下载的标准；使用 `--include-preview` 参数后，爬虫会自动切换为预览方式下载仅可预览的标准，即获取预览页面结构、下载图块资源、重组页面并生成 PDF。此方式下载的 PDF 质量可能略低于直接下载的原始 PDF，且当前预览方式下载可能存在不稳定的情况。

---

## 运行示例

```bash
# 第一步：爬取国家标准列表
uv run python run_crawler.py gb --crawl

# 输出示例：
# 2025-05-15 10:00:00 [INFO] ===== 开始爬取 强制性国家标准 =====
# 2025-05-15 10:00:02 [INFO] 强制性国家标准 共 12 页
# 2025-05-15 10:00:03 [INFO] 第 1/12 页: 爬取 50 条记录
# 2025-05-15 10:00:05 [INFO] 第 2/12 页: 爬取 50 条记录
# ...

# 第二步：下载国家标准 PDF
uv run python run_crawler.py gb --download

# 输出示例：
# 2025-05-15 10:05:00 [INFO] 待下载标准: 44700 个 (仅可下载: 35000, 仅可预览: 8000, 下载+预览: 1700)
# 2025-05-15 10:05:00 [INFO] 下载模式: 仅可下载标准 (使用 --include-preview 可包含仅可预览标准)
# 2025-05-15 10:05:00 [INFO] 正在测试下载服务器 c.gb688.cn 连通性...
# 2025-05-15 10:05:01 [INFO] 下载服务器响应: status=200, content_length=12345
# 2025-05-15 10:05:01 [INFO] 下载进度: 1/44700 - GB/T 1.1 标准化工作导则
# 2025-05-15 10:05:02 [INFO] ddddocr OCR引擎初始化完成
# 2025-05-15 10:05:03 [INFO] 验证码验证成功 (attempt 1)
# 2025-05-15 10:05:05 [INFO] 下载成功(直接): GB_T 1.1-标准化工作导则.pdf

# 第三步：爬取+下载行业标准
uv run python run_crawler.py hb --all

# 第四步：查看统计
uv run python run_crawler.py stats
```

---

## 技术实现细节

### 国家标准爬虫

- 采用 `requests.Session` 维持会话，首次访问首页初始化 Cookie
- 列表页为服务端渲染 HTML，使用 `BeautifulSoup` + `lxml` 解析 `table.result_list` 表格
- 从 `onclick` 事件中提取 `hcno`（标准的唯一标识哈希值），用于拼接详情页 URL 和下载 URL
- 分页信息通过正则匹配页码元素获取
- **PDF 下载流程**：
  1. 检查详情页，判断标准是否允许下载/预览（检测 `button.xz_btn` 和 `button.ck_btn`）
  2. 获取验证码：`GET c.gb688.cn/bzgk/gb/gc?_{timestamp}`
  3. cv2 预处理 + ddddocr 识别验证码
  4. 提交验证：`POST c.gb688.cn/bzgk/gb/verifyCode`（同一会话）
  5. 直接下载：`GET c.gb688.cn/bzgk/gb/viewGb?hcno={hcno}`（优先）
  6. 预览下载：获取预览页面结构 → 下载图块 → 重组页面 → 生成 PDF（备选）
- 验证码和下载必须在同一 HTTP 会话中完成，使用独立的 `requests.Session` 维护

### 行业标准爬虫

- 列表接口为 POST 请求的 REST API，返回 JSON 格式数据，包含分页信息
- 日期字段为毫秒级时间戳，自动转换为 `YYYY-MM-DD` 格式
- PDF 下载需通过验证码验证流程：
  1. 先访问在线查看页面获取 Cookie
  2. 请求验证码图片接口
  3. `ddddocr` 识别验证码文字
  4. 提交验证码到 `/portal/validate-captcha/down` 获取临时下载码
  5. 使用下载码请求 `/portal/download/{code}` 获取 PDF
- 下载时检测重定向到首页的情况，标记为 `no_file`；检测页面含"尚未公开"文字，标记为 `not_public`

---

## 许可证

本项目仅供学习和研究使用，请遵守相关网站的使用条款和 robots.txt 规则。爬取数据时请注意控制请求频率，避免对目标服务器造成过大压力。

提示及测试： hootoovv  
设计及实现：GLM5.1 Agent @ https://chat.z.ai/ 