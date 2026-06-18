import atexit
import json
import logging
import os
import random
import re
import signal
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

# ============ 共用設定 ============
# 預設 Telegram 設定（GitHub Actions 會用 Secrets 覆蓋，更安全）
TG_BOT_TOKEN = os.environ.get(
    "TG_BOT_TOKEN", "7971964410:AAFlyZHnvihiGFWRtW7AYbBuJyRYIvjG8QI"
)

DEFAULT_CHAT_IDS = {"425203130": "我自己", "627158297": "Lok", "7146407783": "Ray Fung"}
TG_CHAT_IDS = DEFAULT_CHAT_IDS
env_chat_ids = os.environ.get("TG_CHAT_IDS")
if env_chat_ids:
    try:
        TG_CHAT_IDS = json.loads(env_chat_ids)
    except Exception:
        TG_CHAT_IDS = DEFAULT_CHAT_IDS

# 🧪 測試開關：True = 即刻列晒兩邊現貨一次就停（本機用）
TEST_MODE = False

# 監察頻率（本機 loop 用；GitHub Actions 由 cron 控制，呢度唔生效）
CHECK_INTERVAL_MIN = 5
JITTER_SEC = 90
PAGE_DELAY = (2, 5)

# Telegram 單一訊息字數上限約 4096，留啲緩衝
TG_MSG_MAX = 4000

# ---- 來源 1：Hobbyland ----
API_URL = "https://backend.hobbylandeshop.com/api/products"
SITE = "https://www.hobbylandeshop.com"
STATE_FILE = Path("seen_products.json")

# ---- 來源 2：Toys"R"Us HK（Salesforce Commerce Cloud / SFRA）----
TRU_BASE = "https://www.toysrus.com.hk"
TRU_PREORDER_URL = TRU_BASE + "/en-hk/whats-on/new-arrivals/pre-order/"
TRU_STATE_FILE = Path("seen_toysrus.json")
# 只想收陀螺相關 → 留住關鍵字；想收晒成個 pre-order → 設成空 tuple ()
TRU_KEYWORDS = ("beyblade", "陀螺", "takara tomy", "bey", "ベイ")

# ============ Logging ============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("monitor.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# 共用 session
session = requests.Session()
HEADERS = {
    "Content-Type": "application/json",
    "Origin": SITE,
    "Referer": SITE + "/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}
TRU_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-HK,en;q=0.9,zh-HK;q=0.8,zh;q=0.7",
    "Referer": TRU_BASE + "/en-hk/",
}


