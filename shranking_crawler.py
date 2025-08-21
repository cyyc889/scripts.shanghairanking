#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Merged crawler for ARWU & BCUR (single-file, no CLI args)
- Features: headless Selenium + robust navigation retry + streaming CSV with resume + MySQL batch insert
- Tasks:
    * ARWU 2025  -> CSV: arwu_rank.csv  -> MySQL table: arwu
    * BCUR 2025  -> CSV: bcur_rank.csv  -> MySQL table: bcur
- DB creds via env:
    ARWU_DB_HOST (default: localhost)
    ARWU_DB_USER (default: root)
    ARWU_DB_PASS (default: mysql0718)
    ARWU_DB_NAME (default: SHranking)
"""

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

# ======= constants =======
TIMEOUT_SEC: int = 15
CLICK_COOLDOWN: float = 0.25
NAV_RETRY: int = 5
NAV_BACKOFF_BASE: float = 1.2
CSV_BATCH_SIZE: int = 1000
DB_CHUNK_SIZE: int = 5000

# ======= logging =======
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ======= task config =======
@dataclass(frozen=True)
class TaskCfg:
    taskname: str
    year: str
    url_template: str
    csv_columns_cn: list[str]
    # xpaths for *non-rank* columns, in the same order as csv_columns_cn[1:]
    xpaths_per_col: list[list[str]]
    db_table: str
    db_columns_en: list[str]           # in DB order, typically ["rank","name",...]
    csv_to_db_map: dict[str, str]      # CN->EN

TASKS: dict[str, TaskCfg] = {
    "arwu": TaskCfg(
        taskname="arwu",
        year="2025",
        url_template="https://www.shanghairanking.cn/rankings/arwu/{year}",
        csv_columns_cn=["排名", "学校名称", "国家", "总分"],
        xpaths_per_col=[
            # 学校名称（两个候选 XPath 二选一）
            ['./td[2]/div/div[2]/div/span', './td[2]/div/div[2]/div[1]/div/div/span'],
            ['./td[3]'],  # 国家
            ['./td[5]'],  # 总分
        ],
        db_table="arwu",
        db_columns_en=["rank", "name", "country", "overall_score"],
        csv_to_db_map={
            "排名": "rank",
            "学校名称": "name",
            "国家": "country",
            "总分": "overall_score",
        },
    ),
    "bcur": TaskCfg(
        taskname="bcur",
        year="2025",
        url_template="https://www.shanghairanking.cn/rankings/bcur/{year}",
        csv_columns_cn=["排名", "学校名称", "标签", "省市", "类型", "评分"],
        xpaths_per_col=[
            ["./td[2]/div/div[2]/div[1]/div/div/span"],  # 学校名称
            ["./td[2]/div/div[2]/p"],                     # 标签
            ["./td[3]"],                                  # 省市
            ["./td[4]"],                                  # 类型
            ["./td[5]"],                                  # 评分
        ],
        db_table="bcur",
        db_columns_en=["rank", "name", "tags", "province", "category", "score"],
        csv_to_db_map={
            "排名": "rank",
            "学校名称": "name",
            "标签": "tags",
            "省市": "province",
            "类型": "category",
            "评分": "score",
        },
    ),
}

# ======= db env =======
DB_HOST = os.getenv("ARWU_DB_HOST", "localhost")
DB_USER = os.getenv("ARWU_DB_USER", "root")
DB_PASSWORD = os.getenv("ARWU_DB_PASS", "mysql0718")
DB_NAME = os.getenv("ARWU_DB_NAME", "SHranking")

# ======= selenium helpers =======
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

# ======= dom helpers =======
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

def _content_box(driver):
    return WebDriverWait(driver, TIMEOUT_SEC).until(
        EC.presence_of_element_located((By.XPATH, '//*[@id="content-box"]'))
    )

def get_rows(driver) -> list:
    content_box = _content_box(driver)
    tbody = WebDriverWait(content_box, TIMEOUT_SEC).until(
        EC.presence_of_element_located((By.XPATH, './div[2]/table/tbody'))
    )
    return tbody.find_elements(By.XPATH, "./tr")

def get_total_pages(driver) -> int:
    try:
        content_box = _content_box(driver)
        ul = WebDriverWait(content_box, TIMEOUT_SEC).until(
            EC.presence_of_element_located((By.XPATH, "./ul"))
        )
        btns = ul.find_elements(By.XPATH, "./li/a")
        nums: list[int] = [int(a.text.strip()) for a in btns if a.text.strip().isdigit()]
        return max(nums) if nums else 1
    except Exception:
        return 1

def click_next_page(driver) -> bool:
    try:
        content_box = _content_box(driver)
        next_btn = WebDriverWait(content_box, TIMEOUT_SEC).until(
            EC.element_to_be_clickable((By.XPATH, './ul/li[contains(@class, "ant-pagination-next")]/a'))
        )
        driver.execute_script("arguments[0].click();", next_btn)
        time.sleep(CLICK_COOLDOWN)
        return True
    except Exception:
        return False

# ======= resume & csv =======
def _read_resume_rank(fpath: str) -> int:
    try:
        if not os.path.exists(fpath) or os.path.getsize(fpath) == 0:
            return 0
        tail = pd.read_csv(fpath, usecols=["排名"], dtype=str).tail(1)
        if tail.empty:
            return 0
        last_rank = str(tail.iloc[0]["排名"]).strip()
        return int(last_rank) if last_rank.isdigit() else 0
    except Exception as e:
        logger.warning("Failed to read resume rank: %s", e)
        return 0

def _flush_to_csv(rows_buf: list[list[str]], fpath: str, columns: list[str], *, total_written: int) -> int:
    if not rows_buf:
        return total_written
    df_batch = pd.DataFrame(rows_buf, columns=columns)
    df_batch.replace([np.nan, ""], "-", inplace=True)
    df_batch.to_csv(
        fpath,
        mode="a",
        index=False,
        encoding="utf-8-sig",
        header=(total_written == 0),
    )
    total_written += len(df_batch)
    rows_buf.clear()
    return total_written

# ======= crawling (streaming csv + resume) =======
def crawl_stream_to_csv(driver: selenium.webdriver.Chrome, cfg: TaskCfg, out_dir: str = ".") -> tuple[str, int]:
    os.makedirs(out_dir, exist_ok=True)
    fpath = os.path.join(out_dir, f"{cfg.taskname}_rank.csv")

    url = cfg.url_template.format(year=cfg.year)
    get_with_retry(driver, url)

    total_pages = get_total_pages(driver)
    logger.info("Detected total pages: %s (task=%s)", total_pages, cfg.taskname)

    resume_rank = _read_resume_rank(fpath)
    total_written = resume_rank
    if resume_rank > 0:
        logger.info("Resume from rank=%d (append CSV)", resume_rank)
    else:
        if os.path.exists(fpath):
            os.remove(fpath)

    rows0 = get_rows(driver)
    rows_per_page = max(1, len(rows0))
    skip_pages = resume_rank // rows_per_page
    skip_offset = resume_rank % rows_per_page

    rows_buf: list[list[str]] = []
    rank_counter = resume_rank + 1

    for page in range(1, total_pages + 1):
        if resume_rank > 0 and page <= skip_pages:
            logger.info("Skip page %d/%d (already done)", page, total_pages)
            if page < total_pages:
                if not click_next_page(driver):
                    logger.warning("Next page not clickable while skipping at page %d", page)
                    break
            continue

        logger.info("Page %d / %d (task=%s)", page, total_pages, cfg.taskname)
        rows = get_rows(driver)

        start_idx = skip_offset if (resume_rank > 0 and page == skip_pages + 1) else 0

        for row in rows[start_idx:]:
            others = [wait_find_text_or_none(row, xps) for xps in cfg.xpaths_per_col]
            row_values = [str(rank_counter)] + others
            rows_buf.append(row_values)
            rank_counter += 1

            if len(rows_buf) >= CSV_BATCH_SIZE:
                total_written = _flush_to_csv(rows_buf, fpath, cfg.csv_columns_cn, total_written=total_written)

        if page < total_pages:
            if not click_next_page(driver):
                logger.warning("Next page not clickable at page %d; stop early.", page)
                break

    total_written = _flush_to_csv(rows_buf, fpath, cfg.csv_columns_cn, total_written=total_written)
    logger.info("CSV saved: %s (rows=%d)", fpath, total_written)
    return fpath, total_written

# ======= db helpers =======
def q(name: str) -> str:
    return f"`{name}`"

class MySQLWriter:
    def __init__(self, host: str, user: str, password: str, db: str):
        self.host = host
        self.user = user
        self.password = password
        self.db = db
        self._conn: Optional[pymysql.Connection] = None

    def __enter__(self) -> "MySQLWriter":
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

    def __exit__(self, exc_type, exc, tb) -> None:
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

    def _run(self, sql: str, rows: Optional[list[tuple]] = None, params: Optional[Iterable[Any]] = None) -> int:
        assert self._conn is not None, "Connection not open"
        with self._conn.cursor() as cur:
            if rows is not None:
                cur.executemany(sql, rows)
            else:
                cur.execute(sql, params or ())
            return cur.rowcount

    def recreate_table(self, cfg: TaskCfg) -> None:
        drop_sql = f"DROP TABLE IF EXISTS {q(cfg.db_table)}"
        self._run(drop_sql)
        logger.info("Dropped table if existed: %s", cfg.db_table)

        cols_sql_parts: list[str] = [
            f"{q('rank')} INT NOT NULL",
            f"{q('name')} VARCHAR(255) NOT NULL",
        ]
        for col in cfg.db_columns_en:
            if col in ("rank", "name"):
                continue
            cols_sql_parts.append(f"{q(col)} VARCHAR(255) NOT NULL")

        uniq_cols = ", ".join([q(c) for c in ("rank", "name")])
        cols_sql_parts.append(f"UNIQUE KEY uniq_rank_name ({uniq_cols})")

        create_sql = (
            f"CREATE TABLE {q(cfg.db_table)} ("
            + ", ".join(cols_sql_parts)
            + ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
        )
        self._run(create_sql)
        logger.info("Recreated table: %s", cfg.db_table)

    def insert_dataframe(self, df: pd.DataFrame, cfg: TaskCfg) -> int:
        if df.empty:
            logger.warning("Empty DataFrame for task=%s; skip DB insert.", cfg.taskname)
            return 0

        rows: list[tuple] = []
        for _, r in df.iterrows():
            values: dict[str, Any] = {}
            for csv_cn, db_en in cfg.csv_to_db_map.items():
                if db_en == "rank":
                    # 保证 rank 为整型递增
                    values["rank"] = int(str(r.get(csv_cn, "0")).strip() or "0")
                else:
                    values[db_en] = str(r.get(csv_cn, "")).strip() or "-"
            # 统一为 DB 列顺序
            row_tuple = tuple(values[k] for k in cfg.db_columns_en)
            rows.append(row_tuple)

        cols_sql = ", ".join(q(c) for c in cfg.db_columns_en)
        placeholders = ", ".join(["%s"] * len(cfg.db_columns_en))
        insert_sql = f"INSERT INTO {q(cfg.db_table)} ({cols_sql}) VALUES ({placeholders})"
        affected = self._run(insert_sql, rows=rows)
        logger.info("DB insert done: table=%s, affected_rows=%d", cfg.db_table, affected)
        return affected

# ======= main (no args) =======
def run_all(out_dir: str = ".") -> None:
    driver: Optional[selenium.webdriver.Chrome] = None
    try:
        driver = make_driver()
        with MySQLWriter(DB_HOST, DB_USER, DB_PASSWORD, DB_NAME) as writer:
            for key in ("arwu", "bcur"):
                cfg = TASKS[key]
                # crawl -> csv
                fpath, total_rows = crawl_stream_to_csv(driver, cfg, out_dir=out_dir)
                logger.info("Task=%s rows(total)=%d", cfg.taskname, total_rows)
                # recreate table and write chunks
                writer.recreate_table(cfg)
                inserted = 0
                for chunk in pd.read_csv(fpath, chunksize=DB_CHUNK_SIZE):
                    affected = writer.insert_dataframe(chunk, cfg)
                    writer.commit()
                    inserted += affected
                    logger.info("DB progress: table=%s affected=%d, cumulative=%d", cfg.db_table, affected, inserted)
                logger.info("DB insert finished: table=%s total_rows=%d", cfg.db_table, inserted)
    finally:
        if driver:
            driver.quit()

def main() -> None:
    run_all(out_dir=".")

if __name__ == "__main__":
    main()
