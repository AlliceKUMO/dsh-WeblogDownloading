# -*- coding: utf-8 -*-
"""
微博收藏下载器 - 图形界面
=========================
- 批量模式：全自动下载全部收藏（开始/暂停/继续/停止），可选"下载后自动取消收藏"
- 手动模式：逐条浏览（正文 + 缩略图预览），可下载此条 / 下载并取消收藏 / 仅取消收藏 / 跳过
- 进度清单：下载记录实时显示，断点续传

运行: python weibo_favorites_gui.py
打包: pyinstaller --noconfirm --onefile --windowed --name 微博收藏下载器 weibo_favorites_gui.py
"""

import argparse
import io
import json
import os
import sys
import threading
from datetime import datetime

import customtkinter as ctk
from PIL import Image

import weibo_favorites_downloader as core

# 程序目录：打包成 exe 后取 exe 所在目录（避免写入临时解压目录）
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
APP_VERSION = "v1.2"
# 默认下载目录放在"我的文档"，与程序本体分离 —— 更新/删除程序不影响下载数据
DEFAULT_DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "Documents", "微博收藏下载")
# 旧版本的默认目录（exe 旁 downloads），用于迁移判断
LEGACY_DEFAULT_DIR = os.path.join(APP_DIR, "downloads")

STATE_TEXT = {"done": "完成", "partial": "部分失败", "failed": "异常", "unfavorited": "已取消收藏"}


