#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Hanime1.me 视频爬虫（旧片优先，每天5部）
- 强化直链提取多重备用 + 请求重试
- 标题第一行：下载页 h3 繁体→简体→清理
- 封面优先下载页海报
"""

import os, re, sys, time, tempfile, json, logging
from typing import Optional, Set, List, Dict

import cloudscraper
import requests as req
from bs4 import BeautifulSoup
from pyrogram import Client
from pyrogram.errors import FloodWait, RPCError
import zhconv

# ---------- 配置 ----------
BASE_URL = "https://hanime1.me"
SEARCH_URL = f"{BASE_URL}/search?genre=裏番&page="

# 环境变量校验
REQUIRED_ENV_VARS = ["CHAT_ID", "API_ID", "API_HASH", "SESSION_STRING"]

_missing = [k for k in REQUIRED_ENV_VARS if k not in os.environ]
if _missing:
    sys.exit(f"❌ 缺少必需的环境变量: {', '.join(_missing)}\n"
             f"   请在 GitHub Secrets 或环境中设置以上变量后重试。")

CHAT_ID = os.environ["CHAT_ID"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
PROXY = os.environ.get("PROXY", "")  # 可选，格式: http://user:pass@host:port 或 http://host:port

REQUEST_DELAY = 2
MAX_VIDEOS_PER_RUN = 5
SEEN_FILE = "seen.txt"
STATE_FILE = "state.json"

MAX_RETRIES = 3       # 网络请求最大重试次数
MAX_FAIL_COUNT = 3    # 同一视频连续失败上限，超过则永久跳过
SEEN_WARN_THRESHOLD = 5000  # seen.txt 条目数超过此值打印警告

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


# ---------- 环境变量诊断日志 ----------
logger.info(f"CHAT_ID: {CHAT_ID}")
logger.info(f"PROXY 已{'设置' if PROXY else '未设置'}")

# ---------- 全局 cloudscraper 实例（复用） ----------
_scraper = None

def get_scraper() -> cloudscraper.CloudScraper:
    """返回复用的 cloudscraper 实例（单例）"""
    global _scraper
    if _scraper is None:
        _scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False},
            delay=15,
        )
        logger.info("cloudscraper 实例已创建（全局复用）")
    return _scraper


# ---------- 工具函数 ----------
def load_seen_ids() -> Set[str]:
    seen = set()
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            for line in f:
                vid = line.strip()
                if vid:
                    seen.add(vid)
    if len(seen) > SEEN_WARN_THRESHOLD:
        logger.warning(f"seen.txt 已超过 {SEEN_WARN_THRESHOLD} 条记录（当前 {len(seen)}），建议清理旧条目。")
    return seen

def save_seen_ids(seen: Set[str]):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        for vid in sorted(seen):
            f.write(vid + "\n")

def extract_video_id(url: str) -> str:
    m = re.search(r'v=(\d+)', url)
    return m.group(1) if m else ""

def get_soup(url: str, retries=MAX_RETRIES) -> BeautifulSoup:
    """带重试的 cloudscraper 请求"""
    last_exc = None
    scraper = get_scraper()
    for attempt in range(1, retries + 1):
        try:
            kwargs = {"timeout": 40}
            if PROXY:
                kwargs["proxies"] = {"http": PROXY, "https": PROXY}
            resp = scraper.get(url, **kwargs)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, 'html.parser')
        except Exception as e:
            logger.warning(f"请求失败 (尝试 {attempt}/{retries}): {url}, 错误: {e}")
            last_exc = e
            time.sleep(5)
    raise last_exc

def get_max_page() -> int:
    url = SEARCH_URL + "1"
    soup = get_soup(url)
    pagination = soup.find('ul', class_='pagination')
    if not pagination:
        return 1
    max_p = 1
    for a in pagination.find_all('a', class_='page-link'):
        href = a.get('href', '')
        m = re.search(r'page=(\d+)', href)
        if m:
            p = int(m.group(1))
            if p > max_p:
                max_p = p
    return max_p

def load_state(default_page: int) -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {
            "current_page": default_page,
            "processed_in_page": 0,
            "failed_videos": {},
        }
    # 确保旧 state 格式兼容
    state.setdefault("failed_videos", {})
    return state

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)

def _safe_unlink(path: str):
    """安全删除临时文件，忽略权限等错误"""
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except Exception as e:
        logger.warning(f"清理临时文件失败: {path}, 错误: {e}")


# ---------- 标题清理 ----------
def clean_title(text: str) -> str:
    text = re.sub(r'\[.+?\]', '', text)
    text = text.replace('～', '').replace('~', '')
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def clean_tags(word: str) -> str:
    return re.sub(r'[^\w一-鿿]', '', word)


# ---------- 搜索页解析 ----------
def parse_search_page(html: str) -> List[dict]:
    soup = BeautifulSoup(html, 'html.parser')
    cards = soup.select('a[href*="/watch?v="]')
    videos = []
    for card in cards:
        href = card.get('href')
        if not href:
            continue
        if href.startswith('/watch?v='):
            full_url = BASE_URL + href
        elif href.startswith('https://hanime1.me/watch?v='):
            full_url = href
        else:
            continue
        img = card.find('img')
        cover = img.get('src') if img else ""
        if cover and cover.startswith('//'):
            cover = 'https:' + cover
        videos.append({'video_url': full_url, 'cover_url': cover})
    return videos


# ---------- 核心：多重方式提取视频直链 + 重试 ----------
def extract_video_url_from_download_page(video_id: str) -> str:
    """从下载页提取直链，多重尝试"""
    url = f"{BASE_URL}/download?v={video_id}"
    scraper = get_scraper()

    # 方式1：解析 data-url
    try:
        soup = get_soup(url)
        download_links = soup.select('a.juicyads-popunder[data-url]')
        if download_links:
            data_url = download_links[0].get('data-url', '')
            if data_url:
                if data_url.startswith('//'): data_url = 'https:' + data_url
                return data_url
    except Exception as e:
        logger.warning(f"下载页 data-url 提取失败: {e}")

    # 方式2：查找 href 中的 .mp4
    try:
        soup = get_soup(url)
        for link in soup.find_all('a', href=re.compile(r'\.mp4')):
            href = link.get('href')
            if href.startswith('//'): href = 'https:' + href
            return href
    except Exception as e:
        logger.warning(f"下载页 mp4 链接提取失败: {e}")

    # 方式3：直接请求下载页，获取重定向地址
    try:
        kwargs = {"timeout": 15, "allow_redirects": False}
        if PROXY:
            kwargs["proxies"] = {"http": PROXY, "https": PROXY}
        resp = scraper.get(url, **kwargs)
        if resp.status_code in (301, 302, 303):
            loc = resp.headers.get('Location', '')
            if loc:
                if loc.startswith('//'): loc = 'https:' + loc
                return loc
    except Exception as e:
        logger.warning(f"下载页重定向提取失败: {e}")

    return ""

def extract_video_url_from_watch_page(video_url: str) -> str:
    """从观看页提取直链，多重尝试"""
    try:
        soup = get_soup(video_url)
        video_tag = soup.find('video', id='player')
        if video_tag:
            # 优先 source 标签
            sources = video_tag.find_all('source')
            best_url, best_size = "", 0
            for src in sources:
                u = src.get('src', '')
                sz_str = src.get('size', '0')
                try:
                    sz = int(sz_str)
                except:
                    sz = 0
                if sz > best_size:
                    best_size = sz
                    best_url = u
            if best_url:
                if best_url.startswith('//'): best_url = 'https:' + best_url
                return best_url
            # 其次 video 标签的 src
            vid_src = video_tag.get('src', '')
            if vid_src:
                if vid_src.startswith('//'): vid_src = 'https:' + vid_src
                return vid_src
    except Exception as e:
        logger.warning(f"观看页提取失败: {e}")
    return ""


def get_best_video_url(video_id: str, video_url: str) -> str:
    """综合多种方式获取最高清视频直链"""
    scraper = get_scraper()

    # 先尝试下载页
    url = extract_video_url_from_download_page(video_id)
    if url:
        logger.info("从下载页获得直链")
        return url

    # 再尝试观看页
    url = extract_video_url_from_watch_page(video_url)
    if url:
        logger.info("从观看页获得直链")
        return url

    # 最后尝试直接请求下载页并跟随重定向到最终 URL
    try:
        download_url = f"{BASE_URL}/download?v={video_id}"
        kwargs = {"timeout": 30}
        if PROXY:
            kwargs["proxies"] = {"http": PROXY, "https": PROXY}
        resp = scraper.get(download_url, **kwargs)
        final_url = resp.url
        if final_url and 'hembed.com' in final_url:
            return final_url
    except:
        pass

    raise ValueError("所有方式均无法获取视频直链")


def parse_video_page_and_download(video_id: str, video_url: str) -> tuple:
    """
    返回: (原始繁体标题, 海报URL, 发布日期, 视频直链)
    优先使用下载页提取元数据，失败回退观看页
    """
    # 下载页优先
    download_url = f"{BASE_URL}/download?v={video_id}"
    raw_title = poster_url = date_str = ""
    try:
        soup = get_soup(download_url)
        # 标题
        h3 = soup.find('h3')
        if h3:
            raw_title = h3.get_text(strip=True)
        # 海报
        img = soup.find('img', class_='download-image')
        if img:
            p = img.get('src', '')
            if p and p.startswith('//'): p = 'https:' + p
            poster_url = p
        # 日期
        date_el = soup.find('p', style=re.compile(r'font-size: 12px'))
        if date_el:
            m = re.search(r'(\d{4}-\d{2}-\d{2})', date_el.get_text())
            if m:
                date_str = m.group(1)
        if not date_str:
            # 全页扫描
            all_text = soup.get_text()
            m = re.search(r'(\d{4}-\d{2}-\d{2})', all_text)
            if m:
                date_str = m.group(1)
        if raw_title:
            logger.info("从下载页获得元数据")
            # 获取直链
            best_url = get_best_video_url(video_id, video_url)
            return raw_title, poster_url, date_str, best_url
    except Exception as e:
        logger.warning(f"下载页元数据提取失败，回退观看页: {e}")

    # 回退：观看页
    soup = get_soup(video_url)
    h3 = soup.find('h3', class_='video-details-wrapper')
    if h3:
        raw_title = h3.get_text(strip=True)
    else:
        title_tag = soup.find('title')
        raw_title = title_tag.get_text(strip=True) if title_tag else "未知标题"
        if ' - Hanime1.me' in raw_title:
            raw_title = raw_title.split(' - Hanime1.me')[0].strip()

    # 海报
    if not poster_url:
        video_tag = soup.find('video', id='player')
        if video_tag:
            p = video_tag.get('poster', '')
            if p and p.startswith('//'): p = 'https:' + p
            poster_url = p

    # 日期
    if not date_str:
        desc_div = soup.find('div', class_='video-description-panel-hover') or soup.find('div', class_='video-description-panel')
        if desc_div:
            text = desc_div.get_text()
            match = re.search(r'(\d{4}-\d{2}-\d{2})', text)
            if match:
                date_str = match.group(1)
        if not date_str:
            for tag in soup.find_all(text=re.compile(r'\d{4}-\d{2}-\d{2}')):
                m = re.search(r'(\d{4}-\d{2}-\d{2})', tag)
                if m:
                    date_str = m.group(1)
                    break

    best_url = get_best_video_url(video_id, video_url)
    return raw_title, poster_url, date_str, best_url


# ---------- 下载与发送 ----------
def download_file(url: str, referer: str = BASE_URL, timeout: int = 120, retries=3) -> tempfile.NamedTemporaryFile:
    """带重试的文件下载"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9",
        "Referer": referer,
    }
    last_exc = None
    for i in range(1, retries + 1):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4')
        try:
            timeout_conn = min(timeout, 30)
            timeout_read = timeout
            with req.get(url, headers=headers, stream=True, timeout=(timeout_conn, timeout_read)) as r:
                r.raise_for_status()
                for chunk in r.iter_content(8192):
                    if chunk:
                        tmp.write(chunk)
            tmp.flush(); tmp.seek(0)
            return tmp
        except Exception as e:
            tmp.close()
            _safe_unlink(tmp.name)
            logger.warning(f"下载失败 (尝试 {i}/{retries}): {e}")
            last_exc = e
            time.sleep(5)
    raise last_exc

