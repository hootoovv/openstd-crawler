#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国家标准爬虫 - 爬取 openstd.samr.gov.cn 上的强制性、推荐性和指导性国家标准
支持断点续爬和PDF下载（含验证码识别，使用ddddocr + cv2）

下载流程:
1. 爬取标准列表，获取 hcno（标准唯一标识）
2. 访问详情页，判断标准是否允许下载/预览（可用性检测）
3. 获取验证码图片 (c.gb688.cn/bzgk/gb/gc)
4. 使用 ddddocr + cv2 预处理识别验证码
5. 提交验证码验证 (c.gb688.cn/bzgk/gb/verifyCode)
6. 直接下载PDF (c.gb688.cn/bzgk/gb/viewGb) 或 预览方式重组下载

用法:
  python gb_crawler.py --crawl          # 爬取标准列表
  python gb_crawler.py --check          # 检测标准可用性（下载/预览权限）
  python gb_crawler.py --download       # 下载标准PDF（默认仅下载可下载标准）
  python gb_crawler.py --download --include-preview  # 下载标准PDF（包含仅可预览标准）
  python gb_crawler.py --type 1         # 只爬取强制性国家标准
  python gb_crawler.py --all            # 爬取+检测+下载
  python gb_crawler.py --stats          # 显示统计信息
