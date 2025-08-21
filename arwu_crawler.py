#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd
import pymysql
import selenium
import webdriver_manager.chrome
from pymysql.cursors import DictCursor
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ========= 常量 =========
TIMEOUT_SEC: int = 15
CLICK_COOLDOWN: float = 0.25
NAV_RETRY: int = 5
NAV_BACKOFF_BASE: float = 1.2
CSV_BATCH_SIZE: int = 1000   # 每1000行写一次 CSV（抓取阶段）
DB_BATCH_SIZE: int = 5000    # 每5000行一批插入 MySQL（写库阶段）

# ========= 配置 =========
@dataclass(frozen=True)
class TaskCfg:
    year: str
    url_template: str
    csv_columns_cn: list[str]
    xpaths_per_col: list[list[str]]

ARWU_CFG = TaskCfg(
    year="2025",
    url_template="https://www.shanghairanking.cn/rankings/arwu/{year}",
    csv_columns_cn=["排名", "学校名称", "国家", "总分"],
    xpaths_per_col=[
        ['./td[1]/div'],
        ['./td[2]/div/div[2]/div/span', './td[2]/div/div[2]/div[1]/div/div/span'],
        ['./td[3]'],
        ['./td[5]'],
    ],
)

# ========= 日志 =========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ========= Selenium 驱动 =========
def make_driver() -> selenium.webdriver.Chrome:
    chrome_service = selenium.webdriver.ChromeService(
        webdriver_manager.chrome.ChromeDriverManager().install()
    )
    options = selenium.webdriver.ChromeOptions()
    options.page_load_strategy = "eager"
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1366,900")
    options.add_argument("--lang=zh-CN,zh;q=0.9,en;q=0.8")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
    )
    driver = selenium.webdriver.Chrome(service=chrome_service, options=options)
    driver.set_page_load_timeout(60)
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"},
        )
    except Exception:
        pass
    return driver

def get_with_retry(driver: selenium.webdriver.Chrome, url: str) -> None:
    last_err: Optional[BaseException] = None
    for i in range(1, NAV_RETRY + 1):
        try:
            logger.info("Open: %s (try %d/%d)", url, i, NAV_RETRY)
            driver.get(url)
            return
        except (TimeoutException, WebDriverException) as e:
            last_err = e
            logger.warning("Navigate failed (try %d): %s", i, getattr(e, "msg", repr(e)))
            try:
                driver.get("about:blank")
            except Exception:
                pass
            time.sleep(NAV_BACKOFF_BASE ** i)
    raise last_err if last_err else RuntimeError("Navigation failed")

# ========= DOM 工具 =========
def wait_find_text_or_none(row, xpaths: list[str]) -> str:
    for xp in xpaths:
        try:
            elems = row.find_elements(By.XPATH, xp)
            if elems:
                t = elems[0].text.strip()
                if t:
                    return t
        except Exception:
            continue
    return ""

def get_total_pages(content_box) -> int:
    try:
        ul = WebDriverWait(content_box, TIMEOUT_SEC).until(
            EC.presence_of_element_located((By.XPATH, './ul'))
        )
        btns = ul.find_elements(By.XPATH, './li/a')
        nums: list[int] = [int(a.text.strip()) for a in btns if a.text.strip().isdigit()]
        return max(nums) if nums else 1
    except Exception:
        return 1

def click_next_page(content_box) -> bool:
    try:
        next_btn = WebDriverWait(content_box, TIMEOUT_SEC).until(
            EC.element_to_be_clickable((By.XPATH, './ul/li[contains(@class, "ant-pagination-next")]/a'))
        )
        content_box.parent.execute_script("arguments[0].click();", next_btn)
        time.sleep(CLICK_COOLDOWN)
        return True
    except Exception:
        return False

# ========= 断点续爬工具 =========
def _read_resume_rank(fpath: str) -> int:
    """读取已存在 CSV 的最后一行排名，若文件不存在或为空则返回 0。"""
    try:
        if not os.path.exists(fpath) or os.path.getsize(fpath) == 0:
            return 0
        # 只读“排名”列，避免加载整表到内存
        tail = pd.read_csv(fpath, usecols=["排名"], dtype=str).tail(1)
        if tail.empty:
            return 0
        last_rank = str(tail.iloc[0]["排名"]).strip()
        return int(last_rank) if last_rank.isdigit() else 0
    except Exception as e:
        logger.warning("Failed to read resume rank: %s", e)
        return 0

