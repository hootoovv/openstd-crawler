#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF 转 Markdown 工具 - 使用 MinerU 将已下载的标准 PDF 转换为 Markdown 格式

功能:
  - 扫描 download/gb_standards/ 和 download/hb_standards/ 目录下的 PDF 文件
  - 使用 MinerU (pipeline 后端) 将 PDF 转换为 Markdown
  - 输出目录结构: md/gb/<文件名>/<文件名>.md + images/
  - 支持键盘中断 (Ctrl+C) 优雅停止
  - 支持断点续转：已成功转换的文件不会重复处理
  - 转换进度记录在 SQLite 数据库中

用法:
  python pdf_to_md.py gb                # 转换国标 PDF
  python pdf_to_md.py hb                # 转换行标 PDF
  python pdf_to_md.py all               # 转换所有标准 PDF
  python pdf_to_md.py gb --force        # 强制重新转换（忽略已完成记录）
  python pdf_to_md.py stats             # 显示转换统计
"""

import os
import re
import sys
import json
import shutil
import sqlite3
import logging
import subprocess
import time
from pathlib import Path
from datetime import datetime

# ========== 配置 ==========
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, 'standards.db')

# PDF 源目录
GB_PDF_DIR = os.path.join(BASE_DIR, 'download', 'gb_standards')
HB_PDF_DIR = os.path.join(BASE_DIR, 'download', 'hb_standards')

# Markdown 输出目录
GB_MD_DIR = os.path.join(BASE_DIR, 'md', 'gb')
HB_MD_DIR = os.path.join(BASE_DIR, 'md', 'hb')

# MinerU 临时输出目录
TEMP_OUTPUT_DIR = os.path.join(BASE_DIR, 'md', '_mineru_temp')

# 日志配置
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(LOG_DIR, 'pdf_to_md.log'), encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# 标准类型映射
STD_TYPES = {
    'gb': {'name': '国家标准', 'pdf_dir': GB_PDF_DIR, 'md_dir': GB_MD_DIR},
    'hb': {'name': '行业标准', 'pdf_dir': HB_PDF_DIR, 'md_dir': HB_MD_DIR},
}


class ConvertInterrupted(Exception):
    """用户中断转换时抛出的异常"""
    pass


class ConvertDatabase:
    """转换进度数据库操作类"""

    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pdf_convert_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                std_type TEXT NOT NULL,
                pdf_filename TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                output_dir TEXT DEFAULT '',
                error_msg TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(std_type, pdf_filename)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_convert_type ON pdf_convert_progress(std_type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_convert_status ON pdf_convert_progress(status)')
        self.conn.commit()

    def is_converted(self, std_type, pdf_filename):
        """检查文件是否已成功转换"""
        cursor = self.conn.cursor()
        cursor.execute(
            'SELECT status FROM pdf_convert_progress WHERE std_type = ? AND pdf_filename = ?',
            (std_type, pdf_filename)
        )
        row = cursor.fetchone()
        return row is not None and row[0] == 'success'

    def mark_status(self, std_type, pdf_filename, status, output_dir='', error_msg=''):
        """更新转换状态"""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO pdf_convert_progress (std_type, pdf_filename, status, output_dir, error_msg, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(std_type, pdf_filename) DO UPDATE SET
                status=excluded.status,
                output_dir=excluded.output_dir,
                error_msg=excluded.error_msg,
                updated_at=CURRENT_TIMESTAMP
        ''', (std_type, pdf_filename, status, output_dir, error_msg))
        self.conn.commit()

    def get_stats(self, std_type=None):
        """获取转换统计"""
        cursor = self.conn.cursor()
        if std_type:
            cursor.execute(
                'SELECT status, COUNT(*) FROM pdf_convert_progress WHERE std_type = ? GROUP BY status',
                (std_type,)
            )
        else:
            cursor.execute('SELECT status, COUNT(*) FROM pdf_convert_progress GROUP BY status')
        return dict(cursor.fetchall())

    def get_failed(self, std_type=None):
        """获取转换失败的文件列表"""
        cursor = self.conn.cursor()
        if std_type:
            cursor.execute(
                'SELECT pdf_filename, error_msg FROM pdf_convert_progress WHERE std_type = ? AND status = ?',
                (std_type, 'failed')
            )
        else:
            cursor.execute(
                'SELECT pdf_filename, error_msg FROM pdf_convert_progress WHERE status = ?',
                ('failed',)
            )
        return cursor.fetchall()

    def close(self):
        self.conn.close()


class PDFToMDConverter:
    """PDF 转 Markdown 转换器"""

    def __init__(self, force=False):
        self.db = ConvertDatabase(DB_PATH)
        self.force = force
        self._interrupted = False
        # 检查 MinerU 是否可用
        self._check_mineru()

    def _check_mineru(self):
        """检查 MinerU 是否已安装"""
        try:
            from mineru.cli.common import do_parse, read_fn  # noqa: F401
            logger.debug("MinerU Python API 可用")
        except ImportError:
            logger.error("MinerU 未安装，请运行: pip install mineru")
            raise

    def request_stop(self):
        """请求优雅停止"""
        self._interrupted = True
        logger.info("已收到停止请求，等待当前转换完成...")

    def _delay(self):
        if self._interrupted:
            raise ConvertInterrupted()

    def _scan_pdf_files(self, pdf_dir):
        """扫描目录下的所有 PDF 文件

        Returns:
            list[str]: PDF 文件名列表（不含路径）
        """
        if not os.path.isdir(pdf_dir):
            logger.warning(f"PDF 目录不存在: {pdf_dir}")
            return []

        pdf_files = []
        for f in os.listdir(pdf_dir):
            if f.lower().endswith('.pdf'):
                # 检查文件大小，跳过空文件
                filepath = os.path.join(pdf_dir, f)
                if os.path.getsize(filepath) > 1024:
                    pdf_files.append(f)
                else:
                    logger.debug(f"跳过过小文件: {f}")

        pdf_files.sort()
        return pdf_files

    def _convert_single(self, pdf_path, output_base_dir, std_type, pdf_filename):
        """转换单个 PDF 文件为 Markdown

        流程：
        1. 调用 MinerU 的 Python API 将 PDF 转换到临时目录
        2. 将结果重组到目标目录结构：<output_base_dir>/<stem>/<stem>.md + images/
        3. 清理临时文件

        Args:
            pdf_path: PDF 文件完整路径
            output_base_dir: 输出根目录 (md/gb 或 md/hb)
            std_type: 标准类型 ('gb' 或 'hb')
            pdf_filename: PDF 文件名

        Returns:
            bool: 是否转换成功
        """
        from mineru.cli.common import do_parse, read_fn

        stem = Path(pdf_filename).stem
        target_dir = os.path.join(output_base_dir, stem)
        target_md = os.path.join(target_dir, f"{stem}.md")
        target_images = os.path.join(target_dir, "images")

        # 如果目标文件已存在且非强制模式，跳过
        if not self.force and os.path.exists(target_md) and os.path.getsize(target_md) > 0:
            logger.debug(f"目标 MD 已存在，跳过: {stem}")
            self.db.mark_status(std_type, pdf_filename, 'success', target_dir)
            return True

        try:
            # 确保输出目录存在
            os.makedirs(target_dir, exist_ok=True)

            # 1. 读取 PDF 文件
            pdf_bytes = read_fn(Path(pdf_path))

            # 2. 调用 MinerU 转换（输出到临时目录）
            os.makedirs(TEMP_OUTPUT_DIR, exist_ok=True)

            do_parse(
                output_dir=TEMP_OUTPUT_DIR,
                pdf_file_names=[stem],
                pdf_bytes_list=[pdf_bytes],
                p_lang_list=["ch"],            # 中文标准
                backend="pipeline",             # 使用 pipeline 后端（无需 GPU）
                parse_method="auto",            # 自动选择 txt/ocr
                formula_enable=True,            # 启用公式解析
                table_enable=True,              # 启用表格解析
                f_dump_md=True,                 # 输出 MD
                f_dump_middle_json=False,       # 不输出中间 JSON
                f_dump_model_output=False,      # 不输出模型输出
                f_dump_content_list=False,      # 不输出内容列表
                f_dump_orig_pdf=False,          # 不复制原始 PDF
                f_draw_layout_bbox=False,       # 不绘制布局框
                f_draw_span_bbox=False,         # 不绘制 span 框
            )

            # 3. 定位 MinerU 输出结果
            # MinerU 输出结构: <TEMP_OUTPUT_DIR>/<stem>/auto/<stem>.md + images/
            mineru_md_dir = os.path.join(TEMP_OUTPUT_DIR, stem, "auto")
            mineru_md_file = os.path.join(mineru_md_dir, f"{stem}.md")
            mineru_images_dir = os.path.join(mineru_md_dir, "images")

            if not os.path.exists(mineru_md_file):
                # 尝试其他方法子目录
                for method in ["txt", "ocr"]:
                    alt_md = os.path.join(TEMP_OUTPUT_DIR, stem, method, f"{stem}.md")
                    if os.path.exists(alt_md):
                        mineru_md_dir = os.path.join(TEMP_OUTPUT_DIR, stem, method)
                        mineru_md_file = alt_md
                        mineru_images_dir = os.path.join(mineru_md_dir, "images")
                        break

            if not os.path.exists(mineru_md_file):
                error_msg = f"MinerU 未生成 MD 文件: {stem}"
                logger.error(error_msg)
                self.db.mark_status(std_type, pdf_filename, 'failed', '', error_msg)
                return False

            # 4. 读取并修正 MD 中的图片路径
            with open(mineru_md_file, 'r', encoding='utf-8') as f:
                md_content = f.read()

            # 修正图片引用路径：MinerU 输出中使用相对路径引用 images/ 下的图片
            # 将 images/xxx 替换为 images/xxx（保持相对路径，因为在同一目录下）
            # MinerU 的 MD 中图片路径格式: ![...](images/xxx.jpg) 或 ![...](./images/xxx.jpg)
            # 这些相对路径在目标目录中仍然有效

            # 5. 写入目标 MD 文件
            with open(target_md, 'w', encoding='utf-8') as f:
                f.write(md_content)

            # 6. 复制 images 目录
            if os.path.exists(mineru_images_dir) and os.path.isdir(mineru_images_dir):
                if os.path.exists(target_images):
                    shutil.rmtree(target_images)
                shutil.copytree(mineru_images_dir, target_images)
                img_count = len(os.listdir(target_images))
                logger.debug(f"复制图片: {img_count} 张 -> {target_images}")
            else:
                # 没有 images 目录也是正常的（纯文字 PDF）
                os.makedirs(target_images, exist_ok=True)

            # 7. 清理 MinerU 临时输出
            temp_stem_dir = os.path.join(TEMP_OUTPUT_DIR, stem)
            if os.path.exists(temp_stem_dir):
                shutil.rmtree(temp_stem_dir)

            # 8. 验证结果
            if os.path.exists(target_md) and os.path.getsize(target_md) > 0:
                self.db.mark_status(std_type, pdf_filename, 'success', target_dir)
                return True
            else:
                error_msg = f"转换结果为空: {stem}"
                logger.error(error_msg)
                self.db.mark_status(std_type, pdf_filename, 'failed', '', error_msg)
                return False

        except Exception as e:
            error_msg = str(e)[:500]
            logger.error(f"转换失败: {stem} - {error_msg}")
            self.db.mark_status(std_type, pdf_filename, 'failed', '', error_msg)

            # 清理临时文件
            temp_stem_dir = os.path.join(TEMP_OUTPUT_DIR, stem)
            if os.path.exists(temp_stem_dir):
                shutil.rmtree(temp_stem_dir)

            return False

    def convert(self, std_type='gb'):
        """批量转换指定类型的标准 PDF

        Args:
            std_type: 'gb' 或 'hb' 或 'all'
        """
        if std_type == 'all':
            types_to_convert = ['gb', 'hb']
        else:
            types_to_convert = [std_type]

        for t in types_to_convert:
            self._convert_type(t)

    def _convert_type(self, std_type):
        """转换单一类型的标准 PDF"""
        if std_type not in STD_TYPES:
            logger.error(f"未知标准类型: {std_type}")
            return

        type_info = STD_TYPES[std_type]
        type_name = type_info['name']
        pdf_dir = type_info['pdf_dir']
        md_dir = type_info['md_dir']

        logger.info(f"===== 开始转换 {type_name} PDF → Markdown =====")

        # 扫描 PDF 文件
        pdf_files = self._scan_pdf_files(pdf_dir)
        total = len(pdf_files)

        if total == 0:
            logger.info(f"未找到 {type_name} PDF 文件 (目录: {pdf_dir})")
            return

        # 统计已转换和待转换
        already_done = 0
        pending_files = []
        for f in pdf_files:
            if not self.force and self.db.is_converted(std_type, f):
                already_done += 1
            else:
                # 也检查目标文件是否已存在（即使数据库没记录）
                stem = Path(f).stem
                target_md = os.path.join(md_dir, stem, f"{stem}.md")
                if not self.force and os.path.exists(target_md) and os.path.getsize(target_md) > 0:
                    already_done += 1
                    self.db.mark_status(std_type, f, 'success', os.path.join(md_dir, stem))
                else:
                    pending_files.append(f)

        logger.info(f"PDF 文件: {total} 个, 已转换: {already_done}, 待转换: {len(pending_files)}")

        if not pending_files:
            logger.info(f"{type_name} 无待转换文件")
            return

        # 确保输出目录存在
        os.makedirs(md_dir, exist_ok=True)

        success = 0
        failed = 0
        skipped = 0

        try:
            for i, pdf_filename in enumerate(pending_files):
                if self._interrupted:
                    logger.info(f"检测到中断信号，停止 {type_name} 转换")
                    break

                pdf_path = os.path.join(pdf_dir, pdf_filename)
                stem = Path(pdf_filename).stem

                logger.info(f"转换进度: {i+1}/{len(pending_files)} - {stem}")

                try:
                    result = self._convert_single(pdf_path, md_dir, std_type, pdf_filename)
                except ConvertInterrupted:
                    logger.info("用户中断转换，已保存进度")
                    break
                except KeyboardInterrupt:
                    self._interrupted = True
                    logger.info("用户中断转换 (Ctrl+C)，已保存进度")
                    break

                if result:
                    success += 1
                else:
                    failed += 1

                if (i + 1) % 20 == 0:
                    logger.info(f"=== 进度: {i+1}/{len(pending_files)}, "
                                f"成功={success}, 失败={failed} ===")

                # 转换间隔（避免资源占用过高）
                time.sleep(0.5)

        except ConvertInterrupted:
            logger.info(f"用户中断 {type_name} 转换，已保存进度")

        logger.info(f"{type_name} 转换完成: 成功 {success}, 失败 {failed}, "
                     f"已跳过(之前完成) {already_done}")

    def show_stats(self):
        """显示转换统计"""
        print("\n===== PDF → Markdown 转换统计 =====")

        for std_type, type_info in STD_TYPES.items():
            type_name = type_info['name']
            pdf_dir = type_info['pdf_dir']
            md_dir = type_info['md_dir']

            # 扫描 PDF 文件数量
            pdf_count = 0
            if os.path.isdir(pdf_dir):
                pdf_count = len([f for f in os.listdir(pdf_dir) if f.lower().endswith('.pdf')])

            # 扫描已生成的 MD 目录数量
            md_count = 0
            if os.path.isdir(md_dir):
                md_count = len([d for d in os.listdir(md_dir)
                                if os.path.isdir(os.path.join(md_dir, d))
                                and d != '_mineru_temp'])

            # 数据库统计
            db_stats = self.db.get_stats(std_type)

            print(f"\n  {type_name}:")
            print(f"    PDF 文件: {pdf_count}")
            print(f"    MD 目录: {md_count}")
            if db_stats:
                for status, count in db_stats.items():
                    print(f"    数据库[{status}]: {count}")

            # 失败列表
            failed = self.db.get_failed(std_type)
            if failed:
                print(f"    失败文件 (最近5个):")
                for row in failed[:5]:
                    error_short = row[1][:80] if row[1] else ''
                    print(f"      {row[0]}: {error_short}")

        # 清理临时目录
        if os.path.exists(TEMP_OUTPUT_DIR):
            try:
                shutil.rmtree(TEMP_OUTPUT_DIR)
                logger.debug("已清理 MinerU 临时目录")
            except Exception:
                pass

    def close(self):
        self.db.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description='PDF 转 Markdown 工具 (MinerU)')
    parser.add_argument('target', nargs='?', default='all',
                        choices=['gb', 'hb', 'all', 'stats'],
                        help='目标: gb=国家标准, hb=行业标准, all=全部, stats=统计信息')
    parser.add_argument('--force', action='store_true',
                        help='强制重新转换（忽略已完成记录）')
    args = parser.parse_args()

    if args.target == 'stats':
        converter = PDFToMDConverter()
        try:
            converter.show_stats()
        finally:
            converter.close()
        return

    converter = PDFToMDConverter(force=args.force)
    try:
        converter.convert(args.target)
        converter.show_stats()
    except KeyboardInterrupt:
        converter.request_stop()
        print("\n")
        print("  收到 Ctrl+C 中断信号")
        print("  已保存所有转换进度")
        print("  下次运行将从断点处继续")
    finally:
        # 清理临时目录
        if os.path.exists(TEMP_OUTPUT_DIR):
            try:
                shutil.rmtree(TEMP_OUTPUT_DIR)
            except Exception:
                pass
        converter.close()


if __name__ == '__main__':
    main()
