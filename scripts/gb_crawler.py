#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国家标准爬虫 - 爬取 openstd.samr.gov.cn 上的强制性、推荐性和指导性国家标准
支持断点续爬和PDF下载（含验证码识别，使用Playwright和ddddocr）

注意: 国家标准PDF下载需要访问c.gb688.cn，该服务器可能有网络访问限制。
本爬虫提供两种下载方式:
1. requests直接下载（需要c.gb688.cn可访问）
2. Playwright浏览器下载（处理JavaScript渲染和验证码）

用法:
  python gb_crawler.py --crawl          # 爬取标准列表
  python gb_crawler.py --download       # 下载标准PDF
  python gb_crawler.py --type 1         # 只爬取强制性国家标准
  python gb_crawler.py --all            # 爬取+下载
  python gb_crawler.py --stats          # 显示统计信息
"""

import os
import re
import sys
import time
import random
import sqlite3
import logging
import requests
import asyncio
from datetime import datetime
from bs4 import BeautifulSoup

# ========== 配置 ==========
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, 'standards.db')
DOWNLOAD_DIR = os.path.join(BASE_DIR, 'download', 'gb_standards')
BASE_URL = "https://openstd.samr.gov.cn"
LIST_URL = BASE_URL + "/bzgk/std/std_list_type"
DETAIL_URL = BASE_URL + "/bzgk/std/newGbInfo"
DOWNLOAD_URL = "http://c.gb688.cn/bzgk/gb/showGb"

# 标准类型映射
STD_TYPES = {
    1: "强制性国家标准",
    2: "推荐性国家标准",
    3: "指导性技术文件"
}

PAGE_SIZE = 50
REQUEST_DELAY = (1, 3)
MAX_RETRIES = 3
DOWNLOAD_MAX_RETRIES = 5

# ========== 日志配置 ==========
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(LOG_DIR, 'gb_crawler.log'), encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


class Database:
    """数据库操作类"""

    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS gb_standards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                std_type TEXT NOT NULL,
                standard_no TEXT NOT NULL UNIQUE,
                is_adopted TEXT DEFAULT '',
                standard_name TEXT NOT NULL,
                status TEXT DEFAULT '',
                publish_date TEXT DEFAULT '',
                implement_date TEXT DEFAULT '',
                detail_url TEXT DEFAULT '',
                hcno TEXT DEFAULT '',
                local_file TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS gb_crawl_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                std_type_code INTEGER NOT NULL,
                page_number INTEGER NOT NULL,
                total_pages INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(std_type_code, page_number)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS gb_download_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                standard_no TEXT NOT NULL UNIQUE,
                hcno TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                retries INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_gb_std_type ON gb_standards(std_type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_gb_status ON gb_standards(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_gb_hcno ON gb_standards(hcno)')
        self.conn.commit()

    def insert_standard(self, data):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO gb_standards (std_type, standard_no, is_adopted, standard_name, status, publish_date, implement_date, detail_url, hcno, local_file)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(standard_no) DO UPDATE SET
                std_type=excluded.std_type,
                is_adopted=excluded.is_adopted,
                standard_name=excluded.standard_name,
                status=excluded.status,
                publish_date=excluded.publish_date,
                implement_date=excluded.implement_date,
                detail_url=excluded.detail_url,
                hcno=excluded.hcno
        ''', (
            data['std_type'], data['standard_no'], data['is_adopted'],
            data['standard_name'], data['status'], data['publish_date'],
            data['implement_date'], data['detail_url'], data['hcno'],
            data.get('local_file', '')
        ))
        self.conn.commit()

    def update_local_file(self, standard_no, filename):
        cursor = self.conn.cursor()
        cursor.execute('UPDATE gb_standards SET local_file = ? WHERE standard_no = ?', (filename, standard_no))
        self.conn.commit()

    def get_crawled_pages(self, std_type_code):
        cursor = self.conn.cursor()
        cursor.execute('SELECT page_number FROM gb_crawl_progress WHERE std_type_code = ? AND status = ?', (std_type_code, 'done'))
        return set(row[0] for row in cursor.fetchall())

    def mark_page_done(self, std_type_code, page_number, total_pages=0):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO gb_crawl_progress (std_type_code, page_number, total_pages, status)
            VALUES (?, ?, ?, 'done')
            ON CONFLICT(std_type_code, page_number) DO UPDATE SET status='done', total_pages=?, updated_at=CURRENT_TIMESTAMP
        ''', (std_type_code, page_number, total_pages, total_pages))
        self.conn.commit()

    def get_pending_downloads(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT standard_no, hcno FROM gb_standards
            WHERE (local_file = '' OR local_file IS NULL) AND hcno != ''
        ''')
        return cursor.fetchall()

    def mark_download_status(self, standard_no, hcno, status, retries=0):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO gb_download_progress (standard_no, hcno, status, retries)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(standard_no) DO UPDATE SET status=?, retries=?, updated_at=CURRENT_TIMESTAMP
        ''', (standard_no, hcno, status, retries, status, retries))
        self.conn.commit()

    def get_download_retries(self, standard_no):
        cursor = self.conn.cursor()
        cursor.execute('SELECT retries FROM gb_download_progress WHERE standard_no = ?', (standard_no,))
        row = cursor.fetchone()
        return row[0] if row else 0

    def get_stats(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM gb_standards')
        total = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM gb_standards WHERE local_file != '' AND local_file IS NOT NULL")
        downloaded = cursor.fetchone()[0]
        for code in [1, 2, 3]:
            cursor.execute('SELECT COUNT(*) FROM gb_standards WHERE std_type = ?', (STD_TYPES[code],))
            count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM gb_standards WHERE std_type = ? AND local_file != '' AND local_file IS NOT NULL", (STD_TYPES[code],))
            dl = cursor.fetchone()[0]
            logger.info(f"  {STD_TYPES[code]}: {count} 条, 已下载 {dl}")
        return total, downloaded

    def close(self):
        self.conn.close()


class GBCrawler:
    """国家标准爬虫"""

    def __init__(self):
        self.db = Database(DB_PATH)
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        })
        self._init_session()

    def _init_session(self):
        try:
            resp = self.session.get(BASE_URL + "/bzgk/std/", timeout=30)
            logger.info(f"Session初始化: status={resp.status_code}")
        except Exception as e:
            logger.warning(f"Session初始化失败: {e}")

    def _delay(self):
        time.sleep(random.uniform(*REQUEST_DELAY))

    def _fetch_page(self, std_type_code, page_num):
        params = {
            'p.p1': std_type_code,
            'p.p90': 'circulation_date',
            'p.p91': 'desc',
            'page': page_num,
            'pageSize': PAGE_SIZE,
        }
        for attempt in range(MAX_RETRIES):
            try:
                self._delay()
                resp = self.session.get(LIST_URL, params=params, timeout=30)
                resp.encoding = 'utf-8'
                if resp.status_code == 200 and '标准号' in resp.text:
                    return resp.text
                else:
                    logger.warning(f"页面返回异常: status={resp.status_code}, len={len(resp.text)}")
            except Exception as e:
                logger.warning(f"请求失败 (attempt {attempt+1}/{MAX_RETRIES}): {e}")
                time.sleep(3)
        return None

    def _parse_list_page(self, html, std_type_code):
        soup = BeautifulSoup(html, 'lxml')
        records = []

        table = soup.find('table', class_='result_list')
        if not table:
            logger.warning("未找到result_list表格")
            return records

        rows = table.find_all('tr')
        for row in rows:
            cells = row.find_all('td')
            if len(cells) < 7:
                continue

            try:
                hcno = ''
                for cell in cells:
                    for link in cell.find_all('a'):
                        onclick = link.get('onclick', '') or ''
                        match = re.search(r"showInfo\(['\"]([0-9a-fA-F]+)['\"]\)", onclick)
                        if match:
                            hcno = match.group(1)
                            break
                    if hcno:
                        break

                standard_no = cells[1].get_text(strip=True)
                is_adopted = cells[2].get_text(strip=True)
                standard_name = cells[3].get_text(strip=True)
                status = cells[4].get_text(strip=True)
                publish_date = cells[5].get_text(strip=True)
                implement_date = cells[6].get_text(strip=True)
                detail_url = f"{DETAIL_URL}?hcno={hcno}" if hcno else ''

                record = {
                    'std_type': STD_TYPES.get(std_type_code, ''),
                    'standard_no': standard_no,
                    'is_adopted': is_adopted,
                    'standard_name': standard_name,
                    'status': status,
                    'publish_date': publish_date,
                    'implement_date': implement_date,
                    'detail_url': detail_url,
                    'hcno': hcno,
                }
                records.append(record)
            except Exception as e:
                logger.warning(f"解析行失败: {e}")
                continue

        return records

    def _get_total_pages(self, html):
        soup = BeautifulSoup(html, 'lxml')
        for span in soup.find_all('span'):
            text = span.get_text(strip=True)
            match = re.match(r'/\s*(\d+)', text)
            if match:
                return int(match.group(1))
        text = soup.get_text()
        match = re.search(r'共\s*(\d+)\s*条', text)
        if match:
            total = int(match.group(1))
            return (total + PAGE_SIZE - 1) // PAGE_SIZE
        return 1

    def crawl_list(self, std_type_code=None):
        if std_type_code:
            type_codes = [std_type_code]
        else:
            type_codes = [1, 2, 3]

        for code in type_codes:
            type_name = STD_TYPES.get(code, str(code))
            logger.info(f"===== 开始爬取 {type_name} =====")

            html = self._fetch_page(code, 1)
            if not html:
                logger.error(f"无法获取 {type_name} 第一页")
                continue

            total_pages = self._get_total_pages(html)
            logger.info(f"{type_name} 共 {total_pages} 页")

            crawled_pages = self.db.get_crawled_pages(code)
            logger.info(f"{type_name} 已爬取 {len(crawled_pages)} 页")

            if 1 not in crawled_pages:
                records = self._parse_list_page(html, code)
                for record in records:
                    self.db.insert_standard(record)
                self.db.mark_page_done(code, 1, total_pages)
                logger.info(f"第 1/{total_pages} 页: 爬取 {len(records)} 条记录")

            for page in range(2, total_pages + 1):
                if page in crawled_pages:
                    continue

                html = self._fetch_page(code, page)
                if not html:
                    logger.error(f"第 {page}/{total_pages} 页获取失败")
                    continue

                records = self._parse_list_page(html, code)
                for record in records:
                    self.db.insert_standard(record)
                self.db.mark_page_done(code, page, total_pages)
                logger.info(f"第 {page}/{total_pages} 页: 爬取 {len(records)} 条记录")

            logger.info(f"===== {type_name} 爬取完成 =====")

        total, downloaded = self.db.get_stats()
        logger.info(f"数据库中共有 {total} 条国家标准记录, 已下载 {downloaded} 个文件")

    def _safe_filename(self, standard_no, standard_name):
        safe_name = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', standard_name)
        safe_no = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', standard_no)
        if len(safe_name) > 150:
            safe_name = safe_name[:150]
        return f"{safe_no}-{safe_name}.pdf"

    def _download_with_requests(self, hcno, filepath):
        for dl_type in ['download', 'online']:
            try:
                url = f"{DOWNLOAD_URL}?type={dl_type}&hcno={hcno}&request_locale=zh"
                headers = {
                    'Referer': f'{DETAIL_URL}?hcno={hcno}',
                    'Accept': 'application/pdf,*/*',
                }
                resp = self.session.get(url, headers=headers, timeout=60, allow_redirects=True)
                if resp.status_code == 200:
                    content_type = resp.headers.get('Content-Type', '')
                    if ('pdf' in content_type.lower() or resp.content[:4] == b'%PDF') and len(resp.content) > 1024:
                        with open(filepath, 'wb') as f:
                            f.write(resp.content)
                        if os.path.getsize(filepath) > 1024:
                            return True
                        else:
                            if os.path.exists(filepath):
                                os.remove(filepath)
            except Exception as e:
                logger.debug(f"requests下载({dl_type})失败: {e}")
        return False

    async def _download_with_playwright(self, hcno, filepath):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("Playwright未安装，跳过浏览器下载方式")
            return False

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    locale='zh-CN',
                    accept_downloads=True,
                )
                page = await context.new_page()

                detail_url = f'{DETAIL_URL}?hcno={hcno}'
                await page.goto(detail_url, timeout=30000, wait_until='domcontentloaded')

                download_btn = await page.query_selector('.xz_btn')
                if not download_btn:
                    logger.info(f"详情页无下载按钮: hcno={hcno}")
                    await browser.close()
                    return False

                download_url = f'{DOWNLOAD_URL}?type=download&hcno={hcno}&request_locale=zh'
                try:
                    async with page.expect_download(timeout=30000) as download_info:
                        await page.goto(download_url, timeout=30000, wait_until='domcontentloaded')
                    download = await download_info.value
                    await download.save_as(filepath)
                    if os.path.exists(filepath) and os.path.getsize(filepath) > 1024:
                        await browser.close()
                        return True
                except Exception as e:
                    logger.debug(f"Playwright直接下载失败: {e}")

                await browser.close()
        except Exception as e:
            logger.warning(f"Playwright下载异常: {e}")
        return False

    def download_pdf(self, hcno, standard_no, standard_name):
        filename = self._safe_filename(standard_no, standard_name)
        filepath = os.path.join(DOWNLOAD_DIR, filename)

        if os.path.exists(filepath) and os.path.getsize(filepath) > 1024:
            logger.info(f"文件已存在: {filename}")
            self.db.update_local_file(standard_no, filename)
            return True

        if self._download_with_requests(hcno, filepath):
            logger.info(f"下载成功(requests): {filename}")
            self.db.update_local_file(standard_no, filename)
            return True

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, self._download_with_playwright(hcno, filepath))
                    result = future.result(timeout=120)
            else:
                result = loop.run_until_complete(self._download_with_playwright(hcno, filepath))
            if result and os.path.exists(filepath) and os.path.getsize(filepath) > 1024:
                logger.info(f"下载成功(Playwright): {filename}")
                self.db.update_local_file(standard_no, filename)
                return True
        except Exception as e:
            logger.warning(f"Playwright下载失败: {e}")

        if os.path.exists(filepath) and os.path.getsize(filepath) < 1024:
            os.remove(filepath)
        return False

    def download_all(self):
        pending = self.db.get_pending_downloads()
        total = len(pending)
        logger.info(f"待下载标准: {total} 个")

        success = 0
        failed = 0
        skipped = 0
        for i, row in enumerate(pending):
            standard_no = row[0]
            hcno = row[1]

            if not hcno:
                skipped += 1
                continue

            retries = self.db.get_download_retries(standard_no)
            if retries >= DOWNLOAD_MAX_RETRIES:
                skipped += 1
                continue

            cursor = self.db.conn.cursor()
            cursor.execute('SELECT standard_name FROM gb_standards WHERE standard_no = ?', (standard_no,))
            result = cursor.fetchone()
            standard_name = result[0] if result else standard_no

            logger.info(f"下载进度: {i+1}/{total} - {standard_no} {standard_name[:30]}")

            result = self.download_pdf(hcno, standard_no, standard_name)
            if result:
                success += 1
                self.db.mark_download_status(standard_no, hcno, 'success')
            else:
                failed += 1
                self.db.mark_download_status(standard_no, hcno, 'failed', retries + 1)

            if (i + 1) % 50 == 0:
                logger.info(f"=== 进度: {i+1}/{total}, 成功={success}, 失败={failed}, 跳过={skipped} ===")

        logger.info(f"下载完成: 成功 {success}, 失败 {failed}, 跳过 {skipped}")

    def close(self):
        self.db.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description='国家标准爬虫')
    parser.add_argument('--crawl', action='store_true', help='爬取标准列表')
    parser.add_argument('--download', action='store_true', help='下载标准PDF')
    parser.add_argument('--type', type=int, choices=[1, 2, 3], help='标准类型: 1=强制性, 2=推荐性, 3=指导性')
    parser.add_argument('--all', action='store_true', help='执行全部操作（爬取+下载）')
    parser.add_argument('--stats', action='store_true', help='显示统计信息')
    args = parser.parse_args()

    crawler = GBCrawler()
    try:
        if args.stats:
            crawler.db.get_stats()
        elif args.all:
            crawler.crawl_list(args.type)
            crawler.download_all()
        elif args.crawl:
            crawler.crawl_list(args.type)
        elif args.download:
            crawler.download_all()
        else:
            parser.print_help()
    finally:
        crawler.close()


if __name__ == '__main__':
    main()