"""

import os
import re
import sys
import time
import random
import sqlite3
import logging
import tempfile
import requests
from datetime import datetime
from bs4 import BeautifulSoup

# ========== 配置 ==========
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, 'standards.db')
DOWNLOAD_DIR = os.path.join(BASE_DIR, 'download', 'gb_standards')

# 前端页面 (openstd.samr.gov.cn)
BASE_URL = "https://openstd.samr.gov.cn"
LIST_URL = BASE_URL + "/bzgk/std/std_list_type"
DETAIL_URL = BASE_URL + "/bzgk/std/newGbInfo"

# 下载服务器 (c.gb688.cn)
GB688_BASE = "http://c.gb688.cn/bzgk/gb"
CAPTCHA_URL = GB688_BASE + "/gc"
VERIFY_CODE_URL = GB688_BASE + "/verifyCode"
VIEW_GB_URL = GB688_BASE + "/viewGb"
SHOW_GB_URL = GB688_BASE + "/showGb"
VIEW_GB_IMG_URL = GB688_BASE + "/viewGbImg"

# 标准类型映射
STD_TYPES = {
    1: "强制性国家标准",
    2: "推荐性国家标准",
    3: "指导性技术文件"
}

PAGE_SIZE = 50
REQUEST_DELAY = (1, 3)
CHECK_DELAY = (0.5, 1.5)
MAX_RETRIES = 3
CAPTCHA_MAX_RETRIES = 10
DOWNLOAD_MAX_RETRIES = 5
CONSECUTIVE_FAIL_LIMIT = 20  # 连续下载失败超过此数量则终止批次

# ========== 日志配置（必须在其他模块级代码之前初始化） ==========
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

# ========== ddddocr OCR识别器（延迟初始化） ==========
_ocr = None


def get_ocr():
    global _ocr
    if _ocr is None:
        import ddddocr
        _ocr = ddddocr.DdddOcr(show_ad=False)
        logger.info("ddddocr OCR引擎初始化完成")
    return _ocr


def recognize_captcha(captcha_img: bytes) -> str:
    """识别国标下载验证码（带cv2预处理）
    
    处理流程：灰度化 → 二值化(阈值190) → 反色 → ddddocr识别
    """
    import cv2
    import numpy as np

    ocr = get_ocr()

    # 灰度化
    img = cv2.imdecode(np.frombuffer(captcha_img, np.uint8), cv2.IMREAD_GRAYSCALE)
    # 二值化
    _, img = cv2.threshold(img, 190, 255, cv2.THRESH_BINARY)
    # 反色
    img = cv2.bitwise_not(img)
    # 编码为PNG供OCR识别
    _, img_data = cv2.imencode(".png", img)
    code = ocr.classification(img_data.tobytes())

    return code.strip()


class Database:
    """数据库操作类"""

    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()
        self._migrate_tables()

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
                allow_download INTEGER DEFAULT 0,
                allow_preview INTEGER DEFAULT 0,
                allow_checked INTEGER DEFAULT 0,
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

    def _migrate_tables(self):
        """数据库迁移：为已有表添加新列"""
        cursor = self.conn.cursor()
        try:
            cursor.execute('ALTER TABLE gb_standards ADD COLUMN allow_checked INTEGER DEFAULT 0')
            self.conn.commit()
            logger.info("数据库迁移: 添加 allow_checked 列")
        except sqlite3.OperationalError:
            # 列已存在，无需迁移
            pass

    def insert_standard(self, data):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO gb_standards (std_type, standard_no, is_adopted, standard_name, status, publish_date, implement_date, detail_url, hcno, allow_download, allow_preview, local_file)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(standard_no) DO UPDATE SET
                std_type=excluded.std_type,
                is_adopted=excluded.is_adopted,
                standard_name=excluded.standard_name,
                status=excluded.status,
                publish_date=excluded.publish_date,
                implement_date=excluded.implement_date,
                detail_url=excluded.detail_url,
                hcno=excluded.hcno,
                allow_download=COALESCE(excluded.allow_download, allow_download),
                allow_preview=COALESCE(excluded.allow_preview, allow_preview)
        ''', (
            data['std_type'], data['standard_no'], data['is_adopted'],
            data['standard_name'], data['status'], data['publish_date'],
            data['implement_date'], data['detail_url'], data['hcno'],
            data.get('allow_download', 0), data.get('allow_preview', 0),
            data.get('local_file', '')
        ))
        self.conn.commit()

    def update_local_file(self, standard_no, filename):
        cursor = self.conn.cursor()
        cursor.execute('UPDATE gb_standards SET local_file = ? WHERE standard_no = ?', (filename, standard_no))
        self.conn.commit()

    def update_allow_flags(self, standard_no, allow_download, allow_preview):
        cursor = self.conn.cursor()
        cursor.execute('UPDATE gb_standards SET allow_download = ?, allow_preview = ? WHERE standard_no = ?',
                       (allow_download, allow_preview, standard_no))
        self.conn.commit()

    def update_allow_checked(self, standard_no):
        """标记标准已完成可用性检测"""
        cursor = self.conn.cursor()
        cursor.execute('UPDATE gb_standards SET allow_checked = 1 WHERE standard_no = ?', (standard_no,))
        self.conn.commit()

    def get_unchecked_standards(self, std_type=None):
        """获取未检测可用性的标准列表

        Args:
            std_type: 标准类型名称（可选），如 "强制性国家标准"

        Returns:
            list[sqlite3.Row]: allow_checked=0 且 hcno 不为空的标准列表
        """
        cursor = self.conn.cursor()
        if std_type:
            cursor.execute('''
                SELECT standard_no, hcno FROM gb_standards
                WHERE allow_checked = 0 AND hcno != '' AND std_type = ?
            ''', (std_type,))
        else:
            cursor.execute('''
                SELECT standard_no, hcno FROM gb_standards
                WHERE allow_checked = 0 AND hcno != ''
            ''')
        return cursor.fetchall()

    def get_downloadable_standards(self, include_preview=False):
        """获取可下载但尚未下载的标准列表

        Args:
            include_preview: 是否包含仅允许预览的标准。
                False(默认): 仅返回 allow_download=1 的标准
                True: 返回 allow_download=1 或 allow_preview=1 的标准

        Returns:
            list[sqlite3.Row]: 包含 standard_no, hcno, allow_download, allow_preview 字段
        """
        cursor = self.conn.cursor()
        if include_preview:
            cursor.execute('''
                SELECT standard_no, hcno, allow_download, allow_preview FROM gb_standards
                WHERE (allow_download = 1 OR allow_preview = 1)
                  AND (local_file = '' OR local_file IS NULL)
                  AND hcno != ''
            ''')
        else:
            cursor.execute('''
                SELECT standard_no, hcno, allow_download, allow_preview FROM gb_standards
                WHERE allow_download = 1
                  AND (local_file = '' OR local_file IS NULL)
                  AND hcno != ''
            ''')
        return cursor.fetchall()

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

    def get_pending_downloads(self, include_preview=False):
        """获取待下载的标准列表

        Args:
            include_preview: 是否包含仅允许预览的标准
        """
        cursor = self.conn.cursor()
        if include_preview:
            cursor.execute('''
                SELECT standard_no, hcno FROM gb_standards
                WHERE (allow_download = 1 OR allow_preview = 1)
                  AND (local_file = '' OR local_file IS NULL)
                  AND hcno != ''
            ''')
        else:
            cursor.execute('''
                SELECT standard_no, hcno FROM gb_standards
                WHERE allow_download = 1
                  AND (local_file = '' OR local_file IS NULL)
                  AND hcno != ''
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
        cursor.execute('SELECT COUNT(*) FROM gb_standards WHERE allow_checked = 1')
        checked = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM gb_standards WHERE allow_download = 1')
        downloadable = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM gb_standards WHERE allow_preview = 1')
        previewable = cursor.fetchone()[0]
        logger.info(f"  总计: {total} 条, 已下载 {downloaded}, 已检测 {checked}, "
                     f"可下载 {downloadable}, 可预览 {previewable}")
        for code in [1, 2, 3]:
            cursor.execute('SELECT COUNT(*) FROM gb_standards WHERE std_type = ?', (STD_TYPES[code],))
            count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM gb_standards WHERE std_type = ? AND local_file != '' AND local_file IS NOT NULL", (STD_TYPES[code],))
            dl = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM gb_standards WHERE std_type = ? AND allow_download = 1', (STD_TYPES[code],))
            dl_count = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM gb_standards WHERE std_type = ? AND allow_preview = 1', (STD_TYPES[code],))
            pv_count = cursor.fetchone()[0]
            logger.info(f"  {STD_TYPES[code]}: {count} 条, 已下载 {dl}, 可下载 {dl_count}, 可预览 {pv_count}")
        return total, downloaded

    def close(self):
        self.conn.close()


class CrawlInterrupted(Exception):
    """用户中断爬虫时抛出的异常"""
    pass


class GBCrawler:
    """国家标准爬虫"""

    def __init__(self):
        self.db = Database(DB_PATH)
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        # 列表页session (openstd.samr.gov.cn)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        })
        self._init_session()
        self._interrupted = False
        # 下载服务器session (c.gb688.cn) - 需要维护验证码会话
        self._dl_session = None

    def _init_session(self):
        try:
            resp = self.session.get(BASE_URL + "/bzgk/std/", timeout=30)
            logger.info(f"Session初始化: status={resp.status_code}")
        except Exception as e:
            logger.warning(f"Session初始化失败: {e}")

    def _get_dl_session(self):
        """获取或创建下载服务器的HTTP会话（验证码和下载必须在同一会话中）"""
        if self._dl_session is None:
            self._dl_session = requests.Session()
            self._dl_session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 Edg/133.0.0.0',
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
                'Accept': '*/*',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
            })
            logger.info("创建c.gb688.cn下载会话")
        return self._dl_session

    def _reset_dl_session(self):
        """重置下载会话（验证码验证失败时重新开始）"""
        self._dl_session = None

    def _delay(self):
        if self._interrupted:
            raise CrawlInterrupted()
        time.sleep(random.uniform(*REQUEST_DELAY))

    def _check_delay(self):
        """可用性检测的延迟（比爬取延迟短）"""
        if self._interrupted:
            raise CrawlInterrupted()
        time.sleep(random.uniform(*CHECK_DELAY))

    # ==================== 列表爬取 ====================

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

            try:
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
                    if self._interrupted:
                        logger.info(f"检测到中断信号，停止爬取 {type_name}")
                        break
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

            except CrawlInterrupted:
                logger.info(f"用户中断爬取 {type_name}，已保存进度")
                break

            if not self._interrupted:
                logger.info(f"===== {type_name} 爬取完成 =====")

        total, downloaded = self.db.get_stats()
        logger.info(f"数据库中共有 {total} 条国家标准记录, 已下载 {downloaded} 个文件")

    # ==================== 可用性检测 ====================

    def _check_detail_availability(self, hcno):
        """检测单个标准的详情页可用性（下载/预览权限）

        通过访问 openstd.samr.gov.cn 的详情页，检查页面上的按钮来判断：
        - class='ck_btn' 的 button: 在线预览按钮
        - class='xz_btn' 的 button: 下载按钮

        注意：不能使用 '系统尚未收录' 字符串匹配，因为该文本出现在每个页面的
        JavaScript i18n 对象中，无论标准是否被收录都会存在。

        Args:
            hcno: 标准唯一标识哈希

        Returns:
            tuple[int, int]: (allow_download, allow_preview) 1=允许, 0=不允许
        """
        try:
            resp = self.session.get(
                DETAIL_URL,
                params={'hcno': hcno},
                timeout=30,
            )
            if resp.status_code != 200:
                logger.debug(f"详情页请求失败: hcno={hcno}, status={resp.status_code}")
                return 0, 0

            html = resp.text
            soup = BeautifulSoup(html, 'lxml')

            # 检查预览按钮
            ck_btn = soup.find('button', class_='ck_btn')
            allow_preview = 1 if ck_btn else 0

            # 检查下载按钮
            xz_btn = soup.find('button', class_='xz_btn')
            allow_download = 1 if xz_btn else 0

            return allow_download, allow_preview

        except Exception as e:
            logger.warning(f"检测可用性异常: hcno={hcno}, {e}")
            return 0, 0

    def check_availability(self, std_type_code=None):
        """批量检测标准的可用性（下载/预览权限）

        从数据库中获取未检测的标准，逐一访问详情页判断权限，
        并更新数据库中的 allow_download, allow_preview, allow_checked 字段。

        Args:
            std_type_code: 标准类型代码（可选），1=强制性, 2=推荐性, 3=指导性
        """
        if std_type_code:
            std_type = STD_TYPES.get(std_type_code, '')
            unchecked = self.db.get_unchecked_standards(std_type=std_type)
        else:
            unchecked = self.db.get_unchecked_standards()

        total = len(unchecked)
        logger.info(f"待检测可用性标准: {total} 个")

        if total == 0:
            logger.info("没有待检测的标准")
            return

        checked = 0
        dl_count = 0
        pv_count = 0

        try:
            for i, row in enumerate(unchecked):
                if self._interrupted:
                    logger.info("检测到中断信号，停止可用性检测")
                    break

                standard_no = row[0]
                hcno = row[1]

                self._check_delay()

                allow_download, allow_preview = self._check_detail_availability(hcno)

                # 更新数据库
                self.db.update_allow_flags(standard_no, allow_download, allow_preview)
                self.db.update_allow_checked(standard_no)

                checked += 1
                if allow_download:
                    dl_count += 1
                if allow_preview:
                    pv_count += 1

                if (i + 1) % 100 == 0:
                    logger.info(f"检测进度: {i+1}/{total}, 可下载={dl_count}, 可预览={pv_count}")

                logger.debug(f"检测: {standard_no} -> 下载={'是' if allow_download else '否'}, "
                             f"预览={'是' if allow_preview else '否'}")

        except CrawlInterrupted:
            logger.info(f"用户中断可用性检测，已检测 {checked}/{total}")

        except KeyboardInterrupt:
            self._interrupted = True
            logger.info(f"用户中断可用性检测 (Ctrl+C)，已检测 {checked}/{total}")

        logger.info(f"可用性检测完成: 共检测 {checked} 个, 可下载 {dl_count}, 可预览 {pv_count}")

    # ==================== 下载相关 ====================

    def _safe_filename(self, standard_no, standard_name):
        safe_name = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', standard_name)
        safe_no = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', standard_no)
        # 文件系统单文件名限制255字节，中文占3字节，需按字节截断
        # 预留 safe_no + '-' + '.pdf' 的字节数
        prefix = f"{safe_no}-"
        suffix = ".pdf"
        max_name_bytes = 255 - len(prefix.encode('utf-8')) - len(suffix.encode('utf-8'))
        name_bytes = safe_name.encode('utf-8')
        if len(name_bytes) > max_name_bytes:
            # 按字节截断，避免截断在多字节字符中间
            safe_name = name_bytes[:max_name_bytes].decode('utf-8', errors='ignore')
        return f"{prefix}{safe_name}{suffix}"


    def _init_download_session(self):
        """初始化下载会话：建立与 c.gb688.cn 的 HTTP 会话

        c.gb688.cn 需要先建立会话获取 JSESSIONID Cookie。
        访问 /bzgk/gb/index 端点即可触发服务器设置 Cookie。
        如果 index 端点不可用，则回退到验证码端点。
        requests.Session 会自动管理 Cookie，无需手动提取。
        """
        dl_session = self._get_dl_session()
        try:
            resp = dl_session.get(
                GB688_BASE + '/index',
                timeout=15,
                allow_redirects=True,
            )
            logger.debug(f"下载会话初始化: status={resp.status_code}")
        except Exception as e:
            logger.debug(f"下载会话初始化(index)失败: {e}, 尝试验证码端点")
            try:
                resp = dl_session.get(
                    f"{CAPTCHA_URL}?_{int(time.time() * 1000)}",
                    timeout=15,
                )
                logger.debug(f"下载会话初始化(captcha): status={resp.status_code}")
            except Exception as e2:
                logger.warning(f"下载会话初始化失败: {e2}")

    def _solve_captcha(self, hcno='', download_type='download'):
        """解决验证码：获取验证码图片 -> OCR识别 -> 提交验证

        验证码和下载必须在同一HTTP会话中完成，因此使用 _dl_session

        Args:
            hcno: 标准唯一标识哈希（用于构造正确的Referer）
            download_type: 下载类型 ('download' 或 'online')

        Returns:
            bool: 验证码是否通过
        """
        dl_session = self._get_dl_session()

        for attempt in range(CAPTCHA_MAX_RETRIES):
            try:
                # 1. 获取验证码图片
                captcha_url = f"{CAPTCHA_URL}?_{int(time.time() * 1000)}"
                resp = dl_session.get(captcha_url, timeout=15)
                if resp.status_code != 200 or len(resp.content) < 50:
                    logger.warning(f"验证码图片获取失败 (attempt {attempt+1}/{CAPTCHA_MAX_RETRIES})")
                    continue

                # 2. OCR识别
                captcha_code = recognize_captcha(resp.content)
                if len(captcha_code) < 3:
                    logger.warning(f"验证码识别结果过短: '{captcha_code}' (attempt {attempt+1})")
                    continue

                logger.debug(f"验证码识别: '{captcha_code}' (attempt {attempt+1})")

                # 3. 提交验证
                verify_resp = dl_session.post(
                    VERIFY_CODE_URL,
                    data={
                        'verifyCode': captcha_code,
                        'agreeIECTips': 'true',
                    },
                    headers={
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'Referer': f'{SHOW_GB_URL}?type={download_type}&hcno={hcno}',
                        'Origin': 'http://c.gb688.cn',
                    },
                    timeout=15
                )

                if verify_resp.status_code == 200 and verify_resp.text.strip() == 'success':
                    logger.info(f"验证码验证成功 (attempt {attempt+1})")
                    return True
                else:
                    logger.debug(f"验证码验证失败: '{verify_resp.text}' (attempt {attempt+1})")

                time.sleep(0.3)

            except Exception as e:
                logger.warning(f"验证码处理异常 (attempt {attempt+1}): {e}")
                time.sleep(0.3)

        logger.error(f"验证码验证失败，已尝试 {CAPTCHA_MAX_RETRIES} 次")
        return False

    def _download_pdf_direct(self, hcno, filepath):
        """方式一：直接下载PDF（标准允许下载时使用）

        调用 viewGb 接口直接获取PDF文件流
        需要先通过验证码验证（同一会话）

        Returns:
            bool: 是否下载成功
        """
        dl_session = self._get_dl_session()

        try:
            resp = dl_session.get(
                VIEW_GB_URL,
                params={'hcno': hcno},
                timeout=120,
                allow_redirects=True,
                headers={
                    'Referer': f'{DETAIL_URL}?hcno={hcno}',
                }
            )

            if resp.status_code == 200:
                content_type = resp.headers.get('Content-Type', '')
                content_disposition = resp.headers.get('Content-Disposition', '')
                content_length = len(resp.content)

                # 检查是否为PDF
                is_pdf = (
                    resp.content[:4] == b'%PDF'
                    or 'pdf' in content_type.lower()
                    or content_disposition.endswith('.pdf')
                )

                if is_pdf and content_length > 1024:
                    with open(filepath, 'wb') as f:
                        f.write(resp.content)
                    if os.path.exists(filepath) and os.path.getsize(filepath) > 1024:
                        return True
                    else:
                        if os.path.exists(filepath):
                            os.remove(filepath)
                        return False
                else:
                    logger.debug(f"直接下载返回非PDF: Content-Type={content_type}, "
                                 f"Content-Disposition={content_disposition}, size={content_length}")
                    return False
            else:
                logger.warning(f"直接下载失败: status={resp.status_code}")
                return False

        except Exception as e:
            logger.warning(f"直接下载异常: {e}")
            return False

    def _parse_preview_pages(self, html_text):
        """解析预览页面结构，提取页面和图块信息

        预览页面结构:
        - div#viewer > div.page 每页一个div
        - div.page 的 bg 属性包含图片URL: "viewGbImg?fileName=ENCRYPTED%2BString%3D"
        - div.page 的 style 包含 width 和 height
        - 每个 span[class^="pdfImg"] 是一个图块:
          - class="pdfImg-{col}-{row}" 表示图块的目标位置（第col列、第row行）
          - style="background-position: -Xpx -Ypx" 表示图块在源图中的偏移

        Returns:
            list[dict]: 页面结构列表，每个元素包含:
                - no: 页码
                - img_id: 图片文件名（仅fileName参数值，已URL解码）
                - w: 页面宽度
                - h: 页面高度
                - blocks: 图块列表 [{x, y, img_x, img_y}, ...]
        """
        import urllib.parse

        soup = BeautifulSoup(html_text, 'lxml')
        pages = []

        for page_div in soup.select("div#viewer div.page"):
            blocks = []
            for block_span in page_div.select('span[class^="pdfImg"]'):
                class_parts = block_span['class'][0].split('-')
                if len(class_parts) < 3:
                    continue
                _, block_x, block_y = class_parts[0], class_parts[1], class_parts[2]
                style = block_span.get('style', '')
                # background-position 可能为负值: "-360px -169px" 或 "0px 0px"
                match = re.search(r"background-position:\s*(-?\d+)px\s+(-?\d+)px", style)
                if match:
                    blocks.append({
                        'x': int(block_x),
                        'y': int(block_y),
                        'img_x': abs(int(match.group(1))),
                        'img_y': abs(int(match.group(2))),
                    })

            blocks.sort(key=lambda b: (b['y'], b['x']))

            # 从bg属性提取图片文件名
            # bg值形如: "viewGbImg?fileName=LtD10kkUv5fcSz%2B0onLko3ONnBTAH2e4aHP%2F1FxI9bM%3D"
            # 需要提取 fileName 参数的值并URL解码
            bg_raw = page_div.get('bg', '')
            img_file_name = ''
            if bg_raw:
                # 方法1: 从查询参数中提取fileName
                if 'fileName=' in bg_raw:
                    file_name_encoded = bg_raw.split('fileName=', 1)[1].split('&')[0]
                    img_file_name = urllib.parse.unquote(file_name_encoded)
                else:
                    # bg属性可能直接是文件名
                    img_file_name = urllib.parse.unquote(bg_raw)
            else:
                # 方法2: 从第一个span的background-image CSS中提取
                first_span = page_div.find('span')
                if first_span:
                    span_style = first_span.get('style', '')
                    bg_img_match = re.search(r'background-image:\s*url\(["\']?([^"\')]+)["\']?\)', span_style)
                    if bg_img_match:
                        bg_url = bg_img_match.group(1)
                        if 'fileName=' in bg_url:
                            file_name_encoded = bg_url.split('fileName=', 1)[1].split('&')[0]
                            img_file_name = urllib.parse.unquote(file_name_encoded)

            style = page_div.get('style', '')
            w_match = re.search(r'width:\s*(\d+)px', style)
            h_match = re.search(r'height:\s*(\d+)px', style)

            pages.append({
                'no': int(page_div.get('id', 0)),
                'img_id': img_file_name,
                'w': int(w_match.group(1)) if w_match else 0,
                'h': int(h_match.group(1)) if h_match else 0,
                'blocks': blocks,
            })

        return pages

    def _download_preview_image(self, img_id, hcno=''):
        """下载预览图片资源

        img_id 是从 bg 属性中提取的 fileName 参数值（已URL解码），
        requests 库会自动对 params 值进行 URL 编码，无需手动编码。

        Returns:
            bytes: 图片数据（WebP/PNG格式）
        """
        dl_session = self._get_dl_session()
        resp = dl_session.get(
            VIEW_GB_IMG_URL,
            params={'fileName': img_id},
            headers={
                'Cache-Alive': 'chunked',
                'Referer': f'{SHOW_GB_URL}?type=online&hcno={hcno}',
            },
            timeout=60,
            allow_redirects=True,
        )
        resp.raise_for_status()
        return resp.content

    def _reorganize_page(self, page_info, raw_img_data, output_img_path):
        """重组页面：将打乱的图块重新拼接为完整页面

        预览页面将每一页分成10x10的网格，图块被随机打乱排列。
        需要根据每个图块的(x,y)位置和(img_x,img_y)偏移重新拼接。

        Args:
            page_info: 页面结构信息
            raw_img_data: 原始图片二进制数据（WebP/PNG格式）
            output_img_path: 输出页面图片路径
        """
        import cv2
        import numpy as np

        # 使用 cv2.imdecode 从内存中读取图片，避免文件扩展名问题（图片可能是WebP或PNG）
        img_array = np.frombuffer(raw_img_data, np.uint8)
        raw_img = cv2.imdecode(img_array, cv2.IMREAD_UNCHANGED)
        if raw_img is None:
            logger.error(f"无法解码图片数据: size={len(raw_img_data)}")
            return False

        # 透明图层加白背景
        if len(raw_img.shape) == 3 and raw_img.shape[2] == 4:
            bg = np.full((*raw_img.shape[:2], 3), 255, np.uint8)
            alpha = raw_img[:, :, 3].astype(float) / 255.0
            alpha = np.expand_dims(alpha, axis=-1)
            raw_img = (raw_img[:, :, :3] * alpha + bg * (1 - alpha)).astype(np.uint8)
        elif len(raw_img.shape) == 2:
            # 灰度图转RGB
            raw_img = cv2.cvtColor(raw_img, cv2.COLOR_GRAY2RGB)

        page_w = page_info['w']
        page_h = page_info['h']
        block_h = page_h // 10
        block_w = page_w // 10

        # 创建白色页面画布
        page_img = np.full((page_h, page_w, 3), 255, np.uint8)

        for block in page_info['blocks']:
            # 从原始图片裁剪图块
            block_img = raw_img[
                block['img_y']: block['img_y'] + block_h,
                block['img_x']: block['img_x'] + block_w,
            ]
            # 粘贴到页面画布的正确位置
            page_y = block['y'] * block_h
            page_x = block['x'] * block_w
            page_img[page_y: page_y + block_h, page_x: page_x + block_w] = block_img

        cv2.imwrite(str(output_img_path), page_img)
        return True

    def _render_pdf(self, page_infos, base_dir, pdf_path):
        """将重组后的页面图片渲染为PDF"""
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen.canvas import Canvas

        page_cnt = len(page_infos)
        pdf = Canvas(str(pdf_path), pagesize=A4)
        page_w, page_h = A4

        for idx, page_info in enumerate(page_infos):
            img_file = os.path.join(base_dir, f"P_{page_info['no']}.png")
            if not os.path.exists(img_file):
                logger.warning(f"页面图片不存在: {img_file}")
                continue

            img_reader = ImageReader(img_file)
            img_w, img_h = img_reader.getSize()

            # 按比例缩放，适配A4页面
            scale_ratio = min(page_w / img_w, page_h / img_h)
            scaled_w = img_w * scale_ratio
            scaled_h = img_h * scale_ratio

            # 居中
            x_offset = (page_w - scaled_w) / 2
            y_offset = (page_h - scaled_h) / 2

            pdf.drawImage(
                img_reader,
                x_offset, y_offset,
                width=scaled_w, height=scaled_h,
                preserveAspectRatio=True,
            )

            if idx < page_cnt - 1:
                pdf.showPage()

        pdf.save()
        return True

    def _download_pdf_preview(self, hcno, filepath):
        """方式二：预览方式下载PDF（标准仅允许预览时使用）

        流程：
        1. 获取预览页面结构（showGb?type=online）
        2. 下载所有原始图片资源到内存缓存（viewGbImg）
        3. 重组页面（图块拼合，使用内存缓存）
        4. 生成PDF（仅临时目录用于页面PNG和最终PDF）

        Returns:
            bool: 是否下载成功
        """
        dl_session = self._get_dl_session()

        try:
            # 1. 获取预览页面结构
            resp = dl_session.get(
                SHOW_GB_URL,
                params={'type': 'online', 'hcno': hcno},
                headers={
                    'Referer': f'{DETAIL_URL}?hcno={hcno}',
                },
                timeout=30,
            )

            if resp.status_code != 200:
                logger.warning(f"预览页面请求失败: status={resp.status_code}")
                return False

            html = resp.text

            # 诊断日志：检查页面是否包含预览结构
            has_viewer = 'div id="viewer"' in html or "div id='viewer'" in html or 'id="viewer"' in html
            has_page = 'class="page"' in html
            has_pdf_img = 'pdfImg' in html
            logger.debug(f"预览页面诊断: len={len(html)}, has_viewer={has_viewer}, "
                         f"has_page={has_page}, has_pdfImg={has_pdf_img}")

            if not has_viewer and not has_page:
                # 页面可能未正确加载（需要验证码先验证）
                # 或者页面返回了错误信息
                if '尚未收录' in html and 'pdfImg' not in html:
                    logger.warning(f"预览页面显示\"尚未收录\": hcno={hcno}")
                    return False
                if 'verifyCode' in html or 'gc?' in html:
                    logger.warning(f"预览页面需要验证码验证: hcno={hcno}")
                    return False
                # 可能是JS动态加载的页面，无法用requests获取
                logger.warning(f"预览页面缺少viewer结构: hcno={hcno}, html_len={len(html)}")
                # 输出部分HTML以便调试
                logger.debug(f"预览页面前2000字符: {html[:2000]}")
                return False

            page_infos = self._parse_preview_pages(html)
            if not page_infos:
                logger.warning(f"预览页面解析失败（无page元素）: hcno={hcno}")
                return False

            logger.debug(f"预览页面解析成功: {len(page_infos)} 页")

            # 收集唯一的图片ID（多页可能共享同一张图片）
            img_ids = set(p['img_id'] for p in page_infos if p['img_id'])
            logger.debug(f"预览图片: {len(img_ids)} 张唯一图片, {len(page_infos)} 页")

            # 2. 下载所有唯一的图片并缓存到内存（不写临时文件）
            img_cache = {}  # img_id -> raw image bytes
            for img_id in img_ids:
                try:
                    img_data = self._download_preview_image(img_id, hcno)
                    img_cache[img_id] = img_data
                    logger.debug(f"已下载图片: {img_id[:30]}... ({len(img_data)} bytes)")
                except Exception as e:
                    logger.warning(f"图片下载失败: {img_id[:30]}..., {e}")

            if not img_cache:
                logger.error(f"所有图片下载失败: hcno={hcno}")
                return False

            # 3. 使用临时目录重组页面（仅用于页面PNG文件和最终PDF）
            with tempfile.TemporaryDirectory(prefix='gb_preview_') as tmp_dir:
                success_pages = 0
                for page_info in page_infos:
                    img_id = page_info['img_id']
                    if img_id not in img_cache:
                        logger.warning(f"页面图片未下载: 第{page_info['no']}页, {img_id[:30]}...")
                        continue
                    page_file = os.path.join(tmp_dir, f"P_{page_info['no']}.png")
                    if self._reorganize_page(page_info, img_cache[img_id], page_file):
                        success_pages += 1
                    else:
                        logger.warning(f"页面重组失败: 第{page_info['no']}页")

                if success_pages == 0:
                    logger.error(f"所有页面重组失败: hcno={hcno}")
                    return False
                logger.debug(f"页面重组完成: {success_pages}/{len(page_infos)} 页成功")

                # 4. 渲染PDF
                self._render_pdf(page_infos, tmp_dir, filepath)

            if os.path.exists(filepath) and os.path.getsize(filepath) > 1024:
                return True
            else:
                if os.path.exists(filepath):
                    os.remove(filepath)
                return False

        except Exception as e:
            logger.warning(f"预览方式下载异常: {e}")
            if os.path.exists(filepath):
                os.remove(filepath)
            return False

    def _test_download_connectivity(self):
        """测试下载服务器是否可达"""
        logger.info("正在测试下载服务器 c.gb688.cn 连通性...")
        try:
            dl_session = self._get_dl_session()
            resp = dl_session.get(
                CAPTCHA_URL,
                params={f'_{int(time.time() * 1000)}': ''},
                timeout=15,
            )
            logger.info(f"下载服务器响应: status={resp.status_code}, content_length={len(resp.content)}")
            return True
        except requests.ConnectionError as e:
            logger.error(f"无法连接下载服务器 c.gb688.cn: {e}")
            return False
        except requests.Timeout:
            logger.error("连接下载服务器超时: c.gb688.cn")
            return False
        except Exception as e:
            logger.error(f"下载服务器连通性测试异常: {e}")
            return False

    def download_pdf(self, hcno, standard_no, standard_name, allow_download=1, allow_preview=1):
        """下载国标PDF文件

        下载流程：
        1. 根据 allow_download 标志：重置会话 → 初始化会话 → 解决验证码 → 尝试直接下载(viewGb)
        2. 根据 allow_preview 标志：重置会话 → 初始化会话 → 解决验证码 → 尝试预览方式下载(showGb+重组)

        Args:
            hcno: 标准唯一标识哈希
            standard_no: 标准号
            standard_name: 标准名称
            allow_download: 是否允许直接下载（1=允许，0=不允许）
            allow_preview: 是否允许在线预览（1=允许，0=不允许）

        Returns:
            str: 下载结果 ('success', 'captcha_failed', 'failed', 'no_access')
        """
        filename = self._safe_filename(standard_no, standard_name)
        filepath = os.path.join(DOWNLOAD_DIR, filename)

        # 检查本地是否已下载
        if os.path.exists(filepath) and os.path.getsize(filepath) > 1024:
            logger.debug(f"文件已存在: {filename}")
            self.db.update_local_file(standard_no, filename)
            return 'success'

        # 无下载权限也无预览权限，跳过
        if not allow_download and not allow_preview:
            logger.debug(f"标准无下载/预览权限: {standard_no}")
            return 'no_access'

        # 方式一：允许直接下载
        if allow_download:
            # 每次下载重置会话，确保状态干净
            self._reset_dl_session()
            # 初始化会话（获取JSESSIONID）
            self._init_download_session()
            if not self._solve_captcha(hcno, 'download'):
                logger.warning(f"验证码验证失败: {standard_no}")
                return 'captcha_failed'

            if self._download_pdf_direct(hcno, filepath):
                logger.info(f"下载成功(直接): {filename}")
                self.db.update_local_file(standard_no, filename)
                return 'success'
            else:
                logger.debug(f"直接下载失败: {standard_no}")

        # 方式二：预览方式下载（仅允许预览 或 直接下载失败时）
        if allow_preview:
            self._reset_dl_session()
            self._init_download_session()
            if not self._solve_captcha(hcno, 'online'):
                logger.warning(f"预览下载前验证码验证失败: {standard_no}")
                return 'captcha_failed'

            if self._download_pdf_preview(hcno, filepath):
                logger.info(f"下载成功(预览): {filename}")
                self.db.update_local_file(standard_no, filename)
                return 'success'

        # 清理无效文件
        if os.path.exists(filepath):
            if os.path.getsize(filepath) < 1024:
                os.remove(filepath)

        return 'failed'

    def download_all(self, include_preview=False):
        """批量下载所有可下载的标准PDF

        Args:
            include_preview: 是否包含仅允许预览的标准。
                False(默认): 仅下载 allow_download=1 的标准（直接下载）
                True: 也尝试下载仅 allow_preview=1 的标准（预览重组方式）
        """
        pending = self.db.get_downloadable_standards(include_preview=include_preview)
        total = len(pending)

        # 统计下载模式
        dl_only = sum(1 for row in pending if row[2] == 1 and row[3] == 0)
        pv_only = sum(1 for row in pending if row[2] == 0 and row[3] == 1)
        both = sum(1 for row in pending if row[2] == 1 and row[3] == 1)

        logger.info(f"待下载标准: {total} 个 (仅可下载: {dl_only}, 仅可预览: {pv_only}, "
                     f"下载+预览: {both})")
        if not include_preview:
            logger.info("下载模式: 仅可下载标准 (使用 --include-preview 可包含仅可预览标准)")

        if total == 0:
            logger.info("没有待下载的标准")
            return

        # 下载前探测连通性
        if not self._test_download_connectivity():
            logger.error("=" * 60)
            logger.error("  下载服务器不可达，跳过本轮下载")
            logger.error("  目标服务器: c.gb688.cn")
            logger.error("  可能原因:")
            logger.error("    1. 当前网络环境无法访问该服务器 (需要中国大陆IP)")
            logger.error("    2. 服务器暂时不可用")
            logger.error("    3. 需要通过代理访问")
            logger.error("=" * 60)
            # 将所有待下载标记为 unreachable
            for row in pending:
                standard_no = row[0]
                hcno = row[1]
                if hcno:
                    self.db.mark_download_status(standard_no, hcno, 'unreachable')
            return

        success = 0
        failed = 0
        skipped = 0
        captcha_failed = 0
        consecutive_fails = 0
        interrupted = False

        for i, row in enumerate(pending):
            if self._interrupted:
                interrupted = True
                break

            standard_no = row[0]
            hcno = row[1]
            allow_download = row[2]
            allow_preview = row[3]

            if not hcno:
                skipped += 1
                continue

            retries = self.db.get_download_retries(standard_no)
            if retries >= DOWNLOAD_MAX_RETRIES:
                skipped += 1
                continue

            # 连续失败早停
            if consecutive_fails >= CONSECUTIVE_FAIL_LIMIT:
                logger.warning(f"连续 {consecutive_fails} 次下载失败，终止本轮下载")
                logger.warning("可能下载服务器已不可达，请检查网络环境")
                interrupted = True
                break

            cursor = self.db.conn.cursor()
            cursor.execute('SELECT standard_name FROM gb_standards WHERE standard_no = ?', (standard_no,))
            result = cursor.fetchone()
            standard_name = result[0] if result else standard_no

            # 显示下载模式
            mode = '直接下载' if allow_download else '预览下载'
            logger.info(f"下载进度: {i+1}/{total} [{mode}] - {standard_no} {standard_name[:30]}")

            try:
                result = self.download_pdf(
                    hcno, standard_no, standard_name,
                    allow_download=allow_download,
                    allow_preview=allow_preview,
                )
            except CrawlInterrupted:
                logger.info("用户中断下载，已保存进度")
                interrupted = True
                break

            if result == 'success':
                success += 1
                consecutive_fails = 0
                self.db.mark_download_status(standard_no, hcno, 'success')
            elif result == 'captcha_failed':
                captcha_failed += 1
                consecutive_fails += 1
                self.db.mark_download_status(standard_no, hcno, 'captcha_failed', retries + 1)
            elif result == 'no_access':
                skipped += 1
            else:
                failed += 1
                consecutive_fails += 1
                self.db.mark_download_status(standard_no, hcno, 'failed', retries + 1)

            if (i + 1) % 50 == 0:
                logger.info(f"=== 进度: {i+1}/{total}, 成功={success}, 失败={failed}, "
                            f"验证码失败={captcha_failed}, 跳过={skipped} ===")

            # 每次下载后适当延时
            time.sleep(random.uniform(0.5, 1.5))

        if interrupted and consecutive_fails >= CONSECUTIVE_FAIL_LIMIT:
            logger.info(f"下载已终止(连续失败): 成功 {success}, 失败 {failed}, "
                        f"验证码失败 {captcha_failed}, 跳过 {skipped}")
        elif interrupted:
            logger.info(f"下载已中断: 成功 {success}, 失败 {failed}, "
                        f"验证码失败 {captcha_failed}, 跳过 {skipped}")
        else:
            logger.info(f"下载完成: 成功 {success}, 失败 {failed}, "
                        f"验证码失败 {captcha_failed}, 跳过 {skipped}")

    def request_stop(self):
        """请求优雅停止，会在当前任务完成后退出"""
        self._interrupted = True
        logger.info("已收到停止请求，等待当前任务完成...")

    def close(self):
        self.db.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description='国家标准爬虫')
    parser.add_argument('--crawl', action='store_true', help='爬取标准列表')
    parser.add_argument('--check', action='store_true', help='检测标准可用性（下载/预览权限）')
    parser.add_argument('--download', action='store_true', help='下载标准PDF')
    parser.add_argument('--type', type=int, choices=[1, 2, 3], help='标准类型: 1=强制性, 2=推荐性, 3=指导性')
    parser.add_argument('--include-preview', action='store_true',
                        help='下载时包含仅可预览的标准（默认仅下载可直接下载的标准）')
    parser.add_argument('--all', action='store_true', help='执行全部操作（爬取+检测+下载）')
    parser.add_argument('--stats', action='store_true', help='显示统计信息')
    args = parser.parse_args()

    crawler = GBCrawler()
    try:
        if args.stats:
            crawler.db.get_stats()
        elif args.all:
            crawler.crawl_list(args.type)
            crawler.check_availability(args.type)
            crawler.download_all(include_preview=args.include_preview)
        elif args.crawl:
            crawler.crawl_list(args.type)
        elif args.check:
            crawler.check_availability(args.type)
        elif args.download:
            crawler.download_all(include_preview=args.include_preview)
        else:
            parser.print_help()
    except KeyboardInterrupt:
        crawler.request_stop()
        print("\n")
        print("  收到 Ctrl+C 中断信号")
        print("  已保存所有爬取和下载进度")
        print("  下次运行将从断点处继续")
    finally:
        crawler.close()


if __name__ == '__main__':
    main()