# ========= CSV 刷新 =========
def _flush_to_csv(rows_data: list[list[str]], fpath: str, cfg: TaskCfg, *, total_written: int) -> int:
    """将内存中的 rows_data 刷新到 CSV 并清空缓冲。"""
    if not rows_data:
        return total_written
    df_batch = pd.DataFrame(rows_data, columns=cfg.csv_columns_cn)
    df_batch.replace([np.nan, ""], "-", inplace=True)
    df_batch.to_csv(
        fpath,
        mode="a",
        index=False,
        encoding="utf-8-sig",
        header=(total_written == 0),
    )
    total_written += len(df_batch)
    rows_data.clear()
    return total_written

# ========= 抓取 =========
def crawl_arwu(driver: selenium.webdriver.Chrome, year: str, out_dir: str = ".") -> str:
    cfg = ARWU_CFG
    url = cfg.url_template.format(year=year)
    get_with_retry(driver, url)

    os.makedirs(out_dir, exist_ok=True)
    fpath = os.path.join(out_dir, "arwu_rank.csv")

    # 断点信息（如果 CSV 已存在则不删除，继续追写）
    resume_rank = _read_resume_rank(fpath)
    total_written = resume_rank
    if resume_rank > 0:
        logger.info("Resume from rank=%d (append CSV)", resume_rank)
    else:
        if os.path.exists(fpath):
            os.remove(fpath)

    content_box = WebDriverWait(driver, TIMEOUT_SEC).until(
        EC.presence_of_element_located((By.XPATH, '//*[@id="content-box"]'))
    )
    WebDriverWait(content_box, TIMEOUT_SEC).until(
        EC.presence_of_element_located((By.XPATH, './div[2]/table/tbody'))
    )

    total_pages = get_total_pages(content_box)
    logger.info("Detected total pages: %s", total_pages)

    # 计算每页行数，以便估算需要跳过的页数和页内偏移
    tbody0 = WebDriverWait(content_box, TIMEOUT_SEC).until(
        EC.presence_of_element_located((By.XPATH, './div[2]/table/tbody'))
    )
    rows0 = tbody0.find_elements(By.XPATH, './tr')
    rows_per_page = max(1, len(rows0))
    skip_pages = resume_rank // rows_per_page
    skip_offset = resume_rank % rows_per_page

    rows_data: list[list[str]] = []
    rank_counter = resume_rank + 1

    for page in range(1, total_pages + 1):
        # 跳过已完成的整页
        if resume_rank > 0 and page <= skip_pages:
            logger.info("Skip page %d/%d (already done)", page, total_pages)
            if page < total_pages:
                if not click_next_page(content_box):
                    logger.warning("Next page not clickable while skipping at page %d", page)
                    break
            continue

        logger.info("Page %d / %d", page, total_pages)
        content_box = WebDriverWait(driver, TIMEOUT_SEC).until(
            EC.presence_of_element_located((By.XPATH, '//*[@id="content-box"]'))
        )
        tbody = WebDriverWait(content_box, TIMEOUT_SEC).until(
            EC.presence_of_element_located((By.XPATH, './div[2]/table/tbody'))
        )
        rows = tbody.find_elements(By.XPATH, './tr')

        # 第一页采集时若存在页内偏移，则跳过前 skip_offset 条；后续页自动从 0 开始
        start_idx = skip_offset if (resume_rank > 0 and page == skip_pages + 1) else 0

        for row in rows[start_idx:]:
            row_values = [wait_find_text_or_none(row, xps) for xps in cfg.xpaths_per_col]
            # 覆盖排名为连续自增，保证唯一
            row_values[0] = str(rank_counter)
            rows_data.append(row_values)
            rank_counter += 1

            # 达到批量阈值，写入文件
            if len(rows_data) >= CSV_BATCH_SIZE:
                total_written = _flush_to_csv(rows_data, fpath, cfg, total_written=total_written)

        if page < total_pages:
            if not click_next_page(content_box):
                logger.warning("Next page not clickable at page %d", page)
                break

    # 写剩余部分
    total_written = _flush_to_csv(rows_data, fpath, cfg, total_written=total_written)
    logger.info("CSV saved: %s (rows=%d)", fpath, total_written)
    return fpath

