# -*- coding: utf-8 -*-
"""
微博收藏下载器
================
- 逐条下载微博收藏：正文文字 + 图片原图 + 用户名 + 发布时间
- 目录结构：下载目录/用户名-微博时间/微博内容.txt, 图片1.jpg ...
- 转发微博：原博正文与图片一并下载，保存到子文件夹 "原博-用户名-时间/"
- 断点续传：已存在的文件自动跳过，中断后重跑只补缺失项
- 长微博：自动获取全文
- 探测模式：先运行 --probe 验证 Cookie 与接口字段，再全量下载
- 进度清单：downloads/已下载清单.json 记录每条收藏状态（完成/部分失败/异常），--list 查看
- 下载控制：Ctrl+C 安全暂停；--limit 限量；--start-page/--end-page 控制范围；重跑自动续传

用法:
    python weibo_favorites_downloader.py --probe          # 先探测验证
    python weibo_favorites_downloader.py                  # 全量下载
    python weibo_favorites_downloader.py --dry-run        # 只预览不落盘
    python weibo_favorites_downloader.py --list           # 查看下载进度清单
    python weibo_favorites_downloader.py --limit 50       # 本次只下载 50 条
"""

import argparse
import html
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

try:
    import requests
except ImportError:
    sys.exit("缺少依赖 requests，请先执行: pip install requests")

LONGTEXT_URL = "https://weibo.com/ajax/statuses/longtext"
UNFAV_URL = "https://weibo.com/ajax/statuses/destoryFavorites"

# 收藏列表接口地址（微博改版后可覆盖，见 set_list_api / 配置项 list_api）
BASE_URL = "https://weibo.com/ajax/favorites/all_fav"
LIST_API = BASE_URL
LAST_TOTAL = None  # 最近一次拉取到的总收藏数（供界面显示）


def set_list_api(url):
    """自定义收藏列表接口地址。url 为空时恢复默认；相对路径自动补全 https://weibo.com 前缀。"""
    global LIST_API
    url = (url or "").strip()
    if url.startswith("/"):
        url = "https://weibo.com" + url
    LIST_API = url if url else BASE_URL

TXT_NAME = "微博内容.txt"
RAW_NAME = "raw.json"
MANIFEST_NAME = "已下载清单.json"
LOG_NAME = "运行日志.log"

# 微博图床的缩略图尺寸标记，替换为 large 即原图
SIZE_TAGS = [
    "orj360", "mw690", "mw1024", "mw2048", "thumb150",
    "thumbnail", "bmiddle", "m150", "square", "wap360", "woriginal",
]