def send_video_pyrogram(video_path: str, thumb_path: Optional[str], caption: str):
    app = Client(":memory:", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)
    try:
        with app:
            while True:
                try:
                    app.send_video(
                        chat_id=CHAT_ID,
                        video=video_path,
                        caption=caption,
                        thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None,
                        supports_streaming=True,
                    )
                    logger.info("视频发送成功")
                    break
                except FloodWait as e:
                    logger.warning(f"限流等待 {e.value} 秒")
                    time.sleep(e.value)
                except RPCError as e:
                    if "CHAT_WRITE_FORBIDDEN" in str(e):
                        logger.error("权限不足！请检查：1) 账号是否为频道管理员？2) SESSION_STRING 是否正确？3) CHAT_ID 格式是否正确？")
                    raise
    except Exception as e:
        logger.error(f"发送失败: {e}")
        raise


# ---------- 处理单个视频 ----------
def process_video(video_info: dict) -> Optional[str]:
    video_url = video_info['video_url']
    cover_fallback = video_info.get('cover_url', '')
    vid = extract_video_id(video_url)
    logger.info(f"处理视频 ID={vid}")

    video_tmp = None
    thumb_path = None

    try:
        raw_title, poster_url, date_str, best_video_url = parse_video_page_and_download(vid, video_url)

        # 标题构建
        line1 = clean_title(zhconv.convert(raw_title, 'zh-cn'))
        line2 = clean_title(raw_title)
        words = line1.split()
        tags = [f"#{clean_tags(w)}" for w in words if clean_tags(w) and not clean_tags(w).isdigit()]
        line3 = ' '.join(tags)
        if date_str:
            parts = date_str.split('-')
            if len(parts) == 3:
                line4 = f"#{parts[0]}年{parts[1]}月"
            else:
                line4 = date_str
        else:
            line4 = ""
        caption = f"{line1}\n{line2}\n{line3}\n{line4}".strip()

        final_cover = poster_url if poster_url else cover_fallback
        video_tmp = download_file(best_video_url, referer=video_url)

        if final_cover:
            try:
                thumb_tmp = download_file(final_cover, referer=video_url)
                thumb_path = thumb_tmp.name + '.jpg'
                os.rename(thumb_tmp.name, thumb_path)
            except Exception as e:
                logger.warning(f"封面下载失败: {e}")

        send_video_pyrogram(video_tmp.name, thumb_path, caption)

        logger.info(f"视频处理完成: {line1}")
        return vid
    except Exception as e:
        logger.error(f"处理失败 {vid}: {e}")
        return None
    finally:
        _safe_unlink(video_tmp.name if video_tmp else None)
        _safe_unlink(thumb_path)


