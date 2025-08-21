# SHranking Crawler (ARWU / BCUR, 2025)

本项目用于抓取上海软科（ShanghaiRanking）两类榜单的 2025 年度数据：
- **ARWU 世界大学学术排名**（Academic Ranking of World Universities）
- **BCUR 中国大陆大学本科生培养实力排名**

项目提供两套实现：
1. `shranking_crawler.py`：**合并版**，在一个入口内完成 ARWU 与 BCUR 的抓取、CSV 持续写入（streaming & resume）与按批入库 MySQL。
2. `arwu_crawler.py`、`bcur_crawler.py`：**任务拆分版**，分别抓取对应榜单，便于独立运行与调试。

---

## 目录结构

```
SHranking_extracted/
├── arwu_crawler.py        # ARWU 专用爬虫脚本
├── bcur_crawler.py        # BCUR 专用爬虫脚本
├── shranking_crawler.py   # 合并版爬虫脚本（推荐部署）
├── arwu_rank.csv          # 样例输出（ARWU）
├── bcur_rank.csv          # 样例输出（BCUR）
└── requirements.txt       # 依赖清单（Python 3.11+）
```

---

## 核心功能概述

- **Headless Selenium 抓取**：使用 `selenium` + `webdriver-manager` 自动管理 ChromeDriver，`page_load_strategy=eager`，默认无头运行、自动重试与指数退避。
- **健壮的页面定位**：每一列字段提供**多组 XPath 候选**（OR 匹配），以适应软科前端细节变动。
- **流式 CSV 写入**（Streaming）：
  - 以**批（batch）缓存**的方式写入，降低内存峰值。
  - **断点续爬**：若检测到目标 CSV 已存在，将读取已有行数作为 `start_idx`，从中断页继续抓取。
- **MySQL 批量入库（可选）**：
  - 读取 CSV 按 `chunksize` 切块插入，逐块提交，保证大数据量入库稳定性。
  - 表与字段映射在 `TaskCfg` 中显式配置（中英文列名映射）。
- **统一任务配置（TaskCfg）**：包含 `year / url_template / csv_columns_cn / xpaths_per_col / db_table / db_columns_en / csv_to_db_map` 等，便于扩展到其他榜单/年份。

---

## 数据处理流程（端到端）

1. **驱动初始化**：
   - `make_driver()` 创建无头 Chrome，设定窗口大小、语言、禁用自动化检测开关，失败时自动回退到 `about:blank`。
2. **任务加载**：
   - 以 `TaskCfg` 载入目标年份与 URL 模板，例如：
     - ARWU：`https://www.shanghairanking.cn/rankings/arwu/{year}`
     - BCUR：`https://www.shanghairanking.cn/rankings/bcur/{year}`
3. **分页与定位**：
   - 自动检测总页数（含重试）；逐页打开，获取榜单表格主体节点（内容容器）。
   - 逐行解析，先写入“排名”列，然后按 `xpaths_per_col` 依次尝试每个候选 XPath，命中即取值；缺失则以占位符 `"-"` 回填。
4. **批处理与落盘**：
   - 按 `CSV_BATCH_SIZE`（代码中为 500 行）聚合为 DataFrame，替换空值为 `"-"`，`encoding="utf-8-sig"` 追加写入。
   - 已写入总行数计数器 `total_written` 用于控制首批是否写表头。
5. **断点续爬**：
   - `_read_resume_rank(fpath)` 读取已存在 CSV 的行数，计算 `start_idx`（跳过表头）。后续从该页继续抓取。
6. **入库（可选）**：
   - `MySQLWriter.insert_dataframe()` 将 CSV 分块写入 MySQL，字段名按照 `csv_to_db_map` 映射到 `db_columns_en`。
   - 每个 chunk 完成后 `commit()`，异常时 `rollback()` 并中止。

---

## 输出数据格式

- **ARWU（`arwu_rank.csv`）**
  - 列：`排名, 学校名称, 国家, 总分`
  - 示例（见仓库同名 CSV）：前几行已提供样例。

- **BCUR（`bcur_rank.csv`）**
  - 列：`排名, 学校名称, 标签, 省市, 类型, 评分`
  - 示例（见仓库同名 CSV）：前几行已提供样例。

> 缺失值统一写为 `"-"`；CSV 采用 `UTF-8 with BOM (utf-8-sig)`，便于 Excel 打开。

---

## 运行环境

- **Python**：3.11/3.12（建议 3.12）
- **依赖**：见 `requirements.txt`（含 `selenium`, `webdriver-manager`, `pandas`, `numpy`, `pymysql` 等）
- **浏览器**：自动下载匹配版本的 ChromeDriver。若本机未安装 Chrome，`webdriver-manager` 会尝试管理相应驱动。

---

## 快速开始

```bash
# 1) 建议新建虚拟环境
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 2) 安装依赖
pip install -r requirements.txt

# 3) 直接运行合并版（默认抓取 2025）
python shranking_crawler.py

# 或单任务运行：
python arwu_crawler.py
python bcur_crawler.py
```

### 运行参数与环境变量

脚本本身不需要 CLI 参数；MySQL 连接可通过环境变量提供（仅在启用入库逻辑时使用）：

- `ARWU_DB_HOST`（默认：`localhost`）  
- `ARWU_DB_USER`（默认：`root`）  
- `ARWU_DB_PASS`（默认：`mysql0718`）  
- `ARWU_DB_NAME`（默认：`SHranking`）  

> 若不需要入库，可忽略数据库配置；脚本将只写 CSV。

---

## MySQL 表结构建议

- `arwu`：`(rank INT, name VARCHAR(255), country VARCHAR(128), overall_score DECIMAL(6,2))`
- `bcur`：`(rank INT, name VARCHAR(255), tags VARCHAR(255), province VARCHAR(64), category VARCHAR(64), score DECIMAL(6,2))`

> 实际建表字段与长度可依据生产需求细化；`MySQLWriter` 会按 `db_columns_en` 顺序插入。

---

## 稳定性与反爬策略

- **导航重试 + 指数退避**：`NAV_RETRY / NAV_BACKOFF_BASE` 控制；失败会先跳转 `about:blank` 清理状态。
- **多 XPath 回退**：前端结构轻微调整时仍能抓到字段。
- **分批写盘 + 分块入库**：降低内存占用，避免单次事务过大。

---

## 日志与可观测性

- 统一 `logging`，包含：页码/重试次数/累计写入/入库影响行数等关键信息。
- 异常捕获包括 `TimeoutException`、`WebDriverException`，打印精简错误信息便于定位。

---

## 断点续爬详细说明

- 若目标 CSV 已存在，程序会读取已写行数（不含表头），计算起始页与行偏移并继续抓取。
- 适合长时间任务或网络不稳定的环境；建议配合 `screen/tmux` 或容器运行。

---

## 版权与合规

- 本项目仅用于**学习与研究**。抓取数据版权归上海软科所有，请遵守其网站的 `Robots.txt/使用条款`，合理设置抓取频率，勿用于商业目的。

---

## 拓展与定制

- 新增榜单：复制一份 `TaskCfg`，调整 `url_template / csv_columns_cn / xpaths_per_col / 映射关系` 即可。
- 新增字段：在 `csv_columns_cn` 中添加列，并在 `xpaths_per_col` 对应位置补充候选 XPath。

