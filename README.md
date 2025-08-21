# 项目来源（Attribution）

本项目 **fork 自** [ysl2/scripts.shanghairanking](https://github.com/ysl2/scripts.shanghairanking)，
并在其基础上进行改写与优化。

---

# ShanghaiRanking Crawlers (ARWU & BCUR)

本项目提供对 **软科排名（ShanghaiRanking）** 官方网站的自动化抓取与落库脚本，覆盖：

- **ARWU 世界大学学术排名（Academic Ranking of World Universities, 2025）**
- **BCUR 中国大学排名（Best Chinese Universities Ranking, 2025）**

脚本基于 **Python 3.12+ / Selenium 4（Headless Chrome）** 实现，具备**断点续爬**、**流式写 CSV**、**批量写入 MySQL** 等工程化能力，适合数据工程/量化场景的稳定离线采集任务。

---

## 1. 数据来源（Data Sources）

- **站点**：<https://www.shanghairanking.cn/>
- **ARWU 2025 列表页**：`https://www.shanghairanking.cn/rankings/arwu/{year}`（代码中默认 `year='2025'`）  
  抓取字段：**排名、学校名称、国家、总分**。
- **BCUR 2025 列表页**：`https://www.shanghairanking.cn/rankings/bcur/{year}`（代码中默认 `year='2025'`）  
  抓取字段：**排名、学校名称、标签、省市、类型、评分**。

> 说明：页面为动态渲染，项目使用 **Selenium + XPath** 直接从 DOM 读取各列并翻页。

---

## 2. 目录与产物（Artifacts）

```
arwu_crawler.py        # ARWU 抓取与入库（单任务版本）
bcur_crawler.py        # BCUR 抓取与入库（单任务版本）
shranking_crawler.py   # 合并版：同时支持 ARWU 与 BCUR
arwu_rank.csv          # ARWU 抓取出的示例数据
bcur_rank.csv          # BCUR 抓取出的示例数据
requirements.txt       # 运行依赖（UTF-16 LE 编码）
```

**CSV 字段**：

- `arwu_rank.csv`：`排名, 学校名称, 国家, 总分`
- `bcur_rank.csv`：`排名, 学校名称, 标签, 省市, 类型, 评分`

示例（前 5 行）：

**ARWU**
|   排名 | 学校名称        | 国家   |   总分 |
|-------:|:----------------|:-------|-------:|
|      1 | 哈佛大学        | 美国   |  100   |
|      2 | 斯坦福大学      | 美国   |   76.8 |
|      3 | 麻省理工学院    | 美国   |   71.2 |
|      4 | 剑桥大学        | 英国   |   68.6 |
|      5 | 加州大学-伯克利 | 美国   |   61.8 |

**BCUR**
|   排名 | 学校名称     | 标签           | 省市   | 类型   |   评分 |
|-------:|:-------------|:---------------|:-------|:-------|-------:|
|      1 | 清华大学     | 双一流/985/211 | 北京   | 综合   | 1076.1 |
|      2 | 北京大学     | 双一流/985/211 | 北京   | 综合   | 1027.7 |
|      3 | 浙江大学     | 双一流/985/211 | 浙江   | 综合   |  868.9 |
|      4 | 上海交通大学 | 双一流/985/211 | 上海   | 综合   |  851.7 |
|      5 | 复旦大学     | 双一流/985/211 | 上海   | 综合   |  788.4 |

---

## 3. 处理流程（Processing Pipeline）

1. **启动 Headless Chrome**
   - 使用 `webdriver_manager` 自动安装兼容版本的 ChromeDriver。
   - 关键浏览器选项：`--headless=new`, `--no-sandbox`, `--disable-dev-shm-usage`, `--window-size=1366,900`。
2. **列表解析与翻页**
   - 通过稳定的 **XPath** 对各列元素定位：
     - ARWU：`排名(./td[1]/div)`、`学校名称(./td[2]/div/div[2]/div/span | ./td[2]/div/div[2]/div[1]/div/div/span)`、`国家(./td[3])`、`总分(./td[5])` 等；
     - BCUR：`学校名称(./td[2]/div/div[2]/div[1]/div/div/span)`、`标签(./td[2]/div/div[2]/p)`、`省市(./td[3])`、`类型(./td[4])`、`评分(./td[5])`。
   - 页面每次翻页前等待短冷却（默认 `0.25s`），并带 **重试与指数回退**（`NAV_RETRY=5, NAV_BACKOFF_BASE=1.2`）。
3. **流式写入 CSV（大数据稳态）**
   - 逐页解析**立即写入**本地 CSV，累计到 **`CSV_BATCH_SIZE=1000`** 行批量 `flush`，降低内存占用并提升稳定性。
   - **断点续爬**：启动时检查既有 CSV 的 **最后一行“排名”** 作为恢复点，仅抓未完成部分。
4. **MySQL 批量入库（可选）**
   - 通过 `pymysql` 执行 DDL/DML：
     - 建表时按任务生成字段（ARWU：`rank,name,country,overall_score`；BCUR：`rank,name,tags,province,category,score`），并设置 `UNIQUE KEY(rank,name)` 保证幂等。
   - **批量写入**采用 `executemany` 分批方式（**`DB_CHUNK_SIZE/DB_BATCH_SIZE = 5000`**），每批 `commit`，在数十万行规模下仍保持稳定。

关键时序常量（可在代码中修改）：

- `TIMEOUT_SEC=15`：元素查找/加载超时；
- `CLICK_COOLDOWN=0.25`：翻页点击后的最小等待；
- `NAV_RETRY=5` + `NAV_BACKOFF_BASE=1.2`：导航失败后的重试与回退；
- `CSV_BATCH_SIZE=1000`，`DB_CHUNK_SIZE/DB_BATCH_SIZE=5000`。

---

## 4. 环境准备（Setup）

1. **Python**：建议 3.12+  
2. **依赖**：
   ```bash
   # 注意 requirements.txt 为 UTF-16 LE 编码
   pip install -r requirements.txt
   ```
3. **Chrome / ChromeDriver**：自动安装，首次运行会下载到用户缓存目录（由 `webdriver_manager` 管理）。
4. **MySQL（可选）**：如果需要入库，先创建一个数据库（默认名 `SHranking`），并准备连接账号。

---

## 5. 运行方式（Usage）

### 直接抓取到 CSV
```bash
# 抓取 ARWU
python arwu_crawler.py

# 抓取 BCUR
python bcur_crawler.py

# 或者使用合并版（同时覆盖 ARWU 与 BCUR）
python shranking_crawler.py
```

### 写入 MySQL（可选）
- 通过 **环境变量**配置连接（代码默认值如下）：
  ```bash
  export ARWU_DB_HOST=localhost
  export ARWU_DB_USER=root
  export ARWU_DB_PASS=mysql0718
  export ARWU_DB_NAME=SHranking
  ```
- 运行脚本后会：
  1) 若表已存在则**重新创建**；
  2) 按 `5000` 行一批执行 `INSERT` 并 `COMMIT`；
  3) 记录累计插入行数到日志。

---

## 6. 工程特性（Engineering Notes）

- **稳态大数据抓取**：流式 CSV + 分批提交，避免一次性加载全量；
- **断点恢复**：根据已写 CSV 的最后排名恢复；
- **选择器冗余**：对“学校名称”等字段提供**多 XPath** 以适配不同 DOM 结构；
- **幂等入库**：唯一键 `(rank, name)` 防止重复；
- **日志友好**：统一 `logging` 格式，抓取与入库均输出进度。

---

## 7. 注意与合规（Notes & Compliance）

- 本项目仅用于**学习与研究**，请遵守 ShanghaiRanking 网站的 **使用条款/robots 指引**；
- 抓取频率应保持温和（保留默认冷却与重试即可）；
- 列表结构与 XPath 可能**随页面升级而变化**，必要时更新选择器。

---
