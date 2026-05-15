#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
标准爬虫统一入口脚本

用法:
  python run_crawler.py gb --crawl          # 爬取国家标准列表
  python run_crawler.py gb --crawl --type 1 # 只爬取强制性国家标准
  python run_crawler.py gb --download       # 下载国家标准PDF
  python run_crawler.py gb --all            # 爬取+下载国家标准

  python run_crawler.py hb --crawl          # 爬取行业标准列表
  python run_crawler.py hb --download       # 下载行业标准PDF
  python run_crawler.py hb --all            # 爬取+下载行业标准

  python run_crawler.py all --crawl         # 爬取所有标准列表
  python run_crawler.py all --download      # 下载所有标准PDF
  python run_crawler.py all --all           # 爬取+下载所有标准

  python run_crawler.py stats               # 显示统计信息
"""

import os
import sys

SCRIPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scripts')
sys.path.insert(0, SCRIPT_DIR)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'standards.db')


def _table_exists(cur, table_name):
    """检查表是否存在"""
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    return cur.fetchone() is not None


def show_stats():
    import sqlite3
    if not os.path.exists(DB_PATH):
        print("数据库不存在，请先运行爬虫")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    gb_total = 0
    gb_downloaded = 0
    hb_total = 0
    hb_downloaded = 0

    # 国家标准统计
    print("\n===== 国家标准统计 =====")
    if _table_exists(cur, 'gb_standards'):
        cur.execute('SELECT COUNT(*) FROM gb_standards')
        gb_total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM gb_standards WHERE local_file != '' AND local_file IS NOT NULL")
        gb_downloaded = cur.fetchone()[0]

        types = {'强制性国家标准': 1, '推荐性国家标准': 2, '指导性技术文件': 3}
        for name, code in types.items():
            cur.execute('SELECT COUNT(*) FROM gb_standards WHERE std_type = ?', (name,))
            count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM gb_standards WHERE std_type = ? AND local_file != '' AND local_file IS NOT NULL", (name,))
            dl = cur.fetchone()[0]
            print(f"  {name}: {count} 条, 已下载 {dl}")

        if _table_exists(cur, 'gb_crawl_progress'):
            cur.execute('SELECT std_type_code, COUNT(*) FROM gb_crawl_progress WHERE status = "done" GROUP BY std_type_code')
            for code, pages in cur.fetchall():
                name = {1: '强制性', 2: '推荐性', 3: '指导性'}.get(code, str(code))
                print(f"  {name}已爬取页数: {pages}")

        print(f"  国家标准总计: {gb_total} 条, 已下载: {gb_downloaded}")
    else:
        print("  尚未爬取国家标准数据")

    # 行业标准统计
    print("\n===== 行业标准统计 =====")
    if _table_exists(cur, 'hb_standards'):
        cur.execute('SELECT COUNT(*) FROM hb_standards')
        hb_total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM hb_standards WHERE local_file != '' AND local_file IS NOT NULL")
        hb_downloaded = cur.fetchone()[0]

        cur.execute('SELECT industry, COUNT(*) as cnt FROM hb_standards GROUP BY industry ORDER BY cnt DESC LIMIT 10')
        print("  行业分布(Top 10):")
        for industry, cnt in cur.fetchall():
            print(f"    {industry or '未知'}: {cnt} 条")

        if _table_exists(cur, 'hb_download_progress'):
            cur.execute('SELECT status, COUNT(*) FROM hb_download_progress GROUP BY status')
            print("  下载状态:")
            for status, cnt in cur.fetchall():
                print(f"    {status}: {cnt}")

        if _table_exists(cur, 'hb_crawl_progress'):
            cur.execute('SELECT COUNT(*) FROM hb_crawl_progress WHERE status = "done"')
            hb_pages = cur.fetchone()[0]
            print(f"  已爬取页数: {hb_pages}")

        print(f"  行业标准总计: {hb_total} 条, 已下载: {hb_downloaded}")
    else:
        print("  尚未爬取行业标准数据")

    print(f"\n===== 总计 =====")
    print(f"  标准: {gb_total + hb_total} 条, 已下载PDF: {gb_downloaded + hb_downloaded} 个")

    conn.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description='标准爬虫统一入口')
    parser.add_argument('target', choices=['gb', 'hb', 'all', 'stats'],
                       help='目标: gb=国家标准, hb=行业标准, all=全部, stats=统计信息')
    parser.add_argument('--crawl', action='store_true', help='爬取标准列表')
    parser.add_argument('--download', action='store_true', help='下载标准PDF')
    parser.add_argument('--type', type=int, choices=[1, 2, 3],
                       help='国家标准类型: 1=强制性, 2=推荐性, 3=指导性')
    parser.add_argument('--all', action='store_true', help='执行全部操作（爬取+下载）')
    args = parser.parse_args()

    if args.target == 'stats':
        show_stats()
        return

    do_crawl = args.crawl or args.all
    do_download = args.download or args.all

    if not do_crawl and not do_download:
        print("请指定操作: --crawl, --download 或 --all")
        parser.print_help()
        return

    gb_crawler = None
    hb_crawler = None

    try:
        if args.target in ['gb', 'all']:
            print("\n" + "="*60)
            print("  国家标准爬虫")
            print("="*60)
            from gb_crawler import GBCrawler
            gb_crawler = GBCrawler()
            if do_crawl:
                gb_crawler.crawl_list(args.type)
            if do_download:
                gb_crawler.download_all()

        if args.target in ['hb', 'all']:
            print("\n" + "="*60)
            print("  行业标准爬虫")
            print("="*60)
            from hb_crawler import HBCrawler
            hb_crawler = HBCrawler()
            if do_crawl:
                hb_crawler.crawl_list()
            if do_download:
                hb_crawler.download_all()

        show_stats()

    except KeyboardInterrupt:
        if gb_crawler:
            gb_crawler.request_stop()
        if hb_crawler:
            hb_crawler.request_stop()
        print("\n")
        print("  ⏹  收到 Ctrl+C 中断信号")
        print("  💾 已保存所有爬取和下载进度")
        print("  🔄 下次运行将从断点处继续")
    finally:
        if gb_crawler:
            gb_crawler.close()
        if hb_crawler:
            hb_crawler.close()


if __name__ == '__main__':
    main()
