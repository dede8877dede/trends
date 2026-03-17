#!/usr/bin/env python3
"""
One-shot viral dance/lipsync check — runs once and exits.
Designed for GitHub Actions (called on schedule).
State is persisted via GitHub Actions artifacts.
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path

from telegram import Bot
from telegram.error import TelegramError

TELEGRAM_TOKEN        = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "8151600713")
VIRAL_MIN_VIEWS       = int(os.environ.get("VIRAL_MIN_VIEWS",     "1000000"))
VIRAL_MIN_LIKES_RATIO = float(os.environ.get("VIRAL_MIN_LIKES_RATIO", "0.03"))
MAX_AGE_HOURS         = int(os.environ.get("MAX_AGE_HOURS",       "168"))
MIN_GROWTH_VPH        = int(os.environ.get("MIN_GROWTH_VPH",      "50000"))

BASE_DIR   = Path(__file__).parent
SEEN_FILE  = BASE_DIR / "seen_videos.json"
STATS_FILE = BASE_DIR / "video_stats.json"

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

DANCE_KEYWORDS = {
    "dance", "dancing", "dancer", "dancechallenge", "dancetrend",
    "choreography", "choreo", "lipsync", "lip sync", "lipsyncing",
    "shuffle", "twerk", "breakdance", "duet",
    "танец", "танцы", "танцует", "хореография", "дуэт", "липсинк", "флешмоб",
    "#dance", "#lipsync", "#choreo", "#dancechallenge", "#танец",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ── State ──────────────────────────────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)[-10000:]), encoding="utf-8")


def load_stats() -> dict:
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_stats(stats: dict):
    cutoff = time.time() - 96 * 3600
    pruned = {k: v for k, v in stats.items() if v.get("ts", 0) > cutoff}
    STATS_FILE.write_text(json.dumps(pruned), encoding="utf-8")


# ── Detection ──────────────────────────────────────────────────────────────────

def is_dance_or_lipsync(video: dict) -> bool:
    text = " ".join([
        video.get("description", ""),
        video.get("music", ""),
        *video.get("challenges", []),
    ]).lower()
    return any(kw in text for kw in DANCE_KEYWORDS)


def evaluate_video(video: dict, stats: dict):
    views   = video.get("views", 0)
    likes   = video.get("likes", 0)
    vid_id  = video["id"]
    now     = time.time()
    likes_ok = views > 0 and (likes / views) >= VIRAL_MIN_LIKES_RATIO

    if vid_id in stats:
        prev = stats[vid_id]
        dt_h = (now - prev["ts"]) / 3600
        if dt_h >= 0.1:
            growth = (views - prev["views"]) / dt_h
            stats[vid_id] = {"views": views, "likes": likes, "ts": now}
            if growth >= MIN_GROWTH_VPH and likes_ok:
                return True, f"📈 +{growth / 1000:.0f}K views/h"
        stats[vid_id]["views"] = views
        stats[vid_id]["likes"] = likes
        return False, None

    stats[vid_id] = {"views": views, "likes": likes, "ts": now}
    ct = video.get("create_time", 0)
    if ct:
        age_h = (now - ct) / 3600
        if age_h <= MAX_AGE_HOURS and views >= VIRAL_MIN_VIEWS and likes_ok:
            return True, f"🆕 {age_h:.0f}h ago · {views / 1_000_000:.1f}M views"
    return False, None


# ── Playwright helper ──────────────────────────────────────────────────────────

async def make_page(p):
    from playwright_stealth import Stealth
    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"],
    )
    ctx = await browser.new_context(
        user_agent=BROWSER_UA,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
    )
    page = await ctx.new_page()
    await Stealth().apply_stealth_async(page)
    return browser, page


# ── TikTok ─────────────────────────────────────────────────────────────────────

def _parse_tt(item: dict) -> dict | None:
    vid_id = item.get("id", item.get("aweme_id", ""))
    if not vid_id:
        return None
    author = (item.get("author", {}).get("uniqueId")
              or item.get("author", {}).get("unique_id", "unknown"))
    desc   = (item.get("desc", "") or "")[:200]
    stats  = item.get("stats", item.get("statistics", {}))
    music  = item.get("music", {}).get("title", "")
    challs = [c.get("title", "") for c in item.get("challengeInfoList", [])]
    ct     = item.get("createTime", item.get("create_time", 0))
    return {
        "id":          f"tt_{vid_id}",
        "platform":    "TikTok",
        "url":         f"https://www.tiktok.com/@{author}/video/{vid_id}",
        "description": desc,
        "music":       music,
        "challenges":  challs,
        "views":       stats.get("playCount",    stats.get("play_count", 0)),
        "likes":       stats.get("diggCount",    stats.get("digg_count", 0)),
        "comments":    stats.get("commentCount", stats.get("comment_count", 0)),
        "shares":      stats.get("shareCount",   stats.get("share_count", 0)),
        "author":      author,
        "create_time": int(ct) if ct else 0,
    }


async def fetch_tiktok() -> list[dict]:
    from playwright.async_api import async_playwright

    captured: list = []

    async def handle(response):
        url = response.url
        if ("item_list" in url or "recommend" in url) and "tiktok.com" in url:
            try:
                body  = await response.body()
                data  = json.loads(body)
                items = data.get("itemList", data.get("aweme_list", []))
                if items:
                    captured.extend(items)
                    log.info(f"TikTok: +{len(items)} items")
            except Exception:
                pass

    try:
        async with async_playwright() as p:
            browser, page = await make_page(p)
            page.on("response", handle)
            await page.goto("https://www.tiktok.com/explore",
                            wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(8)
            for _ in range(5):
                await page.evaluate("window.scrollBy(0, 700)")
                await asyncio.sleep(2)
            await browser.close()
    except Exception as e:
        log.error(f"TikTok error: {e}")
        return []

    seen_ids: set = set()
    videos: list = []
    for item in captured:
        v = _parse_tt(item)
        if v and v["id"] not in seen_ids:
            seen_ids.add(v["id"])
            videos.append(v)
    log.info(f"TikTok: {len(videos)} unique videos")
    return videos


# ── Instagram ──────────────────────────────────────────────────────────────────

async def fetch_instagram() -> list[dict]:
    from playwright.async_api import async_playwright

    raw: list = []

    async def handle(response):
        url = response.url
        if "instagram.com" in url and ("graphql" in url or "clips" in url):
            try:
                body = await response.body()
                text = body.decode("utf-8", errors="ignore")
                if '"play_count"' in text or '"video_view_count"' in text:
                    raw.append(json.loads(text))
            except Exception:
                pass

    try:
        async with async_playwright() as p:
            browser, page = await make_page(p)
            page.on("response", handle)
            await page.goto("https://www.instagram.com/reels/",
                            wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(8)
            for _ in range(3):
                await page.evaluate("window.scrollBy(0, 600)")
                await asyncio.sleep(2)
            await browser.close()
    except Exception as e:
        log.error(f"Instagram error: {e}")
        return []

    videos: list = []

    def _extract(obj, depth=0):
        if depth > 12 or not isinstance(obj, dict):
            return
        sc     = obj.get("shortcode") or obj.get("code")
        is_vid = obj.get("is_video") or obj.get("media_type") in (2, "VIDEO")
        if sc and is_vid:
            views   = obj.get("video_view_count") or obj.get("play_count") or 0
            likes   = (obj.get("edge_media_preview_like", {}).get("count")
                       or obj.get("like_count") or 0)
            comments = (obj.get("edge_media_to_comment", {}).get("count")
                        or obj.get("comment_count") or 0)
            owner   = obj.get("owner", {}).get("username") or "unknown"
            cap_e   = obj.get("edge_media_to_caption", {}).get("edges", [])
            caption = (cap_e[0].get("node", {}).get("text", "")
                       if cap_e else (obj.get("caption") or {}).get("text", ""))
            taken_at = obj.get("taken_at_timestamp") or obj.get("taken_at") or 0
            music   = (obj.get("clips_metadata", {})
                          .get("original_sound_info", {})
                          .get("original_audio_title", ""))
            videos.append({
                "id":          f"ig_{sc}",
                "platform":    "Instagram",
                "url":         f"https://www.instagram.com/p/{sc}/",
                "description": (caption or "")[:200],
                "music":       music,
                "challenges":  [],
                "views":       views,
                "likes":       likes,
                "comments":    comments,
                "shares":      0,
                "author":      owner,
                "create_time": int(taken_at) if taken_at else 0,
            })
        for v in obj.values():
            if isinstance(v, dict):
                _extract(v, depth + 1)
            elif isinstance(v, list):
                for i in v:
                    _extract(i, depth + 1)

    for data in raw:
        _extract(data)

    seen_sc: set = set()
    unique = []
    for v in videos:
        if v["id"] not in seen_sc:
            seen_sc.add(v["id"])
            unique.append(v)
    log.info(f"Instagram: {len(unique)} unique videos")
    return unique


# ── Notify ─────────────────────────────────────────────────────────────────────

async def notify(bot: Bot, video: dict, reason: str):
    emoji     = "🎵" if video["platform"] == "TikTok" else "📸"
    views     = video["views"]
    likes     = video["likes"]
    views_str = f"{views / 1_000_000:.1f}M" if views >= 1_000_000 else f"{views / 1_000:.0f}K"
    likes_str = f"{likes / 1_000_000:.1f}M" if likes >= 1_000_000 else f"{likes / 1_000:.0f}K"

    lines = [
        f"{emoji} <b>Viral {video['platform']}!</b>  {reason}",
        f"👤 @{video['author']}",
    ]
    if video["description"]:
        lines.append(f"📝 {video['description']}")
    if video["music"]:
        lines.append(f"🎶 {video['music']}")
    lines += ["",
              f"👁 {views_str} просмотров",
              f"❤️ {likes_str} лайков",
              f"💬 {video['comments']:,} комментариев",
              "",
              f"🔗 {video['url']}"]
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text="\n".join(lines),
        parse_mode="HTML",
    )
    log.info(f"Notified: {video['id']} — {reason}")


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    if not TELEGRAM_TOKEN:
        log.error("TELEGRAM_TOKEN not set!")
        return

    bot   = Bot(token=TELEGRAM_TOKEN)
    seen  = load_seen()
    stats = load_stats()
    found = 0

    tiktok    = await fetch_tiktok()
    instagram = await fetch_instagram()
    all_vids  = tiktok + instagram
    log.info(f"Total: {len(all_vids)} | Dance matches: {sum(1 for v in all_vids if is_dance_or_lipsync(v))}")

    for video in all_vids:
        if video["id"] in seen:
            continue
        if not is_dance_or_lipsync(video):
            continue
        should_notify, reason = evaluate_video(video, stats)
        if should_notify:
            try:
                await notify(bot, video, reason)
                seen.add(video["id"])
                found += 1
                await asyncio.sleep(1)
            except TelegramError as e:
                log.error(f"Telegram error: {e}")

    save_seen(seen)
    save_stats(stats)
    log.info(f"Done. Viral dance videos found: {found}")


if __name__ == "__main__":
    asyncio.run(main())
