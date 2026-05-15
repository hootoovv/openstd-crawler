#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国家标准爬虫 - 爬取 openstd.samr.gov.cn 上的强制性、推荐性和指导性国家标准
支持断点续爬和PDF下载（含验证码识别，使用ddddocr + cv2）

下载流程:
1. 爬取标准列表，获取 hcno（标准唯一标识）
2. 访问详情页，判断标准是否允许下载/预览
3. 获取验证码图片 (c.gb688.cn/bzgk/gb/gc)
4. 使用 ddddocr + cv2 预处理识别验证码
5. 提交验证码验证 (c.gb688.cn/bzgk/gb/verifyCode)
6. 直接下载PDF (c.gb688.cn/bzgk/gb/viewGb) 或 预览方式重组下载

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

    def _safe_filename(self, standard_no, standard_name):
        safe_name = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', standard_name)
        safe_no = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', standard_no)
        if len(safe_name) > 150:
            safe_name = safe_name[:150]
        return f"{safe_no}-{safe_name}.pdf"

    def _check_detail_page(self, hcno):
        """检查详情页，判断标准是否允许下载/预览

        注意：openstd.samr.gov.cn 详情页需要JS渲染，requests可能无法正确获取页面内容。
        如果详情页返回"尚未收录"，不应直接判定为不可下载，而是返回未知状态，
        由下载流程(c.gb688.cn)来实际判断。

        Returns:
            tuple: (allow_download: bool, allow_preview: bool, standard_not_found: bool)
        """
        try:
            resp = self.session.get(
                DETAIL_URL,
                params={'hcno': hcno},
                timeout=30,
                headers={
                    'Referer': BASE_URL + '/bzgk/std/',
                }
            )
            resp.encoding = 'utf-8'
            if resp.status_code != 200:
                logger.warning(f"详情页请求失败: status={resp.status_code}, hcno={hcno}")
                # 无法确认，返回全部允许，由下载流程判断
                return True, True, False

            html = resp.text

            # 标准未收录 - 但由于requests无法获取JS渲染后的页面，
            # 该判断可能不可靠。仍返回not_found让下载流程去验证。
            if '您所查询的标准系统尚未收录' in html:
                # 检查页面上是否有按钮区域（说明JS未渲染但标准可能存在）
                # 如果页面确实没有标准信息，才返回not_found
                if 'xz_btn' not in html and 'ck_btn' not in html:
                    # 页面中没有下载/预览按钮的原始HTML，可能标准确实未收录
                    # 但也可能是因为JS未执行。保守起见，仍尝试下载
                    logger.debug(f"详情页显示未收录(可能是JS未渲染): hcno={hcno}")
                    # 不返回not_found，让下载流程去c.gb688.cn验证
                    return True, True, False

            soup = BeautifulSoup(html, 'lxml')

            # 检查下载按钮
            xz_btn = soup.select_one('button.xz_btn')
            allow_download = xz_btn is not None

            # 检查预览按钮
            ck_btn = soup.select_one('button.ck_btn')
            allow_preview = ck_btn is not None

            # 如果都无法检测到，默认都允许尝试（由下载服务器判断）
            if not allow_download and not allow_preview:
                logger.debug(f"详情页未检测到按钮(可能是JS未渲染): hcno={hcno}")
                return True, True, False

            return allow_download, allow_preview, False

        except Exception as e:
            logger.warning(f"检查详情页失败: hcno={hcno}, error={e}")
            # 出错时保守处理，允许尝试下载
            return True, True, False

    def _solve_captcha(self):
        """解决验证码：获取验证码图片 -> OCR识别 -> 提交验证
        
        验证码和下载必须在同一HTTP会话中完成，因此使用 _dl_session
        
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
                        'Referer': f'{SHOW_GB_URL}?type=download&hcno=',
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
        
        Returns:
            list[dict]: 页面结构列表，每个元素包含:
                - no: 页码
                - img_id: 图片资源ID
                - w: 页面宽度
                - h: 页面高度
                - blocks: 图块列表 [{x, y, img_x, img_y}, ...]
        """
        soup = BeautifulSoup(html_text, 'lxml')
        pages = []

        for page_div in soup.select("div#viewer div.page"):
            blocks = []
            for block_span in page_div.select('span[class^="pdfImg"]'):
                class_parts = block_span['class'][0].split('-')
                _, block_x, block_y = class_parts
                style = block_span.get('style', '')
                match = re.search(r"background-position:\s*-(\d+)px\s+-(\d+)px", style)
                if match:
                    blocks.append({
                        'x': int(block_x),
                        'y': int(block_y),
                        'img_x': int(match.group(1)),
                        'img_y': int(match.group(2)),
                    })

            blocks.sort(key=lambda b: (b['y'], b['x']))

            import urllib.parse
            style = page_div.get('style', '')
            w_match = re.search(r'width:\s*(\d+)px', style)
            h_match = re.search(r'height:\s*(\d+)px', style)

            pages.append({
                'no': int(page_div.get('id', 0)),
                'img_id': urllib.parse.unquote(page_div.get('bg', '')),
                'w': int(w_match.group(1)) if w_match else 0,
                'h': int(h_match.group(1)) if h_match else 0,
                'blocks': blocks,
            })

        return pages

    def _download_preview_image(self, img_id):
        """下载预览图片资源
        
        Returns:
            bytes: 图片数据（WebP格式）
        """
        dl_session = self._get_dl_session()
        resp = dl_session.get(
            VIEW_GB_IMG_URL,
            params={'fileName': img_id},
            headers={
                'Cache-Alive': 'chunked',
                'Referer': f'{SHOW_GB_URL}?type=online',
            },
            timeout=60,
            allow_redirects=True,
        )
        resp.raise_for_status()
        return resp.content

    def _reorganize_page(self, page_info, raw_img_path, output_img_path):
        """重组页面：将打乱的图块重新拼接为完整页面
        
        预览页面将每一页分成10x10的网格，图块被随机打乱排列。
        需要根据每个图块的(x,y)位置和(img_x,img_y)偏移重新拼接。
        """
        import cv2
        import numpy as np

        # 读取原始图片（含透明通道）
        raw_img = cv2.imread(str(raw_img_path), cv2.IMREAD_UNCHANGED)
        if raw_img is None:
            logger.error(f"无法读取图片: {raw_img_path}")
            return False

        # 透明图层加白背景
        if raw_img.shape[2] == 4:
            bg = np.full((*raw_img.shape[:2], 3), 255, np.uint8)
            alpha = raw_img[:, :, 3].astype(float) / 255.0
            alpha = np.expand_dims(alpha, axis=-1)
            raw_img = (raw_img[:, :, :3] * alpha + bg * (1 - alpha)).astype(np.uint8)

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
        2. 下载所有原始图片资源（viewGbImg）
        3. 重组页面（图块拼合）
        4. 生成PDF
        
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

            page_infos = self._parse_preview_pages(resp.text)
            if not page_infos:
                logger.warning(f"预览页面解析失败: hcno={hcno}")
                return False

            logger.debug(f"预览页面解析成功: {len(page_infos)} 页")

            # 收集唯一的图片ID
            img_ids = set(p['img_id'] for p in page_infos if p['img_id'])

            # 2. 使用临时目录下载图片和重组
            with tempfile.TemporaryDirectory(prefix='gb_preview_') as tmp_dir:
                # 下载所有原始图片
                for img_id in img_ids:
                    img_data = self._download_preview_image(img_id)
                    img_filename = img_id.replace('/', '_')
                    raw_file = os.path.join(tmp_dir, f"{img_filename}.webp")
                    with open(raw_file, 'wb') as f:
                        f.write(img_data)

                # 3. 重组每一页
                for page_info in page_infos:
                    img_filename = page_info['img_id'].replace('/', '_')
                    raw_file = os.path.join(tmp_dir, f"{img_filename}.webp")
                    page_file = os.path.join(tmp_dir, f"P_{page_info['no']}.png")
                    self._reorganize_page(page_info, raw_file, page_file)

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

    def download_pdf(self, hcno, standard_no, standard_name):
        """下载国标PDF文件
        
        下载流程：
        1. 检查详情页，判断标准是否允许下载/预览
        2. 解决验证码（同一会话）
        3. 优先直接下载(viewGb)，不允许下载则尝试预览方式
        
        Returns:
            str: 下载结果 ('success', 'not_found', 'not_downloadable', 'captcha_failed', 'failed')
        """
        filename = self._safe_filename(standard_no, standard_name)
        filepath = os.path.join(DOWNLOAD_DIR, filename)

        # 检查本地是否已下载
        if os.path.exists(filepath) and os.path.getsize(filepath) > 1024:
            logger.debug(f"文件已存在: {filename}")
            self.db.update_local_file(standard_no, filename)
            return 'success'

        # 1. 检查详情页
        allow_download, allow_preview, not_found = self._check_detail_page(hcno)

        if not_found:
            logger.info(f"标准未收录: {standard_no}")
            self.db.update_allow_flags(standard_no, 0, 0)
            return 'not_found'

        # 更新数据库中的下载/预览标志
        self.db.update_allow_flags(standard_no, int(allow_download), int(allow_preview))

        if not allow_download and not allow_preview:
            logger.info(f"标准不可下载也不可预览: {standard_no}")
            return 'not_downloadable'

        # 2. 解决验证码（每次下载重置会话，确保验证码状态干净）
        self._reset_dl_session()
        if not self._solve_captcha():
            logger.warning(f"验证码验证失败: {standard_no}")
            return 'captcha_failed'

        # 3. 尝试下载
        if allow_download:
            # 优先直接下载
            if self._download_pdf_direct(hcno, filepath):
                logger.info(f"下载成功(直接): {filename}")
                self.db.update_local_file(standard_no, filename)
                return 'success'

        if allow_preview:
            # 预览方式下载
            # 如果直接下载失败，可能需要重新解决验证码
            if not allow_download:
                # 之前没尝试直接下载，验证码应该还有效
                pass
            else:
                # 直接下载失败了，可能需要重新验证
                self._reset_dl_session()
                if not self._solve_captcha():
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

    def download_all(self):
        pending = self.db.get_pending_downloads()
        total = len(pending)
        logger.info(f"待下载标准: {total} 个")

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
        not_found = 0
        not_downloadable = 0
        captcha_failed = 0
        consecutive_fails = 0
        interrupted = False

        for i, row in enumerate(pending):
            if self._interrupted:
                interrupted = True
                break

            standard_no = row[0]
            hcno = row[1]

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

            logger.info(f"下载进度: {i+1}/{total} - {standard_no} {standard_name[:30]}")

            try:
                result = self.download_pdf(hcno, standard_no, standard_name)
            except CrawlInterrupted:
                logger.info("用户中断下载，已保存进度")
                interrupted = True
                break

            if result == 'success':
                success += 1
                consecutive_fails = 0
                self.db.mark_download_status(standard_no, hcno, 'success')
            elif result == 'not_found':
                not_found += 1
                consecutive_fails = 0
                self.db.mark_download_status(standard_no, hcno, 'not_found')
            elif result == 'not_downloadable':
                not_downloadable += 1
                consecutive_fails = 0
                self.db.mark_download_status(standard_no, hcno, 'not_downloadable')
            elif result == 'captcha_failed':
                captcha_failed += 1
                consecutive_fails += 1
                self.db.mark_download_status(standard_no, hcno, 'captcha_failed', retries + 1)
            else:
                failed += 1
                consecutive_fails += 1
                self.db.mark_download_status(standard_no, hcno, 'failed', retries + 1)

            if (i + 1) % 50 == 0:
                logger.info(f"=== 进度: {i+1}/{total}, 成功={success}, 失败={failed}, "
                            f"未收录={not_found}, 不可下载={not_downloadable}, "
                            f"验证码失败={captcha_failed}, 跳过={skipped} ===")

            # 每次下载后适当延时
            time.sleep(random.uniform(0.5, 1.5))

        if interrupted and consecutive_fails >= CONSECUTIVE_FAIL_LIMIT:
            logger.info(f"下载已终止(连续失败): 成功 {success}, 失败 {failed}, "
                        f"未收录 {not_found}, 不可下载 {not_downloadable}, 跳过 {skipped}")
        elif interrupted:
            logger.info(f"下载已中断: 成功 {success}, 失败 {failed}, "
                        f"未收录 {not_found}, 不可下载 {not_downloadable}, 跳过 {skipped}")
        else:
            logger.info(f"下载完成: 成功 {success}, 失败 {failed}, "
                        f"未收录 {not_found}, 不可下载 {not_downloadable}, 跳过 {skipped}")

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