# ========= MySQL =========
def q(name: str) -> str:
    return f"`{name}`"

class MySQLWriter:
    def __init__(self):
        self.host = os.getenv("ARWU_DB_HOST", "localhost")
        self.user = os.getenv("ARWU_DB_USER", "root")
        self.password = os.getenv("ARWU_DB_PASS", "mysql0718")
        self.db = os.getenv("ARWU_DB_NAME", "SHranking")
        self._conn: Optional[pymysql.Connection] = None

    def __enter__(self):
        self._conn = pymysql.connect(
            host=self.host,
            user=self.user,
            password=self.password,
            database=self.db,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
        )
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self._conn:
            return
        try:
            if exc:
                self._conn.rollback()
            else:
                self._conn.commit()
        finally:
            self._conn.close()
            self._conn = None

    def commit(self) -> None:
        assert self._conn is not None
        self._conn.commit()

    def _run(self, sql: str, rows: Optional[Iterable[tuple]] = None, params: Optional[Iterable[Any]] = None) -> int:
        """统一执行函数：无 rows -> execute；有 rows -> executemany。返回影响行数。"""
        assert self._conn is not None
        with self._conn.cursor() as cur:
            if rows is not None:
                cur.executemany(sql, rows)
            else:
                cur.execute(sql, params or ())
            return cur.rowcount

    def recreate_table(self) -> None:
        drop_sql = f"DROP TABLE IF EXISTS {q('arwu')}"
        self._run(drop_sql)
        create_sql = f"""
        CREATE TABLE {q('arwu')} (
            {q('rank')} INT UNSIGNED NOT NULL,
            {q('name')} VARCHAR(255) NOT NULL,
            {q('country')} VARCHAR(255) NOT NULL,
            {q('overall_score')} VARCHAR(255) NOT NULL,
            UNIQUE KEY uniq_rank_name ({q('rank')}, {q('name')})
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """.strip()
        self._run(create_sql)
        logger.info("Recreated table: arwu")

    def insert_dataframe(self, df: pd.DataFrame) -> int:
        if df.empty:
            logger.warning("Empty DataFrame; skip DB insert.")
            return 0
        rows: list[tuple] = []
        for _, r in df.iterrows():
            rows.append((
                int(str(r.get("排名", "0")).strip() or "0"),
                str(r.get("学校名称", "")).strip(),
                (str(r.get("国家", "")).strip() or "-"),
                (str(r.get("总分", "")).strip() or "-"),
            ))
        cols_sql = f"{q('rank')},{q('name')},{q('country')},{q('overall_score')}"
        placeholders = "%s,%s,%s,%s"
        insert_sql = f"INSERT INTO {q('arwu')} ({cols_sql}) VALUES ({placeholders})"
        affected = self._run(insert_sql, rows=rows)
        logger.info("DB insert done: affected_rows=%d", affected)
        return affected

# ========= 主程序 =========
def main() -> None:
    driver: Optional[selenium.webdriver.Chrome] = None
    try:
        driver = make_driver()
        fpath = crawl_arwu(driver, year=ARWU_CFG.year, out_dir=".")
        logger.info("ARWU %s saved to %s", ARWU_CFG.year, fpath)

        # 以流式方式读入 CSV，分批插入数据库，每批提交一次
        with MySQLWriter() as writer:
            writer.recreate_table()
            total = 0
            for chunk in pd.read_csv(fpath, chunksize=DB_BATCH_SIZE):
                affected = writer.insert_dataframe(chunk)
                writer.commit()  # 每批提交
                total += affected
                logger.info("DB progress: inserted=%d (cumulative=%d)", affected, total)
            logger.info("DB insert finished: total_rows=%d", total)
    finally:
        if driver:
            driver.quit()

if __name__ == "__main__":
    main()