# ---------- 主流程 ----------
def main():
    logger.info("====== Hanime1 -> Telegram 每日旧片发布 ======")
    seen_ids = load_seen_ids()
    total_pages = get_max_page()
    logger.info(f"网站总页数: {total_pages}")

    state = load_state(total_pages)
    current_page = state['current_page']
    processed_in_page = state['processed_in_page']
    failed_videos: Dict[str, int] = state.get('failed_videos', {})

    # 页码边界修正
    if current_page > total_pages or current_page < 1:
        current_page = total_pages
        processed_in_page = 0

    videos_to_send = []
    temp_seen = set()

    while len(videos_to_send) < MAX_VIDEOS_PER_RUN and current_page >= 1:
        page_url = SEARCH_URL + str(current_page)
        try:
            page_soup = get_soup(page_url)
            page_videos = parse_search_page(str(page_soup))
        except Exception as e:
            logger.warning(f"无法访问第 {current_page} 页，跳过。错误: {e}")
            current_page -= 1
            processed_in_page = 0
            continue

        total_on_page = len(page_videos)
        start_idx = total_on_page - 1 - processed_in_page

        while start_idx >= 0 and len(videos_to_send) < MAX_VIDEOS_PER_RUN:
            v = page_videos[start_idx]
            vid = extract_video_id(v['video_url'])

            # 跳过已失败超过上限的视频
            if vid and vid in failed_videos and failed_videos[vid] >= MAX_FAIL_COUNT:
                logger.info(f"跳过永久失败视频 {vid}（已失败 {failed_videos[vid]} 次）")
                seen_ids.add(vid)

            if vid and vid not in seen_ids and vid not in temp_seen:
                videos_to_send.append(v)
                temp_seen.add(vid)
            processed_in_page += 1
            start_idx -= 1

        if start_idx < 0:
            current_page -= 1
            processed_in_page = 0
        else:
            break

    # 到达第 1 页且无新视频 → 回到最后一页重新扫描
    if not videos_to_send and current_page < 1:
        logger.info(f"已达第 1 页且无新视频，重置到第 {total_pages} 页重新扫描。")
        current_page = total_pages
        processed_in_page = 0
        failed_videos = {k: v for k, v in failed_videos.items() if v < MAX_FAIL_COUNT and k not in seen_ids}
        state['failed_videos'] = failed_videos
        save_state(state)
        return

    if not videos_to_send:
        logger.info("没有新视频需要发送。")
        state['current_page'] = current_page
        state['processed_in_page'] = processed_in_page
        save_state(state)
        return

    logger.info(f"找到 {len(videos_to_send)} 部新视频，开始发送...")
    count = 0
    for v in videos_to_send:
        vid = process_video(v)
        if vid:
            seen_ids.add(vid)
            failed_videos.pop(vid, None)
            count += 1
        else:
            vid_raw = extract_video_id(v['video_url'])
            if vid_raw:
                failed_videos[vid_raw] = failed_videos.get(vid_raw, 0) + 1
                logger.warning(f"视频 {vid_raw} 失败次数: {failed_videos[vid_raw]}/{MAX_FAIL_COUNT}")
        time.sleep(REQUEST_DELAY)

    save_seen_ids(seen_ids)
    state['current_page'] = current_page
    state['processed_in_page'] = processed_in_page
    state['failed_videos'] = failed_videos
    save_state(state)
    logger.info(f"本次成功发送 {count} 部视频，任务结束。")


if __name__ == "__main__":
    main()