# ============ 狀態記憶（支援多個 state file）============
def load_seen(path=STATE_FILE):
    if path.exists():
        try:
            return set(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_seen(skus, path=STATE_FILE):
    path.write_text(
        json.dumps(sorted(skus), ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ============ Telegram ============
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    for chat_id in TG_CHAT_IDS:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            r = requests.post(url, json=payload, timeout=15)
            r.raise_for_status()
        except Exception as e:
            log.error(f"Telegram 發送失敗 (chat_id={chat_id}): {e}")
        time.sleep(0.5)


def send_telegram_chunks(blocks, header=""):
    buf = header
    for block in blocks:
        if buf and len(buf) + len(block) + 2 > TG_MSG_MAX:
            send_telegram(buf)
            time.sleep(1)
            buf = ""
        buf += ("\n\n" if buf else "") + block
    if buf.strip():
        send_telegram(buf)


def format_item(p, tag="🆕"):
    limit = "無限制" if not p["sale_limit"] else f"{p['sale_limit']} 件"
    return (
        f"{tag} <b>{p['title']}</b>\n"
        f"類型: {p['sell_type']}\n"
        f"價錢: ${p['price']} (原價 ${p['regular_price']})\n"
        f"庫存: {p['stock']} | 限購: {limit}\n"
        f"{p['url']}"
    )


# ============ 來源 1：Hobbyland 抓取 ============
def fetch_page(page, category, stock_status):
    payload = {"page": page, "category": category, "stockStatus": stock_status}
    resp = session.post(API_URL, json=payload, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_all_products(category=None, stock_status="in_stock"):
    if category is None:
        category = ["takaratomy", "beyblade陀螺"]
    all_items = []
    page = 1
    while True:
        data = fetch_page(page, category, stock_status)["data"]
        all_items.extend(data["list"])
        if page >= data["total_pages"]:
            break
        page += 1
        time.sleep(random.uniform(*PAGE_DELAY))
    return all_items


def normalize(item):
    return {
        "title": item["title"],
        "sku": item["sku"],
        "price": item["price"],
        "regular_price": item.get("regular_price", ""),
        "url": SITE + quote(item["link"], safe="/"),
        "sell_type": item["sell_type"],
        "stock": item["stock"],
        "sale_limit": item.get("sale_limit", 0),
    }


# ============ 來源 2：Toys"R"Us 抓取（HTML scrape）============
def _tru_parse_tiles(soup):
    """由 HTML 抽出產品 tiles。SFRA 常見結構，如對唔到請睇下面 debug 段。"""
    tiles = []
    nodes = soup.select(
        "div.product-tile, div.product[data-pid], li.product, div.tile"
    )
    for node in nodes:
        pid = node.get("data-pid")
        if not pid:
            inner = node.select_one("[data-pid]")
            pid = inner.get("data-pid") if inner else None

        name_el = node.select_one(
            ".pdp-link a, .product-name a, a.link, .tile-body a, a.product-name"
        )
        title = name_el.get_text(strip=True) if name_el else ""
        href = name_el.get("href", "") if name_el else ""
        url = href if href.startswith("http") else (TRU_BASE + href if href else "")

        price_el = node.select_one(
            ".price .sales .value, .price .value, .sales .value, .price"
        )
        price = ""
        if price_el is not None:
            price = price_el.get("content") or price_el.get_text(strip=True)
            price = re.sub(r"[^\d.]", "", price)

        if not (title or pid):
            continue

        sku = pid or (url or title)
        tiles.append(
            {
                "title": title,
                "sku": f"tru:{sku}",  # 加前綴，確保唔同 Hobbyland 撞 key
                "price": price or "?",
                "regular_price": "",
                "url": url,
                "sell_type": "預訂 Pre-order",
                "stock": "-",
                "sale_limit": 0,
            }
        )
    return tiles


def fetch_toysrus_preorder():
    """揭晒所有分頁，回傳已 filter 嘅 list。"""
    seen_sku = set()
    items = []
    start = 0
    while True:
        params = {"start": start, "sz": 48}
        resp = session.get(
            TRU_PREORDER_URL, params=params, headers=TRU_HEADERS, timeout=20
        )
        resp.raise_for_status()
        tiles = _tru_parse_tiles(BeautifulSoup(resp.text, "lxml"))
        if not tiles:
            break

        new = 0
        for t in tiles:
            if t["sku"] not in seen_sku:
                seen_sku.add(t["sku"])
                items.append(t)
                new += 1
        if new == 0:  # 呢頁全部見過 → 到尾
            break

        start += len(tiles)
        if start > 2000:  # 安全掣
            break
        time.sleep(random.uniform(*PAGE_DELAY))

    if TRU_KEYWORDS:
        kw = tuple(k.lower() for k in TRU_KEYWORDS)
        items = [p for p in items if any(k in p["title"].lower() for k in kw)]
    return items


# ============ 🛑 停機通知（只本機 loop 用）============
_shutdown_sent = False


def send_shutdown_notice(reason="程式結束"):
    global _shutdown_sent
    if _shutdown_sent:
        return
    _shutdown_sent = True
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = (
        f"🔴 <b>陀螺監察機已停止運作</b>\n"
        f"原因: {reason}\n"
        f"時間: {now}\n"
        f"⚠️ 暫時唔會再收到新貨通知,請留意。"
    )
    try:
        send_telegram(msg)
        log.info(f"已發送停機通知({reason})")
    except Exception as e:
        log.error(f"發送停機通知失敗: {e}")


def _handle_signal(signum, frame):
    try:
        name = signal.Signals(signum).name
    except Exception:
        name = str(signum)
    send_shutdown_notice(reason=f"收到停止訊號 {name}")
    raise SystemExit(0)


def register_shutdown_hooks():
    atexit.register(send_shutdown_notice)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (ValueError, AttributeError):
        pass


# ============ 一次完整檢查（Hobbyland）============
def check_once():
    products = [normalize(it) for it in fetch_all_products()]
    current_skus = {p["sku"] for p in products}

    first_run = not STATE_FILE.exists()
    seen = load_seen(STATE_FILE)
    new_products = [p for p in products if p["sku"] not in seen]
    existing_products = [p for p in products if p["sku"] in seen]

    if first_run:
        log.info(f"[Hobbyland] 首次運行，建立基準({len(products)} 件)，唔出通知")
        save_seen(current_skus, STATE_FILE)
        return

    if new_products:
        log.info(f"[Hobbyland] 🎉 發現 {len(new_products)} 件新貨!")
        new_blocks = [format_item(p, "🆕") for p in new_products]
        existing_blocks = [format_item(p, "•") for p in existing_products]
        send_telegram_chunks(
            new_blocks + existing_blocks,
            header=f"🎉 <b>Hobbyland 發現 {len(new_products)} 件新貨!</b>\n"
            f"(🆕 新貨在上，• 現有貨品在下)\n",
        )
    else:
        log.info(f"[Hobbyland] 無新貨(現有 {len(products)} 件)")

    save_seen(seen | current_skus, STATE_FILE)


# ============ 一次完整檢查（Toys"R"Us）============
def check_toysrus_once():
    products = fetch_toysrus_preorder()
    current = {p["sku"] for p in products}

    first_run = not TRU_STATE_FILE.exists()
    seen = load_seen(TRU_STATE_FILE)
    new_products = [p for p in products if p["sku"] not in seen]

    if first_run:
        log.info(f"[ToysRUs] 首次運行，建立基準({len(products)} 件)，唔出通知")
        save_seen(current, TRU_STATE_FILE)
        return

    if new_products:
        log.info(f"[ToysRUs] 🎉 發現 {len(new_products)} 件新預訂!")
        blocks = [format_item(p, "🧸") for p in new_products]
        send_telegram_chunks(
            blocks,
            header=f"🧸 <b>Toys'R'Us 新預訂 {len(new_products)} 件!</b>\n",
        )
    else:
        log.info(f"[ToysRUs] 無新貨(現有 {len(products)} 件)")

    save_seen(seen | current, TRU_STATE_FILE)


# ============ 兩個來源一齊查（包獨立 try）============
def check_all_sources():
    try:
        check_once()
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        log.warning(f"[Hobbyland] HTTP 錯誤 {code}")
    except Exception as e:
        log.exception(f"[Hobbyland] 出事: {e}")

    try:
        check_toysrus_once()
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        log.warning(f"[ToysRUs] HTTP 錯誤 {code}")
    except Exception as e:
        log.exception(f"[ToysRUs] 出事: {e}")


# ============ 🧪 測試模式：兩邊都列一次 ============
def run_test():
    log.info("🧪 測試模式：列出兩邊現貨")
    # Hobbyland
    try:
        hb = [normalize(it) for it in fetch_all_products()]
        if hb:
            send_telegram_chunks(
                [format_item(p, "📦") for p in hb],
                header=f"🧪 <b>Hobbyland</b>：現有 {len(hb)} 件\n",
            )
        else:
            send_telegram("🧪 Hobbyland：暫時抓唔到任何現貨")
    except Exception as e:
        log.exception(f"測試 Hobbyland 出事: {e}")

    # Toys"R"Us
    try:
        tru = fetch_toysrus_preorder()
        if tru:
            send_telegram_chunks(
                [format_item(p, "🧸") for p in tru],
                header=f"🧪 <b>Toys'R'Us</b>：符合條件 {len(tru)} 件\n",
            )
        else:
            send_telegram("🧪 Toys'R'Us：暫時無符合關鍵字嘅預訂（或 selector 要校）")
    except Exception as e:
        log.exception(f"測試 ToysRUs 出事: {e}")


# ============ 主程式 ============
def main():
    # 🌟 GitHub Actions：行一次兩個來源就 exit
    if os.environ.get("GITHUB_ACTIONS") == "true":
        log.info("🤖 偵測到 GitHub Actions，執行單次檢查...")
        check_all_sources()
        return

    # 本機測試模式
    if TEST_MODE:
        log.info("陀螺監察機（測試模式）🌀")
        send_telegram("🤖 測試模式 [Test Mode]：列出兩邊現貨......")
        try:
            run_test()
        except Exception as e:
            log.exception(f"測試出事: {e}")
        return

    # 本機長駐 loop
    log.info("陀螺監察機啟動 🌀")
    register_shutdown_hooks()
    send_telegram("🤖 Beyblade X 陀螺預訂監察機已開機（Hobbyland + Toys'R'Us），有新貨即時通知!")

    while True:
        check_all_sources()
        wait = CHECK_INTERVAL_MIN * 60 + random.uniform(-JITTER_SEC, JITTER_SEC)
        wait = max(60, wait)
        log.info(f"下次檢查：{wait / 60:.1f} 分鐘後")
        time.sleep(wait)


if __name__ == "__main__":
    main()