def pil_resize(img, width):
    """等比缩放图片到指定宽度。"""
    resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
    ratio = width / float(img.width)
    height = max(1, int(img.height * ratio))
    return img.resize((width, height), resample)


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title("微博收藏下载器 " + APP_VERSION)
        self.geometry("1080x780")
        self.minsize(920, 660)

        # 运行状态
        self.cfg = {}
        self.session = None
        self.base_dir = DEFAULT_DOWNLOAD_DIR
        self.manifest = {}
        self.batch_running = False
        self.batch_stats = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()

        # 手动模式状态
        self.manual_items = []
        self.manual_page_num = 1
        self.manual_index = 0
        self.preview_cache = {}
        self.current_status = None

        core.set_progress_callback(self._on_progress)

        self._install_guards()
        self._build_ui()
        self._load_config()

    # ============================ 防护机制（避免静默失败） ============================

    def _log_file(self, msg):
        """追加写入程序旁的 gui_debug.log（打包版无控制台，靠此文件排查）；超过 2MB 自动截断。"""
        try:
            path = os.path.join(APP_DIR, "gui_debug.log")
            if os.path.exists(path) and os.path.getsize(path) > 2 * 1024 * 1024:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("[%s] 日志已截断\n" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            with open(path, "a", encoding="utf-8") as f:
                f.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
        except Exception:
            pass

    def _start_thread(self, fn, *args):
        """后台线程统一入口：捕获线程内异常并写入日志，避免静默失败。"""
        def wrapper():
            try:
                fn(*args)
            except Exception:
                import traceback
                self._log_file("THREAD ERROR:\n" + traceback.format_exc())
                try:
                    last = traceback.format_exc().strip().splitlines()[-1]
                    self._log("[后台错误] %s" % last)
                except Exception:
                    pass
        threading.Thread(target=wrapper, daemon=True).start()

    def _install_guards(self):
        """覆盖 tkinter 回调异常处理：异常写入日志并在界面提示。"""
        def report(exc, val, tb):
            import traceback
            self._log_file("TK CALLBACK ERROR:\n" + "".join(traceback.format_exception(exc, val, tb)))
            try:
                self._log("[界面错误] %s: %s" % (exc.__name__, val))
            except Exception:
                pass
        try:
            self.report_callback_exception = report
        except Exception:
            pass

    # ============================ 界面搭建 ============================

    def _build_ui(self):
        header = ctk.CTkFrame(self)
        header.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkLabel(header, text="微博收藏下载器",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(side="left", padx=12)
        self.mode_var = ctk.StringVar(value="批量模式")
        seg = ctk.CTkSegmentedButton(header, values=["批量模式", "手动模式"],
                                     variable=self.mode_var, command=self._on_mode_change)
        seg.pack(side="right", padx=12)

        # ---- 设置区 ----
        settings = ctk.CTkFrame(self)
        settings.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(settings, text="Cookie:").grid(row=0, column=0, padx=(12, 4), pady=6, sticky="w")
        self.cookie_entry = ctk.CTkEntry(settings, placeholder_text="粘贴 Cookie（F12 → 网络 → 收藏页请求 → 请求头）")
        self.cookie_entry.grid(row=0, column=1, padx=4, pady=6, sticky="ew")
        ctk.CTkButton(settings, text="保存 Cookie", width=100, command=self._save_cookie).grid(row=0, column=2, padx=4, pady=6)
        ctk.CTkButton(settings, text="测试连接", width=90, command=self._test_cookie).grid(row=0, column=3, padx=4, pady=6)
        settings.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(settings, text="下载目录:").grid(row=1, column=0, padx=(12, 4), pady=6, sticky="w")
        self.dir_entry = ctk.CTkEntry(settings)
        self.dir_entry.grid(row=1, column=1, padx=4, pady=6, sticky="ew")
        ctk.CTkButton(settings, text="浏览", width=100, command=self._browse_dir).grid(row=1, column=2, padx=4, pady=6)
        ctk.CTkButton(settings, text="打开下载目录", width=110, command=self._open_dir).grid(row=1, column=3, padx=4, pady=6)

        ctk.CTkLabel(settings, text="列表接口:").grid(row=2, column=0, padx=(12, 4), pady=6, sticky="w")
        self.api_entry = ctk.CTkEntry(
            settings, placeholder_text="留空使用默认；微博改版后填 F12 看到的实际接口 URL")
        self.api_entry.grid(row=2, column=1, padx=4, pady=6, sticky="ew")
        ctk.CTkButton(settings, text="恢复默认", width=100,
                      command=self._api_reset).grid(row=2, column=2, padx=4, pady=6)

        # ---- 主体（批量页 / 手动页）----
        self.body = ctk.CTkFrame(self)
        self.body.pack(fill="both", expand=True, padx=10, pady=4)
        self._build_batch_page(self.body)
        self._build_manual_page(self.body)
        self.batch_page.pack(fill="both", expand=True)

        # ---- 日志 ----
        ctk.CTkLabel(self, text="运行日志", anchor="w").pack(fill="x", padx=14, pady=(6, 0))
        self.log_box = ctk.CTkTextbox(self, height=140, wrap="word", state="disabled")
        self.log_box.pack(fill="x", padx=10, pady=(0, 4))

        # ---- 状态栏 ----
        self.status_label = ctk.CTkLabel(self, text="就绪", anchor="w", text_color="gray70")
        self.status_label.pack(fill="x", padx=14, pady=(0, 8))

    def _build_batch_page(self, parent):
        self.batch_page = ctk.CTkFrame(parent)

        row = ctk.CTkFrame(self.batch_page)
        row.pack(fill="x", padx=8, pady=6)
        self.btn_batch_start = ctk.CTkButton(row, text="开始下载", width=100, command=self._batch_start)
        self.btn_batch_start.pack(side="left", padx=4)
        self.btn_batch_pause = ctk.CTkButton(row, text="暂停", width=80, command=self._batch_pause, state="disabled")
        self.btn_batch_pause.pack(side="left", padx=4)
        self.btn_batch_stop = ctk.CTkButton(row, text="停止", width=80, command=self._batch_stop, state="disabled")
        self.btn_batch_stop.pack(side="left", padx=4)
        self.auto_unfav_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(row, text="下载成功后自动取消收藏（慎用）",
                        variable=self.auto_unfav_var).pack(side="left", padx=14)
        ctk.CTkLabel(row, text="起始页:").pack(side="left", padx=(10, 2))
        self.start_page_entry = ctk.CTkEntry(row, width=60)
        self.start_page_entry.insert(0, "1")
        self.start_page_entry.pack(side="left", padx=2)

        stats = ctk.CTkFrame(self.batch_page)
        stats.pack(fill="x", padx=8, pady=4)
        self.batch_stat_label = ctk.CTkLabel(stats, text="尚未开始", anchor="w")
        self.batch_stat_label.pack(fill="x", padx=8, pady=4)

        ctk.CTkLabel(self.batch_page, text="已下载清单（状态 / 用户名 / 时间 / 图片）",
                     anchor="w").pack(fill="x", padx=12, pady=(4, 0))
        self.manifest_frame = ctk.CTkScrollableFrame(self.batch_page, height=250)
        self.manifest_frame.pack(fill="both", expand=True, padx=8, pady=4)

    def _build_manual_page(self, parent):
        self.manual_page = ctk.CTkFrame(parent)

        row = ctk.CTkFrame(self.manual_page)
        row.pack(fill="x", padx=8, pady=6)
        self.btn_manual_start = ctk.CTkButton(row, text="开始浏览", width=100, command=self._manual_start)
        self.btn_manual_start.pack(side="left", padx=4)
        self.btn_manual_prev = ctk.CTkButton(row, text="上一页", width=80,
                                             command=self._manual_prev_page, state="disabled")
        self.btn_manual_prev.pack(side="left", padx=4)
        self.btn_manual_next = ctk.CTkButton(row, text="下一页", width=80,
                                             command=self._manual_next_page, state="disabled")
        self.btn_manual_next.pack(side="left", padx=4)
        self.manual_pos_label = ctk.CTkLabel(row, text="未开始浏览", anchor="w")
        self.manual_pos_label.pack(side="left", padx=14)

        info = ctk.CTkFrame(self.manual_page)
        info.pack(fill="x", padx=8, pady=4)
        self.manual_meta_label = ctk.CTkLabel(info, text="", anchor="w", justify="left", wraplength=920)
        self.manual_meta_label.pack(fill="x", padx=10, pady=(6, 2))
        self.manual_text = ctk.CTkTextbox(info, height=120, wrap="word")
        self.manual_text.pack(fill="x", padx=10, pady=(0, 2))
        self.btn_manual_longtext = ctk.CTkButton(info, text="展开全文", width=90,
                                                 command=self._manual_longtext, state="disabled")
        self.btn_manual_longtext.pack(anchor="w", padx=10, pady=(0, 6))

        ctk.CTkLabel(self.manual_page, text="图片预览（缩略图；点下载后保存原图）",
                     anchor="w").pack(fill="x", padx=12, pady=(4, 0))
        self.preview_frame = ctk.CTkScrollableFrame(self.manual_page, height=210)
        self.preview_frame.pack(fill="both", expand=True, padx=8, pady=4)

        btns = ctk.CTkFrame(self.manual_page)
        btns.pack(fill="x", padx=8, pady=8)
        self.btn_manual_dl = ctk.CTkButton(btns, text="下载此条", width=110, fg_color="#2e7d32",
                                           command=lambda: self._manual_action("download"))
        self.btn_manual_dl.pack(side="left", padx=4)
        self.btn_manual_dl_unfav = ctk.CTkButton(btns, text="下载并取消收藏", width=140,
                                                 command=lambda: self._manual_action("download_unfav"))
        self.btn_manual_dl_unfav.pack(side="left", padx=4)
        self.btn_manual_unfav = ctk.CTkButton(btns, text="仅取消收藏", width=100, fg_color="gray30",
                                              command=lambda: self._manual_action("unfav"))
        self.btn_manual_unfav.pack(side="left", padx=4)
        self.btn_manual_skip = ctk.CTkButton(btns, text="跳过", width=80, fg_color="gray30",
                                             command=self._manual_next)
        self.btn_manual_skip.pack(side="left", padx=4)
        self.manual_busy_label = ctk.CTkLabel(btns, text="", text_color="gray60")
        self.manual_busy_label.pack(side="right", padx=10)

    # ============================ 配置与连接 ============================

    def _load_config(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    self.cfg = json.load(f)
            except Exception:
                self.cfg = {}
        self.cfg.setdefault("cookie", "")
        self.cfg.setdefault("download_dir", "downloads")
        self.cfg.setdefault("request_interval", [1.0, 3.0])
        self.cfg.setdefault("image_interval", [0.3, 1.0])
        self.cfg.setdefault("retry_times", 3)
        self.cfg.setdefault("save_raw_json", True)
        self.cfg.setdefault("user_agent", core.CONFIG_TEMPLATE["user_agent"])
        self.cookie_entry.delete(0, "end")
        self.cookie_entry.insert(0, self.cfg.get("cookie", ""))
        self.dir_entry.delete(0, "end")
        # 旧版本默认下载目录（exe 旁 downloads）已废弃：迁移到"我的文档"，避免更新程序时误删数据
        gui_dir = self.cfg.get("_gui_dir", "") or ""
        if gui_dir == LEGACY_DEFAULT_DIR or gui_dir == "":
            gui_dir = DEFAULT_DOWNLOAD_DIR
        self.dir_entry.insert(0, gui_dir)
        self.api_entry.delete(0, "end")
        self.api_entry.insert(0, self.cfg.get("list_api", ""))
        if self.cfg.get("list_api"):
            core.set_list_api(self.cfg["list_api"])
        self._apply_base_dir()

    def _apply_base_dir(self):
        self.base_dir = self.dir_entry.get().strip() or DEFAULT_DOWNLOAD_DIR
        self.manifest = core.load_manifest(self.base_dir)
        self._refresh_manifest()

    def _make_session(self):
        self.cfg["cookie"] = self.cookie_entry.get().strip()
        core.set_list_api(self.api_entry.get().strip())
        self.session = core.make_session(self.cfg)

    def _ensure_session(self):
        cookie = self.cookie_entry.get().strip()
        if not cookie or cookie == "在此粘贴Cookie":
            self._log("[提示] 请先粘贴 Cookie（F12 → 网络 → 收藏页 → 请求头 → Cookie 整段）")
            return False
        self._make_session()
        return True

    def _save_cookie(self):
        cookie = self.cookie_entry.get().strip()
        if not cookie or cookie == "在此粘贴Cookie":
            self._log("[提示] Cookie 为空，请先粘贴")
            return
        self.cfg["cookie"] = cookie
        self.cfg["list_api"] = self.api_entry.get().strip()
        self.cfg["_gui_dir"] = self.dir_entry.get().strip() or DEFAULT_DOWNLOAD_DIR
        core.set_list_api(self.cfg["list_api"])
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        self._make_session()
        self._log("Cookie 已保存并应用。")

    def _api_reset(self):
        self.api_entry.delete(0, "end")
        core.set_list_api("")
        self._log("已恢复默认收藏列表接口: %s" % core.LIST_API)

    def _test_cookie(self):
        if not self._ensure_session():
            return
        self._set_status("正在测试连接...")
        self._start_thread(self._test_worker)

    def _test_worker(self):
        try:
            items = core.fetch_page(self.session, 1)
            def _ok():
                self._log("测试成功：第 1 页共 %d 条收藏。" % len(items))
                self._set_status("连接正常")
            self.after(0, _ok)
        except Exception as e:
            err = str(e)
            def _err():
                self._log("[错误] 测试失败：%s" % err)
                self._set_status("连接失败")
            self.after(0, _err)

    def _browse_dir(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(initialdir=self.base_dir, title="选择下载目录")
        if d:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, d)
            self._apply_base_dir()

    def _open_dir(self):
        try:
            os.makedirs(self.base_dir, exist_ok=True)
            os.startfile(self.base_dir)
        except Exception as e:
            self._log("[错误] 打开目录失败：%s" % e)

    # ============================ 批量模式 ============================

    def _batch_start(self):
        if self.batch_running:
            return
        if not self._ensure_session():
            return
        try:
            start_page = int(self.start_page_entry.get().strip() or 1)
        except ValueError:
            start_page = 1
        self.base_dir = self.dir_entry.get().strip() or DEFAULT_DOWNLOAD_DIR
        try:
            os.makedirs(self.base_dir, exist_ok=True)
        except Exception as e:
            self._log("[错误] 无法创建下载目录：%s" % e)
            return
        self.manifest = core.load_manifest(self.base_dir)
        self.stop_event.clear()
        self.pause_event.clear()
        self.batch_stats = {"items": 0, "new": 0, "skipped": 0,
                            "images": 0, "image_skipped": 0, "image_failed": 0, "failed": []}
        opts = argparse.Namespace(start_page=start_page, end_page=None, limit=None, max_pages=500,
                                  dry_run=False, auto_unfavorite=self.auto_unfav_var.get(),
                                  stop_event=self.stop_event, pause_event=self.pause_event)
        self.batch_running = True
        self._set_batch_buttons(running=True)
        self._set_status("批量下载中...")
        self._start_thread(self._batch_worker, opts)

    def _batch_worker(self, opts):
        try:
            core.run_batch_download(self.session, self.cfg, self.base_dir, self.manifest,
                                    opts, stats=self.batch_stats)
            self.after(0, lambda: self._log("批量下载完成。"))
        except Exception as e:
            err = str(e)
            self.after(0, lambda: self._log("[错误] 批量下载：%s" % err))
        finally:
            try:
                core.save_manifest(self.base_dir, self.manifest)
            except Exception:
                pass
            self.after(0, self._batch_finished)

    def _batch_finished(self):
        self.batch_running = False
        self._set_batch_buttons(running=False)
        self._refresh_manifest()
        self._update_batch_stats()
        self._set_status("空闲")
        try:
            from tkinter import messagebox
            messagebox.showinfo("下载结束", "批量下载已结束，详见日志与已下载清单。", parent=self)
        except Exception:
            pass

    def _batch_pause(self):
        if not self.batch_running:
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.btn_batch_pause.configure(text="暂停")
            self._set_status("继续下载...")
        else:
            self.pause_event.set()
            self.btn_batch_pause.configure(text="继续")
            self._set_status("已暂停（下载中图片会完成后停下）")

    def _batch_stop(self):
        if not self.batch_running:
            return
        self.stop_event.set()
        self.pause_event.clear()
        self._set_status("正在停止...")

    def _set_batch_buttons(self, running):
        self.btn_batch_start.configure(state="disabled" if running else "normal")
        self.btn_batch_pause.configure(state="normal" if running else "disabled")
        self.btn_batch_stop.configure(state="normal" if running else "disabled")

    def _update_batch_stats(self):
        s = self.batch_stats or {}
        if not s:
            self.batch_stat_label.configure(text="尚未开始")
            return
        total = getattr(self, "batch_total", None)
        prefix = "共 %s 条收藏 | " % total if total else ""
        self.batch_stat_label.configure(text=(
            prefix + "已处理 %d 条 | 新保存 %d | 跳过(已存在) %d | 图片成功 %d / 失败 %d" %
            (s.get("items", 0), s.get("new", 0), s.get("skipped", 0),
             s.get("images", 0), s.get("image_failed", 0))))

    # ============================ 手动模式 ============================

    def _on_mode_change(self, value):
        if value == "批量模式":
            self.manual_page.pack_forget()
            self.batch_page.pack(fill="both", expand=True)
        else:
            self.batch_page.pack_forget()
            self.manual_page.pack(fill="both", expand=True)

    def _manual_start(self):
        if not self._ensure_session():
            return
        self.manual_page_num = 1
        self.manual_items = []
        self.manual_index = 0
        self.btn_manual_prev.configure(state="disabled")
        self.btn_manual_next.configure(state="disabled")
        self._manual_fetch()

    def _manual_fetch(self):
        self._set_status("正在读取第 %d 页..." % self.manual_page_num)
        self._start_thread(self._manual_fetch_worker)

    def _manual_fetch_worker(self):
        try:
            items = core.fetch_page(self.session, self.manual_page_num)
            statuses = [s for s in (core.extract_status(i) for i in items) if s]
            self.after(0, lambda: self._manual_set_items(statuses))
        except Exception as e:
            err = str(e)
            def _err():
                self._log("[错误] 读取收藏失败：%s" % err)
                self._set_status("读取失败")
            self.after(0, _err)

    def _manual_set_items(self, statuses):
        if not statuses:
            self._log("没有更多收藏了。")
            self._set_status("浏览结束")
            self.btn_manual_next.configure(state="disabled")
            return
        self.manual_items = statuses
        self.manual_index = 0
        self.btn_manual_prev.configure(state="normal")
        self.btn_manual_next.configure(state="normal")
        self._manual_show()

    def _manual_show(self):
        s = self.manual_items[self.manual_index]
        self.current_status = s
        user = s.get("user") or {}
        name = user.get("screen_name") or user.get("name") or "未知"
        ts = core.format_time(s.get("created_at")) or "未知"
        mid = str(s.get("mid") or s.get("idstr") or s.get("id") or "")
        source = core.clean_text(s.get("source") or "") or "未知"

        mark = ""
        m = self.manifest.get(mid)
        if m:
            st = m.get("status")
            if st == "done":
                mark = "  [已下载 ✓]"
            elif st == "unfavorited":
                mark = "  [已取消收藏]"
            else:
                mark = "  [未完成: %s]" % STATE_TEXT.get(st, st)
        rt_note = "  [转发微博 · 只保存原博]" if s.get("retweeted_status") else ""

        pos = self.manual_index + 1
        self.manual_pos_label.configure(text="第 %d / %d 条（本页）%s%s" % (pos, len(self.manual_items), mark, rt_note))
        self.manual_meta_label.configure(text="用户名: %s\n发布时间: %s\n来源: %s" % (name, ts, source))

        self.manual_text.configure(state="normal")
        self.manual_text.delete("1.0", "end")
        self.manual_text.insert("1.0", core.clean_text(s.get("text") or ""))
        self.manual_text.configure(state="disabled")
        self.btn_manual_longtext.configure(state="normal" if s.get("isLongText") else "disabled")

        for w in self.preview_frame.winfo_children():
            w.destroy()
        self._start_thread(self._manual_load_previews, s)

    def _manual_load_previews(self, status):
        images = []
        for url in core.get_preview_urls(status):
            images.append(self._fetch_preview(url))
        self.after(0, lambda: self._manual_show_previews(images))

    def _fetch_preview(self, url, width=180):
        if url in self.preview_cache:
            return self.preview_cache[url]
        # 缓存上限：浏览大量收藏时防止内存膨胀
        if len(self.preview_cache) > 300:
            self.preview_cache.clear()
        try:
            r = self.session.get(url, timeout=15)
            img = Image.open(io.BytesIO(r.content))
            img = pil_resize(img, width)
            cimg = ctk.CTkImage(light_image=img, dark_image=img, size=(img.width, img.height))
            self.preview_cache[url] = cimg
            return cimg
        except Exception:
            return None

    def _manual_show_previews(self, images):
        for w in self.preview_frame.winfo_children():
            w.destroy()
        if not images:
            ctk.CTkLabel(self.preview_frame, text="（本条无图片，或预览加载失败）",
                         text_color="gray50").grid(row=0, column=0, padx=8, pady=8)
            return
        cols = 4
        for i, img in enumerate(images):
            r, c = divmod(i, cols)
            if img is None:
                lab = ctk.CTkLabel(self.preview_frame, text="[加载失败]", text_color="gray50")
            else:
                lab = ctk.CTkLabel(self.preview_frame, image=img, text="")
            lab.grid(row=r, column=c, padx=4, pady=4)

    def _manual_next(self):
        if not self.manual_items:
            return
        if self.manual_index + 1 < len(self.manual_items):
            self.manual_index += 1
            self._manual_show()
        else:
            self.manual_page_num += 1
            self._manual_fetch()

    def _manual_prev_page(self):
        if self.manual_page_num > 1:
            self.manual_page_num -= 1
            self._manual_fetch()

    def _manual_next_page(self):
        self.manual_page_num += 1
        self._manual_fetch()

    def _manual_longtext(self):
        s = self.manual_items[self.manual_index]
        mid = str(s.get("mid") or s.get("idstr") or s.get("id") or "")
        self._set_status("正在获取全文...")
        self._start_thread(self._longtext_worker, s, mid)

    def _longtext_worker(self, s, mid):
        lt = core.fetch_longtext(self.session, mid)
        def _done():
            if lt:
                self.manual_text.configure(state="normal")
                self.manual_text.delete("1.0", "end")
                self.manual_text.insert("1.0", core.clean_text(lt))
                self.manual_text.configure(state="disabled")
                self._log("已获取全文。")
            else:
                self._log("获取全文失败或无更多内容。")
            self._set_status("空闲")
        self.after(0, _done)

    # ---- 手动动作：下载 / 下载并取消收藏 / 仅取消收藏 ----

    def _manual_action(self, action):
        if not self.manual_items:
            return
        s = self.manual_items[self.manual_index]
        if action == "download":
            self._run_download_worker(s, unfav=False)
        elif action == "download_unfav":
            self._confirm_unfav("确认操作", "下载本条并取消收藏？\n\n下载全部成功后才取消收藏；"
                                "微博本身与已下载文件不受影响。",
                                lambda: self._run_download_worker(s, unfav=True))
        elif action == "unfav":
            self._confirm_unfav("确认操作", "确认从收藏夹移除本条？\n\n此操作不可撤销（可重新收藏），"
                                "微博本身不会删除。",
                                lambda: self._run_unfav(s))

    def _confirm_unfav(self, title, msg, cb):
        from tkinter import messagebox
        if messagebox.askyesno(title, msg, parent=self):
            cb()

    def _run_download_worker(self, s, unfav):
        self._set_manual_actions(False)
        self.manual_busy_label.configure(text="下载中...")
        self._set_status("正在下载本条...")
        self._start_thread(self._download_worker, s, unfav)

    def _download_worker(self, s, unfav):
        stats = {"items": 0, "new": 0, "skipped": 0,
                 "images": 0, "image_skipped": 0, "image_failed": 0, "failed": []}
        try:
            info = core.process_status(self.session, s, self.base_dir, self.cfg,
                                       stats)
        except Exception as e:
            err = str(e)
            def _err():
                self._log("[错误] 下载本条失败：%s" % err)
                self._finish_manual_action()
            self.after(0, _err)
            return

        def _update():
            if info:
                self.manifest[info["mid"]] = {
                    "user": info["user"], "time": info["time"], "dir": info["dir"],
                    "images": info["images"], "image_failed": info.get("image_failed", 0),
                    "status": info["status"],
                    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                core.save_manifest(self.base_dir, self.manifest)
                self._refresh_manifest()
                self._log("本条处理完成：%s（状态：%s）" %
                          (info["dir"], STATE_TEXT.get(info["status"], info["status"])))
            self._finish_manual_action()
        self.after(0, _update)

        # 下载全部成功才允许取消收藏
        if unfav and info and info["status"] == "done":
            self._start_thread(self._unfav_worker, info["mid"])
        elif unfav:
            self.after(0, lambda: self._log("[提示] 本条下载未完全成功，未取消收藏。"))

    def _run_unfav(self, s):
        mid = str(s.get("mid") or s.get("idstr") or s.get("id") or "")
        self._set_manual_actions(False)
        self.manual_busy_label.configure(text="取消收藏中...")
        self._start_thread(self._unfav_worker, mid)

    def _unfav_worker(self, mid):
        ok, msg = core.unfavorite(self.session, mid)
        def _done():
            if ok:
                self._log("[已取消收藏] %s" % mid)
                it = self.manifest.get(mid)
                if it:
                    it["unfavorited"] = True
                else:
                    self.manifest[mid] = {
                        "user": "?", "time": "?", "images": 0,
                        "status": "unfavorited",
                        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                core.save_manifest(self.base_dir, self.manifest)
                self._refresh_manifest()
            else:
                self._log("[取消收藏失败] %s：%s" % (mid, msg))
            self._finish_manual_action()
        self.after(0, _done)

    def _finish_manual_action(self):
        self._set_manual_actions(True)
        self.manual_busy_label.configure(text="")
        self._set_status("空闲")

    def _set_manual_actions(self, enabled):
        st = "normal" if enabled else "disabled"
        for b in (self.btn_manual_dl, self.btn_manual_dl_unfav, self.btn_manual_unfav,
                  self.btn_manual_skip, self.btn_manual_longtext):
            b.configure(state=st)

    # ============================ 进度回调 / 日志 / 清单 ============================

    def _on_progress(self, event, kw):
        try:
            self.after(0, lambda: self._handle_progress(event, kw))
        except Exception:
            pass

    def _handle_progress(self, event, kw):
        if event == "page":
            self._log("--- 第 %d 页 ---" % kw.get("page"))
            if kw.get("total"):
                self.batch_total = kw["total"]
                self._update_batch_stats()
        elif event == "item_start":
            self._set_status("正在处理: %s" % kw.get("mid"))
        elif event == "item_done":
            self._update_batch_stats()
            self._refresh_manifest()
        elif event == "unfavorited":
            if kw.get("ok"):
                self._log("[已取消收藏] %s" % kw.get("mid"))
            else:
                self._log("[取消收藏失败] %s：%s" % (kw.get("mid"), kw.get("msg")))
            self._refresh_manifest()
        elif event == "auto_stop":
            self._log("[提示] 自动停止：%s（请稍后重试，或检查 Cookie 是否过期）" % kw.get("reason", "连续失败"))
        elif event == "paused":
            self._set_status("已暂停（等待继续）")
        elif event == "resumed":
            self._set_status("已继续")

    def _refresh_manifest(self):
        for w in self.manifest_frame.winfo_children():
            w.destroy()
        items = self.manifest
        if not items:
            ctk.CTkLabel(self.manifest_frame, text="（暂无记录）",
                         text_color="gray50").grid(row=0, column=0, padx=6, pady=4, sticky="w")
            return
        rows = sorted(items.items(), key=lambda kv: kv[1].get("time", ""), reverse=True)
        for i, (mid, it) in enumerate(rows):
            st = it.get("status", "?")
            text = STATE_TEXT.get(st, st)
            icon = {"done": "✓", "partial": "⚠", "failed": "✗", "unfavorited": "☆"}.get(st, "?")
            extra = ""
            if st == "partial":
                extra = "（失败 %d 张）" % it.get("image_failed", 0)
            elif st == "failed":
                extra = "（%s）" % str(it.get("error", ""))[:40]
            line = "%s %s | %s | %s | 图片 %d 张%s" % (
                icon, text, it.get("user", "?"), it.get("time", "?"),
                it.get("images", 0), extra)
            ctk.CTkLabel(self.manifest_frame, text=line, anchor="w", justify="left"
                         ).grid(row=i, column=0, padx=6, pady=2, sticky="w")

    def _log(self, msg):
        self._log_file(msg)
        self.log_box.configure(state="normal")
        self.log_box.insert("end", "[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), msg))
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _set_status(self, text):
        self.status_label.configure(text=text)

    def _on_close(self):
        if self.batch_running:
            self.stop_event.set()
            self.pause_event.clear()
        self.destroy()


def main():
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        # 单实例保护：防止重复打开导致并发下载/风控
        try:
            mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "WeiboFavoritesDownloader_SingleInstance")
            if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                import tkinter as tk
                from tkinter import messagebox
                root = tk.Tk()
                root.withdraw()
                messagebox.showwarning("提示", "程序已在运行，请勿重复打开。")
                root.destroy()
                return
        except Exception:
            pass
    app = App()
    app.protocol("WM_DELETE_WINDOW", app._on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
