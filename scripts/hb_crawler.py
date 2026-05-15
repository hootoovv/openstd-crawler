#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行业标准爬虫 - 爬取 hbba.sacinfo.org.cn 上的行业标准
支持断点续爬和PDF下载（含ddddocr验证码识别）

下载流程:
1. 获取标准列表（JSON API）
2. 访问标准的在线查看页面
3. 获取验证码图片
4. 使用ddddocr识别验证码
5. 提交验证码获取临时下载码
6. 使用下载码下载PDF

用法:
  python hb_crawler.py --crawl          # 爬取标准列表
  python hb_crawler.py --download       # 下载标准PDF
  python hb_crawler.py --all            # 爬取+下载
  python hb_crawler.py --stats          # 显示统计信息
"""

import os
import re
import sys
import time
import random
import sqlite3
import logging
import requests
from datetime import datetime
from bs4 import BeautifulSoup

# ========== 配置 ==========
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, 'standards.db')
DOWNLOAD_DIR = os.path.join(BASE_DIR, 'download', 'hb_standards')
BASE_URL = "https://hbba.sacinfo.org.cn"
LIST_API = BASE_URL + "/stdQueryList"
DETAIL_URL = BASE_URL + "/stdDetail"
ONLINE_URL = BASE_URL + "/portal/online"
CAPTCHA_URL = BASE_URL + "/portal/validate-code"
VALIDATE_READ_API = BASE_URL + "/portal/validate-captcha/read"
VALIDATE_DOWN_API = BASE_URL + "/portal/validate-captcha/down"
DOWNLOAD_API = BASE_URL + "/portal/download"
ONLINE_READ_URL = BASE_URL + "/attachment/onlineRead"

PAGE_SIZE = 100
REQUEST_DELAY = (0.5, 2)
MAX_RETRIES = 5
CAPTCHA_MAX_RETRIES = 10
DOWNLOAD_MAX_RETRIES = 5

# ========== 日志配置 ==========
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(LOG_DIR, 'hb_crawler.log'), encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# ddddocr OCR识别器（延迟初始化）
_ocr = None

def get_ocr():
    global _ocr
    if _ocr is None:
        import ddddocr
        _ocr = ddddocr.DdddOcr(show_ad=False, beta=True)
        logger.info("ddddocr 初始化完成")
    return _ocr


class Database:
    """数据库操作类（行业标准）"""

    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS hb_standards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                standard_no TEXT NOT NULL UNIQUE,
                standard_name TEXT NOT NULL,
                industry TEXT DEFAULT '',
                status TEXT DEFAULT '',
                approve_date TEXT DEFAULT '',
                implement_date TEXT DEFAULT '',
                detail_url TEXT DEFAULT '',
                pk TEXT DEFAULT '',
                local_file TEXT DEFAULT '',
                charge_dept TEXT DEFAULT '',
                revise_std_codes TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS hb_crawl_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_number INTEGER NOT NULL,
                total_pages INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(page_number)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS hb_download_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                standard_no TEXT NOT NULL UNIQUE,
                pk TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                retries INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_hb_industry ON hb_standards(industry)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_hb_status ON hb_standards(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_hb_pk ON hb_standards(pk)')
        self.conn.commit()

    def insert_standard(self, data):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO hb_standards (standard_no, standard_name, industry, status, approve_date, implement_date, detail_url, pk, local_file, charge_dept, revise_std_codes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(standard_no) DO UPDATE SET
                standard_name=excluded.standard_name,
                industry=excluded.industry,
                status=excluded.status,
                approve_date=excluded.approve_date,
                implement_date=excluded.implement_date,
                detail_url=excluded.detail_url,
                pk=excluded.pk,
                charge_dept=excluded.charge_dept,
                revise_std_codes=excluded.revise_std_codes
        ''', (
            data['standard_no'], data['standard_name'], data['industry'],
            data['status'], data['approve_date'], data['implement_date'],
            data['detail_url'], data['pk'], data.get('local_file', ''),
            data.get('charge_dept', ''), data.get('revise_std_codes', '')
        ))
        self.conn.commit()

    def update_local_file(self, standard_no, filename):
        cursor = self.conn.cursor()
        cursor.execute('UPDATE hb_standards SET local_file = ? WHERE standard_no = ?', (filename, standard_no))
        self.conn.commit()

    def get_crawled_pages(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT page_number FROM hb_crawl_progress WHERE status = ?', ('done',))
        return set(row[0] for row in cursor.fetchall())

    def mark_page_done(self, page_number, total_pages=0):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO hb_crawl_progress (page_number, total_pages, status)
            VALUES (?, ?, 'done')
            ON CONFLICT(page_number) DO UPDATE SET status='done', total_pages=?, updated_at=CURRENT_TIMESTAMP
        ''', (page_number, total_pages, total_pages))
        self.conn.commit()

    def get_pending_downloads(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT standard_no, pk FROM hb_standards
            WHERE (local_file = '' OR local_file IS NULL) AND pk != ''
            AND standard_no NOT IN (
                SELECT standard_no FROM hb_download_progress WHERE status IN ('not_public', 'success', 'no_file')
            )
        ''')
        return cursor.fetchall()

    def mark_download_status(self, standard_no, pk, status, retries=0):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO hb_download_progress (standard_no, pk, status, retries)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(standard_no) DO UPDATE SET status=?, retries=?, updated_at=CURRENT_TIMESTAMP
        ''', (standard_no, pk, status, retries, status, retries))
        self.conn.commit()

    def get_download_retries(self, standard_no):
        cursor = self.conn.cursor()
        cursor.execute('SELECT retries FROM hb_download_progress WHERE standard_no = ?', (standard_no,))
        row = cursor.fetchone()
        return row[0] if row else 0

    def get_download_status(self, standard_no):
        cursor = self.conn.cursor()
        cursor.execute('SELECT status FROM hb_download_progress WHERE standard_no = ?', (standard_no,))
        row = cursor.fetchone()
        return row[0] if row else None

    def get_stats(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM hb_standards')
        total = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM hb_standards WHERE local_file != '' AND local_file IS NOT NULL")
        downloaded = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM hb_standards WHERE local_file = '' OR local_file IS NULL")
        pending = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM hb_download_progress WHERE status = 'not_public'")
        not_public = cursor.fetchone()[0]
        logger.info(f"  行业标准总数: {total}, 已下载: {downloaded}, 待下载: {pending}, 未公开: {not_public}")
        return total, downloaded

    def close(self):
        self.conn.close()


class HBCrawler:
    """行业标准爬虫"""

    def __init__(self):
        self.db = Database(DB_PATH)
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Origin': BASE_URL,
            'Referer': BASE_URL + '/stdList',
            'X-Requested-With': 'XMLHttpRequest',
        })

    def _delay(self):
        time.sleep(random.uniform(*REQUEST_DELAY))

    def _fetch_list_page(self, page_num):
        data = {'current': page_num, 'size': PAGE_SIZE}
        for attempt in range(MAX_RETRIES):
            try:
                self._delay()
                resp = self.session.post(LIST_API, data=data, timeout=30)
                if resp.status_code == 200:
                    result = resp.json()
                    if 'records' in result:
                        return result
                logger.warning(f"API返回异常: status={resp.status_code}")
            except Exception as e:
                logger.warning(f"请求失败 (attempt {attempt+1}/{MAX_RETRIES}): {e}")
                time.sleep(3)
        return None

    def _parse_records(self, api_result, page_num):
        records = []
        total_pages = api_result.get('pages', 1)

        for item in api_result.get('records', []):
            pk = item.get('pk', '')
            standard_no = item.get('code', '')
            standard_name = item.get('chName', '')
            industry = item.get('industry', '')
            status = item.get('status', '')
            charge_dept = item.get('chargeDept', '')
            revise_std_codes = item.get('reviseStdCodes', '')

            approve_date = ''
            if item.get('issueDate'):
                try:
                    approve_date = datetime.fromtimestamp(item['issueDate'] / 1000).strftime('%Y-%m-%d')
                except:
                    approve_date = str(item.get('issueDate', ''))

            implement_date = ''
            if item.get('actDate'):
                try:
                    implement_date = datetime.fromtimestamp(item['actDate'] / 1000).strftime('%Y-%m-%d')
                except:
                    implement_date = str(item.get('actDate', ''))

            detail_url = f"{DETAIL_URL}/{pk}" if pk else ''

            record = {
                'standard_no': standard_no,
                'standard_name': standard_name,
                'industry': industry,
                'status': status,
                'approve_date': approve_date,
                'implement_date': implement_date,
                'detail_url': detail_url,
                'pk': pk,
                'charge_dept': charge_dept,
                'revise_std_codes': revise_std_codes,
            }
            records.append(record)

        return records, total_pages

    def crawl_list(self):
        logger.info("===== 开始爬取行业标准列表 =====")

        result = self._fetch_list_page(1)
        if not result:
            logger.error("无法获取行业标准第一页")
            return

        records, total_pages = self._parse_records(result, 1)
        total_records = result.get('total', 0)
        logger.info(f"行业标准共 {total_pages} 页 (每页 {PAGE_SIZE} 条, 总计 {total_records} 条)")

        crawled_pages = self.db.get_crawled_pages()
        logger.info(f"已爬取 {len(crawled_pages)} 页")

        if 1 not in crawled_pages:
            for record in records:
                self.db.insert_standard(record)
            self.db.mark_page_done(1, total_pages)
            logger.info(f"第 1/{total_pages} 页: 爬取 {len(records)} 条记录")

        for page in range(2, total_pages + 1):
            if page in crawled_pages:
                continue

            result = self._fetch_list_page(page)
            if not result:
                logger.error(f"第 {page}/{total_pages} 页获取失败")
                continue

            records, _ = self._parse_records(result, page)
            for record in records:
                self.db.insert_standard(record)
            self.db.mark_page_done(page, total_pages)
            logger.info(f"第 {page}/{total_pages} 页: 爬取 {len(records)} 条记录")

        logger.info("===== 行业标准列表爬取完成 =====")
        self.db.get_stats()

    def _safe_filename(self, standard_no, standard_name):
        safe_name = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', standard_name)
        safe_no = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', standard_no)
        if len(safe_name) > 150:
            safe_name = safe_name[:150]
        return f"{safe_no}-{safe_name}.pdf"

    def _check_public(self, pk):
        try:
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'zh-CN,zh;q=0.9',
            })
            online_url = f'{ONLINE_URL}/{pk}'
            resp = session.get(online_url, timeout=15, allow_redirects=True)
            if '尚未公开' in resp.text:
                return False, session
            return True, session
        except Exception as e:
            logger.warning(f"检查公开状态失败: {e}")
            return False, None

    def _recognize_captcha_and_download(self, pk):
        """
        识别验证码并下载PDF
        返回: (status, pdf_content_or_None)
        status: 'success', 'no_file', 'captcha_failed'
        """
        ocr = get_ocr()

        for attempt in range(CAPTCHA_MAX_RETRIES):
            try:
                captcha_session = requests.Session()
                captcha_session.headers.update({
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'zh-CN,zh;q=0.9',
                })

                online_url = f'{ONLINE_URL}/{pk}'
                captcha_session.get(online_url, timeout=15)

                captcha_img_url = f"{CAPTCHA_URL}?pk={pk}&t={int(time.time()*1000)}"
                resp = captcha_session.get(captcha_img_url, timeout=15)
                if resp.status_code != 200 or len(resp.content) < 50:
                    logger.warning(f"验证码图片获取失败 (attempt {attempt+1})")
                    continue

                captcha_text = ocr.classification(resp.content)
                captcha_clean = re.sub(r'[^0-9a-zA-Z]', '', captcha_text).strip()

                if len(captcha_clean) < 3:
                    logger.warning(f"验证码识别结果过短: '{captcha_clean}' (attempt {attempt+1})")
                    continue

                logger.debug(f"验证码识别: '{captcha_text}' -> '{captcha_clean}' (attempt {attempt+1})")

                validate_resp = captcha_session.post(
                    VALIDATE_DOWN_API,
                    data={'captcha': captcha_clean, 'pk': pk},
                    headers={
                        'X-Requested-With': 'XMLHttpRequest',
                        'Referer': online_url,
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'Accept': 'application/json, text/javascript, */*; q=0.01',
                    },
                    timeout=15
                )

                if validate_resp.status_code == 200:
                    result = validate_resp.json()
                    if result.get('code') == 0:
                        temp_code = result.get('msg', '')
                        logger.info(f"验证码验证成功 (attempt {attempt+1})")

                        download_url = f"{DOWNLOAD_API}/{temp_code}"
                        dl_resp = captcha_session.get(download_url, timeout=120, allow_redirects=False)

                        # 重定向到首页 = 无PDF文件
                        if dl_resp.status_code in [301, 302, 303, 307, 308]:
                            location = dl_resp.headers.get('Location', '')
                            if 'hbba.sacinfo.org.cn/' in location and 'download' not in location:
                                logger.info(f"标准无PDF文件可供下载(重定向到首页)")
                                return 'no_file', None

                        if dl_resp.status_code in [301, 302, 303, 307, 308]:
                            dl_resp = captcha_session.get(dl_resp.headers.get('Location'), timeout=120, allow_redirects=True)

                        if dl_resp.status_code == 200 and len(dl_resp.content) > 1024:
                            content_type = dl_resp.headers.get('Content-Type', '')
                            if dl_resp.content[:4] == b'%PDF' or 'pdf' in content_type.lower():
                                return 'success', dl_resp.content
                            elif 'html' in content_type.lower():
                                logger.info(f"下载返回HTML页面，无PDF文件")
                                return 'no_file', None
                            else:
                                logger.warning(f"下载内容类型未知: Content-Type={content_type}")
                        else:
                            logger.warning(f"下载响应异常: status={dl_resp.status_code}, size={len(dl_resp.content)}")
                    else:
                        logger.debug(f"验证码错误: {result.get('msg', '')} (attempt {attempt+1})")
                else:
                    logger.warning(f"验证请求失败: status={validate_resp.status_code}")

                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"验证码处理异常 (attempt {attempt+1}): {e}")
                time.sleep(0.5)

        return 'captcha_failed', None

    def download_pdf(self, pk, standard_no, standard_name):
        if not pk:
            return 'no_pk'

        filename = self._safe_filename(standard_no, standard_name)
        filepath = os.path.join(DOWNLOAD_DIR, filename)

        if os.path.exists(filepath) and os.path.getsize(filepath) > 1024:
            logger.info(f"文件已存在: {filename}")
            self.db.update_local_file(standard_no, filename)
            return 'success'

        is_public, _ = self._check_public(pk)
        if not is_public:
            logger.info(f"标准未公开: {standard_no}")
            self.db.mark_download_status(standard_no, pk, 'not_public')
            return 'not_public'

        status, pdf_content = self._recognize_captcha_and_download(pk)

        if status == 'success' and pdf_content:
            with open(filepath, 'wb') as f:
                f.write(pdf_content)
            if os.path.exists(filepath) and os.path.getsize(filepath) > 1024:
                logger.info(f"下载成功: {filename} ({os.path.getsize(filepath)} bytes)")
                self.db.update_local_file(standard_no, filename)
                return 'success'
            else:
                if os.path.exists(filepath):
                    os.remove(filepath)
        elif status == 'no_file':
            self.db.mark_download_status(standard_no, pk, 'no_file')
            return 'no_file'

        return 'failed'

    def download_all(self):
        pending = self.db.get_pending_downloads()
        total = len(pending)
        logger.info(f"待下载行业标准: {total} 个")

        success = 0
        failed = 0
        skipped = 0
        not_public = 0
        no_file = 0

        for i, row in enumerate(pending):
            standard_no = row[0]
            pk = row[1]

            if not pk:
                skipped += 1
                continue

            retries = self.db.get_download_retries(standard_no)
            dl_status = self.db.get_download_status(standard_no)

            if dl_status == 'not_public':
                not_public += 1
                continue

            if retries >= DOWNLOAD_MAX_RETRIES:
                skipped += 1
                continue

            cursor = self.db.conn.cursor()
            cursor.execute('SELECT standard_name FROM hb_standards WHERE standard_no = ?', (standard_no,))
            result = cursor.fetchone()
            standard_name = result[0] if result else standard_no

            logger.info(f"下载进度: {i+1}/{total} - {standard_no} {standard_name[:30]}")

            result = self.download_pdf(pk, standard_no, standard_name)
            if result == 'success':
                success += 1
                self.db.mark_download_status(standard_no, pk, 'success')
            elif result == 'not_public':
                not_public += 1
            elif result == 'no_file':
                no_file += 1
            else:
                failed += 1
                self.db.mark_download_status(standard_no, pk, 'failed', retries + 1)

            if (i + 1) % 50 == 0:
                logger.info(f"=== 进度: {i+1}/{total}, 成功={success}, 失败={failed}, 未公开={not_public}, 无文件={no_file}, 跳过={skipped} ===")

        logger.info(f"下载完成: 成功 {success}, 失败 {failed}, 未公开 {not_public}, 无文件 {no_file}, 跳过 {skipped}")

    def close(self):
        self.db.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description='行业标准爬虫')
    parser.add_argument('--crawl', action='store_true', help='爬取标准列表')
    parser.add_argument('--download', action='store_true', help='下载标准PDF')
    parser.add_argument('--all', action='store_true', help='执行全部操作')
    parser.add_argument('--stats', action='store_true', help='显示统计信息')
    args = parser.parse_args()

    crawler = HBCrawler()
    try:
        if args.stats:
            crawler.db.get_stats()
        elif args.all:
            crawler.crawl_list()
            crawler.download_all()
        elif args.crawl:
            crawler.crawl_list()
        elif args.download:
            crawler.download_all()
        else:
            parser.print_help()
    finally:
        crawler.close()


if __name__ == '__main__':
    main()