CONFIG_TEMPLATE = {
    "_说明": "本文件为配置模板。Cookie 获取：浏览器登录 weibo.com 后按 F12，打开网络面板，刷新收藏页，点开任意 weibo.com/ajax/collection 请求，在请求头(Request Headers)中复制 Cookie 整段，粘贴到下方 cookie 字段。",
    "cookie": "在此粘贴Cookie",
    "download_dir": "downloads",
    "request_interval": [1.0, 3.0],
    "image_interval": [0.3, 1.0],
    "retry_times": 3,
    "save_raw_json": True,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


# ---------------------------------------------------------------- 进度回调

_progress_cb = None


def set_progress_callback(cb):
    """
    设置进度回调，供 GUI 使用。
    cb(event, kwargs)：event 为字符串（page/text/image/item_done/paused/resumed/log），
    kwargs 为附加数据。回调在下载线程中执行，GUI 需自行切换到 UI 线程更新界面。
    """
    global _progress_cb
    _progress_cb = cb


def _emit(event, **kw):
    cb = _progress_cb
    if cb:
        try:
            cb(event, kw)
        except Exception:
            pass


# ---------------------------------------------------------------- 基础工具

def clean_filename(name, max_len=80):
    """清洗 Windows 文件名非法字符并限制长度。"""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name or "unknown"


def clean_text(t):
    """清理微博正文 HTML：去标签、还原实体、保留换行。"""
    if not t:
        return ""
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</?[a-zA-Z][^>]*>", "", t)
    t = html.unescape(t)
    t = t.replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def format_time(v):
    """时间戳(ms/s)或时间字符串 -> 'YYYY-MM-DD HH-MM-SS'（冒号已替换，可直接用于文件名）。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        try:
            ts = v / 1000.0 if v > 1e12 else v
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H-%M-%S")
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(v, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%a %b %d %H:%M:%S %z %Y"):
            try:
                return datetime.strptime(v, fmt).strftime("%Y-%m-%d %H-%M-%S")
            except ValueError:
                continue
    return None


def to_original_url(url):
    """把缩略图 URL 转为原图 URL（去掉尺寸标记，统一走 /large/）。"""
    for tag in SIZE_TAGS:
        url = re.sub(r"/%s/" % tag, "/large/", url)
    return url


def get_image_urls(status):
    """从微博数据中提取所有图片原图 URL。"""
    urls = []
    pics = status.get("pics")
    if isinstance(pics, list):
        for p in pics:
            if not isinstance(p, dict):
                continue
            large = p.get("large")
            url = large.get("url") if isinstance(large, dict) else None
            if not url:
                url = p.get("url")
            if url:
                urls.append(to_original_url(url))
    if not urls:
        op = status.get("original_pic")
        if op:
            urls.append(to_original_url(op))
    if not urls:
        pids = status.get("pic_ids")
        if isinstance(pids, list):
            for pid in pids:
                if pid:
                    urls.append("https://wx1.sinaimg.cn/large/%s.jpg" % pid)
    # 去重保序
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def get_preview_urls(status):
    """获取缩略图预览 URL（小尺寸，仅用于界面预览，不下载原图）。"""
    urls = []
    pics = status.get("pics")
    if isinstance(pics, list):
        for p in pics:
            if not isinstance(p, dict):
                continue
            url = p.get("url")
            if not url:
                large = p.get("large")
                url = large.get("url") if isinstance(large, dict) else None
            if url:
                urls.append(url)
    if not urls:
        pids = status.get("pic_ids")
        if isinstance(pids, list):
            for pid in pids:
                if pid:
                    urls.append("https://wx1.sinaimg.cn/orj360/%s.jpg" % pid)
    return urls


# ---------------------------------------------------------------- 网络层

def make_session(cfg):
    s = requests.Session()
    cookie = (cfg.get("cookie") or "").strip()
    # 容错：用户可能连同 "Cookie:" 前缀一起复制，自动去掉
    cookie = re.sub(r"^cookie\s*:\s*", "", cookie, flags=re.I)
    ua = cfg.get("user_agent") or CONFIG_TEMPLATE["user_agent"]
    s.headers.update({
        "User-Agent": ua,
        "Referer": "https://weibo.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": cookie,
    })
    # 写操作（如取消收藏）需要 CSRF token：从 Cookie 中解析 XSRF-TOKEN
    m = re.search(r"(?:^|;\s*)XSRF-TOKEN=([^;]+)", cookie)
    if m:
        s.headers["x-xsrf-token"] = m.group(1)
    return s


def fetch_page(session, page):
    """
    拉取一页收藏列表，返回原始条目列表。
    兼容返回字段：data.list（旧版）与 data.status（新版 all_fav）。
    保留接口 URL 中已有的查询参数（如 uid），仅覆盖 page。
    同时记录总收藏数到模块级 LAST_TOTAL（供界面显示）。
    """
    global LAST_TOTAL
    parts = urlparse(LIST_API)
    q = parse_qs(parts.query)
    q["page"] = [str(page)]
    url = urlunparse(parts._replace(query=urlencode(q, doseq=True)))
    r = session.get(url, timeout=20)
    if r.status_code in (401, 403):
        raise RuntimeError("HTTP %d：Cookie 可能已过期，请重新复制后修改 config.json" % r.status_code)
    r.raise_for_status()
    data = r.json()
    if data.get("ok") != 1:
        raise RuntimeError("接口返回异常: " + json.dumps(data, ensure_ascii=False)[:300])
    d = data.get("data")
    if isinstance(d, dict):
        total = d.get("total_number")
        if total is not None:
            LAST_TOTAL = str(total)
        for key in ("list", "status"):
            v = d.get(key)
            if isinstance(v, list):
                return v
        return []
    if isinstance(d, list):
        return d
    return []


def fetch_longtext(session, mid):
    """获取长微博全文。"""
    try:
        r = session.get(LONGTEXT_URL, params={"id": mid}, timeout=20)
        data = r.json()
        if data.get("ok") == 1:
            return data.get("data", {}).get("longTextContent", "")
    except Exception:
        pass
    return ""


def unfavorite(session, mid):
    """
    取消收藏（写操作）。新版接口: POST /ajax/statuses/destoryFavorites，表单 id=<mid>。
    需要 x-xsrf-token 请求头（make_session 已自动从 Cookie 解析）。
    返回 (是否成功, 说明)。
    """
    try:
        r = session.post(UNFAV_URL, data={"id": mid}, timeout=20)
        data = r.json()
        msg = json.dumps(data, ensure_ascii=False)[:300]
        if data.get("ok") == 1:
            return True, "ok"
        # 已不在收藏夹视为成功（可能已在网页端手动取消）
        if "not your collection" in msg:
            return True, "已不在收藏夹"
        return False, msg
    except Exception as e:
        return False, str(e)


def download_image(session, url, path, cfg):
    """下载单张图片；已存在则跳过；失败按指数退避重试。"""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return "skipped"
    retry = int(cfg.get("retry_times", 3))
    for attempt in range(retry + 1):
        try:
            r = session.get(url, timeout=30, stream=True)
            if r.status_code == 200:
                tmp = path + ".part"
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(8192):
                        if chunk:
                            f.write(chunk)
                os.replace(tmp, path)
                return "downloaded"
            if r.status_code == 403:
                return "HTTP 403 (防盗链/权限)"
            if r.status_code == 404:
                return "HTTP 404"
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            if attempt >= retry:
                return "网络错误: %s" % e
            time.sleep(2 ** attempt)
    return "failed"


# ---------------------------------------------------------------- 数据解析与落盘

def extract_status(item):
    """兼容两种返回结构：条目本身是微博，或包在 status 字段里。"""
    if isinstance(item, dict):
        s = item.get("status")
        return s if isinstance(s, dict) else item
    return None


def get_target_dir(base_dir, dir_name, mid):
    """
    计算目标目录。
    - 目录不存在 -> 创建
    - 目录存在且 raw.json 的 mid 相同 -> 复用（断点续传）
    - 目录存在但是另一条微博（同名同时刻）-> 追加序号避免覆盖
    返回 (目录路径, 是否复用已有目录)
    """
    candidate = os.path.join(base_dir, dir_name)
    n = 2
    while True:
        if not os.path.exists(candidate):
            os.makedirs(candidate, exist_ok=True)
            return candidate, False
        raw_path = os.path.join(candidate, RAW_NAME)
        same = False
        if os.path.exists(raw_path):
            try:
                with open(raw_path, encoding="utf-8") as f:
                    raw = json.load(f)
                raw_mid = str(raw.get("mid") or raw.get("idstr") or raw.get("id") or "")
                same = bool(mid) and raw_mid == mid
            except Exception:
                same = False
        if same:
            return candidate, True
        candidate = os.path.join(base_dir, "%s (%d)" % (dir_name, n))
        n += 1


def build_text(status, full_text):
    """组装微博内容.txt 的正文。"""
    user = status.get("user") or {}
    created = format_time(status.get("created_at")) or "未知"
    source = re.sub(r"<[^>]+>", "", status.get("source") or "").strip() or "未知"
    mid = status.get("mid") or status.get("idstr") or status.get("id") or ""
    uid = user.get("idstr") or user.get("id") or ""
    lines = [
        "用户名: %s" % (user.get("screen_name") or user.get("name") or "未知"),
        "发布时间: %s" % created,
        "来源: %s" % source,
    ]
    if uid and mid:
        lines.append("链接: https://weibo.com/%s/%s" % (uid, mid))
    if status.get("isLongText") or status.get("is_long_text"):
        lines.append("(本条为长微博，已获取全文)")
    if status.get("video") or (status.get("page_info") or {}).get("type") in ("video", "article"):
        lines.append("(本条包含视频/长文章，未下载媒体，仅保存文字)")
    lines.append("-" * 40)
    lines.append(full_text)
    return "\n".join(lines)


def process_status(session, status, base_dir, cfg, stats, is_root=True, dry_run=False):
    """
    处理一条微博（根收藏或转发的原博）。
    base_dir: 根收藏时为下载根目录；原博时为所属收藏的目录。
    返回该条的处理结果 dict（根收藏用于写进度清单；原博递归时不返回）。
    """
    user = status.get("user") or {}
    name = clean_filename(user.get("screen_name") or user.get("name") or "unknown")
    ts = format_time(status.get("created_at")) or "unknown-time"
    mid = str(status.get("mid") or status.get("idstr") or status.get("id") or "")
    dir_name = ("原博-" if not is_root else "") + "%s-%s" % (name, ts)
    urls = get_image_urls(status)

    info = {"mid": mid, "user": name, "time": ts,
            "dir": dir_name, "images": len(urls), "text": "exists", "status": "done"}

    if dry_run:
        print("  [计划] %s | 图片 %d 张 | mid=%s" % (dir_name, len(urls), mid))
        return info

    target, resumed = get_target_dir(base_dir, dir_name, mid)
    txt_path = os.path.join(target, TXT_NAME)

    if not (resumed and os.path.exists(txt_path)):
        full_text = clean_text(status.get("text") or "")
        if status.get("isLongText"):
            lt = fetch_longtext(session, mid)
            if lt:
                full_text = clean_text(lt)
        body = build_text(status, full_text)
        if is_root and status.get("retweeted_status"):
            rt = status["retweeted_status"]
            rt_user = (rt.get("user") or {}).get("screen_name") or "未知"
            rt_ts = format_time(rt.get("created_at")) or "unknown-time"
            body += "\n\n[转发] 原博内容见子文件夹: 原博-%s-%s" % (
                clean_filename(rt_user), rt_ts)
        with open(txt_path, "w", encoding="utf-8-sig") as f:
            f.write(body)
        stats["new"] += 1
        info["text"] = "new"
        _emit("text", mid=mid, dir=os.path.basename(target))
        print("  [保存] %s" % os.path.basename(target))
    else:
        stats["skipped"] += 1
    # 原始 JSON（断点续传的识别标记 + 排查用）
    if cfg.get("save_raw_json"):
        raw_path = os.path.join(target, RAW_NAME)
        if not os.path.exists(raw_path):
            with open(raw_path, "w", encoding="utf-8") as f:
                json.dump(status, f, ensure_ascii=False, indent=2)

    # 下载图片（已存在自动跳过）
    img_failed = 0
    for i, url in enumerate(urls, 1):
        ext = os.path.splitext(urlparse(url).path)[1] or ".jpg"
        img_path = os.path.join(target, "图片%d%s" % (i, ext))
        res = download_image(session, url, img_path, cfg)
        _emit("image", mid=mid, url=url, result=res)
        if res == "downloaded":
            stats["images"] += 1
            print("    [下载] %s" % os.path.basename(img_path))
        elif res == "skipped":
            stats["image_skipped"] += 1
        else:
            stats["image_failed"] += 1
            img_failed += 1
            print("    [失败] %s -> %s" % (url, res))
        time.sleep(random.uniform(*cfg["image_interval"]))

    info["image_failed"] = img_failed
    info["status"] = "partial" if img_failed else "done"
    _emit("item_done", info=info)
    print("  [完成] %s" % os.path.basename(target))

    # 转发的原博：递归处理，存入子文件夹
    rt = status.get("retweeted_status")
    if rt and isinstance(rt, dict):
        if dry_run:
            print("    └─ 包含转发原博：")
            process_status(session, rt, base_dir, cfg, stats, is_root=False, dry_run=True)
        else:
            process_status(session, rt, target, cfg, stats, is_root=False, dry_run=False)

    return info


# ---------------------------------------------------------------- 进度清单与日志

def load_manifest(base):
    """读取进度清单，返回 {mid: 条目}。"""
    path = os.path.join(base, MANIFEST_NAME)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items", {})
            return items if isinstance(items, dict) else {}
        except Exception:
            return {}
    return {}


def save_manifest(base, items):
    """保存进度清单。"""
    path = os.path.join(base, MANIFEST_NAME)
    data = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "说明": "status: done=完成, partial=图片有失败(重跑自动补), failed=处理异常",
        "items": items,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def log_run(base, msg):
    """追加一行运行日志。"""
    if not base or not os.path.isdir(base):
        return
    path = os.path.join(base, LOG_NAME)
    with open(path, "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


def print_manifest(items):
    """打印进度清单：哪些已下载、哪些失败、哪些已取消收藏。"""
    if not items:
        print("还没有下载记录（还没有运行过，或下载目录为空）。")
        return
    done = partial = failed = unfav = 0
    labels = {"done": "完成", "partial": "部分失败",
              "failed": "异常", "unfavorited": "已取消收藏"}
    print("已下载清单（共 %d 条）:" % len(items))
    for mid, it in sorted(items.items()):
        st = it.get("status", "?")
        if st == "done":
            done += 1
        elif st == "partial":
            partial += 1
        elif st == "unfavorited":
            unfav += 1
        else:
            failed += 1
        extra = ""
        if st == "partial":
            extra = " (失败 %d 张)" % it.get("image_failed", 0)
        elif st == "failed":
            extra = " (处理异常: %s)" % it.get("error", "")
        print("  [%s] mid=%s | 用户: %s | 时间: %s | 图片: %d 张%s" % (
            labels.get(st, st), mid, it.get("user", "?"), it.get("time", "?"),
            it.get("images", 0), extra))
    print("汇总: 完成 %d | 部分失败 %d | 已取消收藏 %d | 异常 %d" % (done, partial, unfav, failed))


# ---------------------------------------------------------------- 探测与主流程

def probe(session, out):
    """探测模式：拉取第一页原始 JSON，验证 Cookie 与字段结构。"""
    print("正在请求第一页收藏列表 ...")
    try:
        items = fetch_page(session, 1)
    except Exception as e:
        sys.exit("[探测失败] %s" % e)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print("成功获取 %d 条，已保存到 %s" % (len(items), out))
    if items:
        s = extract_status(items[0])
        if s:
            print("首条字段预览（键 -> 类型）:")
            print(json.dumps({k: type(v).__name__ for k, v in s.items()},
                             ensure_ascii=False, indent=2))
            print("请核对脚本使用的字段是否存在：")
            print("  user.screen_name / created_at / text / pics(或 pic_ids) /")
            print("  retweeted_status / isLongText / mid")
            print("若字段名不同，把 probe_output.json 发给我，我来适配解析逻辑。")
    else:
        print("第一页为空：可能没有收藏，或分页参数需调整（见 probe_output.json）。")


def load_config(path):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(CONFIG_TEMPLATE, f, ensure_ascii=False, indent=2)
        sys.exit("未找到配置文件，已生成模板 %s，请填写 Cookie 后重试" % path)
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("download_dir", "downloads")
    cfg.setdefault("request_interval", [1.0, 3.0])
    cfg.setdefault("image_interval", [0.3, 1.0])
    cfg.setdefault("retry_times", 3)
    cfg.setdefault("save_raw_json", True)
    cfg.setdefault("user_agent", CONFIG_TEMPLATE["user_agent"])
    # 自定义收藏列表接口（微博改版时可覆盖）
    if cfg.get("list_api"):
        set_list_api(cfg["list_api"])
    return cfg


def run_batch_download(session, cfg, base, manifest, opts, stats=None):
    """
    批量下载主循环（CLI 与 GUI 共用）。
    opts: 需包含 start_page/end_page/limit/max_pages/dry_run/auto_unfavorite；
          可选 stop_event / pause_event（threading.Event，None 表示不支持）。
    返回 stats 字典。
    """
    if stats is None:
        stats = {"items": 0, "new": 0, "skipped": 0,
                 "images": 0, "image_skipped": 0, "image_failed": 0, "failed": []}
    stop_event = getattr(opts, "stop_event", None)
    pause_event = getattr(opts, "pause_event", None)
    auto_unfavorite = bool(getattr(opts, "auto_unfavorite", False))

    def _should_stop():
        return stop_event is not None and stop_event.is_set()

    page = opts.start_page
    empty_pages = 0
    while True:
        # 暂停：等待 pause_event 清除；暂停期间若收到停止则退出
        if pause_event is not None and pause_event.is_set():
            _emit("paused")
            while pause_event.is_set():
                if _should_stop():
                    return stats
                time.sleep(0.2)
            _emit("resumed")
        if _should_stop():
            break
        if opts.end_page is not None and page > opts.end_page:
            break
        if page > opts.start_page + opts.max_pages:
            print("已达到页数上限 %d 页，停止。" % opts.max_pages)
            break
        print("--- 第 %d 页 ---" % page)
        _emit("page", page=page, total=LAST_TOTAL)
        fail_streak = 0
        try:
            items = fetch_page(session, page)
        except Exception as e:
            fail_streak += 1
            print("[错误] 第 %d 页请求失败: %s" % (page, e))
            if fail_streak >= 3:
                print("连续 %d 次请求失败，已停止。请稍后重试，或检查 Cookie 是否过期。" % fail_streak)
                _emit("auto_stop", reason="连续请求失败 %d 次" % fail_streak)
                break
            continue  # 重试同一页
        statuses = [s for s in (extract_status(i) for i in items) if s]
        if not statuses:
            empty_pages += 1
            if empty_pages >= 2 or opts.end_page is not None:
                print("没有更多收藏，结束。")
                break
        for s in statuses:
            if _should_stop():
                return stats
            mid = str(s.get("mid") or s.get("idstr") or s.get("id") or "")
            stats["items"] += 1
            _emit("item_start", mid=mid, index=stats["items"])
            if mid and manifest.get(mid, {}).get("status") == "done":
                stats["skipped"] += 1
                print("  [跳过] 已下载完成: %s" % mid)
            else:
                try:
                    info = process_status(session, s, base, cfg, stats,
                                          is_root=True, dry_run=opts.dry_run)
                    if info and not opts.dry_run:
                        entry = {
                            "user": info["user"], "time": info["time"],
                            "dir": info["dir"], "images": info["images"],
                            "image_failed": info.get("image_failed", 0),
                            "status": info["status"],
                            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        }
                        manifest[info["mid"]] = entry
                        # 下载成功后按需自动取消收藏（写操作）
                        if auto_unfavorite and mid and info["status"] == "done":
                            ok, msg = unfavorite(session, mid)
                            if ok:
                                entry["unfavorited"] = True
                                print("  [已取消收藏] %s" % mid)
                                _emit("unfavorited", mid=mid, ok=True)
                            else:
                                print("  [取消收藏失败] %s: %s" % (mid, msg))
                                _emit("unfavorited", mid=mid, ok=False, msg=msg)
                except Exception as e:
                    stats["failed"].append((mid, str(e)))
                    print("[错误] 处理失败 mid=%s: %s" % (mid, e))
                    manifest[mid] = {
                        "user": (s.get("user") or {}).get("screen_name", "?"),
                        "status": "failed",
                        "error": str(e),
                        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
            if opts.limit and stats["items"] >= opts.limit:
                print("已达到本次限量 %d 条，停止。" % opts.limit)
                return stats
            time.sleep(random.uniform(*cfg["request_interval"]))
        page += 1
    return stats


def main():
    ap = argparse.ArgumentParser(description="微博收藏下载器：正文 + 图片原图，按 用户名-微博时间 分目录")
    ap.add_argument("--config", default="config.json", help="配置文件路径")
    ap.add_argument("--probe", action="store_true", help="探测模式：拉取第一页原始 JSON 到 probe_output.json 后退出")
    ap.add_argument("--probe-out", default="probe_output.json")
    ap.add_argument("--list", action="store_true", help="查看已下载清单（哪些已下载/完成/失败），不下载")
    ap.add_argument("--start-page", type=int, default=1, help="起始页码")
    ap.add_argument("--end-page", type=int, default=None, help="结束页码（默认自动翻页直到没有更多）")
    ap.add_argument("--limit", type=int, default=None, help="本次最多处理多少条（含跳过的已完成项），用于小批量试跑")
    ap.add_argument("--max-pages", type=int, default=500, help="最大翻页数保护")
    ap.add_argument("--dry-run", action="store_true", help="只预览将要保存的内容，不写文件不下载")
    ap.add_argument("--auto-unfavorite", action="store_true",
                    help="下载成功后自动取消该条收藏（写操作，谨慎使用）")
    args = ap.parse_args()

    cfg = load_config(args.config)
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), cfg["download_dir"])

    # --list 不需要 Cookie，直接查看进度清单
    if args.list:
        print_manifest(load_manifest(base))
        return

    cookie = (cfg.get("cookie") or "").strip()
    if not cookie or cookie == "在此粘贴Cookie":
        sys.exit("请先在 config.json 中填写 Cookie。获取方法：登录微博 → F12 → 网络 → 刷新收藏页 → "
                 "点开 weibo.com/ajax/collection 请求 → 复制请求头中的 Cookie 整段。")

    session = make_session(cfg)
    if args.probe:
        probe(session, args.probe_out)
        return

    if not args.dry_run:
        os.makedirs(base, exist_ok=True)

    print("下载目录: %s" % base)
    print("提示: 已完成的收藏会自动跳过（断点续传）；Ctrl+C 可安全暂停，进度保存在 %s\n" % MANIFEST_NAME)

    manifest = load_manifest(base)
    stats = {"items": 0, "new": 0, "skipped": 0,
             "images": 0, "image_skipped": 0, "image_failed": 0, "failed": []}
    log_run(base, "开始运行 (start-page=%s, end-page=%s, limit=%s, dry-run=%s, auto-unfavorite=%s)" % (
        args.start_page, args.end_page, args.limit, args.dry_run, args.auto_unfavorite))

    interrupted = False
    try:
        stats = run_batch_download(session, cfg, base, manifest, args, stats=stats)
    except KeyboardInterrupt:
        interrupted = True
        print("\n[暂停] 已停止，进度已保存，重跑可续传。")
    finally:
        if not args.dry_run:
            save_manifest(base, manifest)
            log_run(base, ("运行结束(手动暂停)" if interrupted else "运行结束") +
                    " | 处理 %d 条, 新保存 %d, 跳过 %d, 图片成功 %d / 失败 %d" %
                    (stats["items"], stats["new"], stats["skipped"],
                     stats["images"], stats["image_failed"]))

    print("\n========== 完成 ==========")
    print("共处理 %d 条收藏" % stats["items"])
    print("  新保存文本: %d 条 | 跳过(已存在): %d 条" % (stats["new"], stats["skipped"]))
    print("  下载图片: %d 张 | 跳过已有: %d 张 | 失败: %d 张" %
          (stats["images"], stats["image_skipped"], stats["image_failed"]))
    if stats["failed"]:
        print("失败项:")
        for mid, err in stats["failed"]:
            print("  mid=%s: %s" % (mid, err))
    print("输出目录: %s" % base)


if __name__ == "__main__":
    main()
