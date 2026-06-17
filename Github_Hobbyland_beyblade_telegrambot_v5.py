import atexit
import json
import logging
import os  # 新增：讀取環境變數
import random
import signal
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

# ============ 設定區(改呢度就得)============
API_URL = "https://backend.hobbylandeshop.com/api/products"
SITE = "https://www.hobbylandeshop.com"

# 預設 Telegram 設定（如果偵測到 GitHub 環境，會優先使用 GitHub Secrets 覆蓋，更安全！）
TG_BOT_TOKEN = os.environ.get(
    "TG_BOT_TOKEN", "7971964410:AAFlyZHnvihiGFWRtW7AYbBuJyRYIvjG8QI"
)

# 預設 Chat IDs
DEFAULT_CHAT_IDS = {"425203130": "我自己", "627158297": "Lok", "7146407783": "Ray Fung"}
TG_CHAT_IDS = DEFAULT_CHAT_IDS

# 嘗試從環境變數讀取 Chat IDs (GitHub Actions 用)
env_chat_ids = os.environ.get("TG_CHAT_IDS")
if env_chat_ids:
    try:
        TG_CHAT_IDS = json.loads(env_chat_ids)
    except Exception:
        TG_CHAT_IDS = DEFAULT_CHAT_IDS

# 🧪 測試開關:True = 即刻列晒所有現貨一次就停;測完記得改返 False
TEST_MODE = False

# 監察頻率(建議唔好低過 5 分鐘)
CHECK_INTERVAL_MIN = 5  # 每約 5 分鐘 check 一次
JITTER_SEC = 90  # 隨機 ±1.5 分鐘,避免太機械化
PAGE_DELAY = (2, 5)  # 每揭一頁隨機停 2~5 秒
STATE_FILE = Path("seen_products.json")

# Telegram 單一訊息字數上限約 4096,留啲緩衝
TG_MSG_MAX = 4000

# ============ Logging(方便你睇返佢做過咩)============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("monitor.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# 共用一個 session(似真人:同一個連線一路用)
session = requests.Session()
HEADERS = {
    "Content-Type": "application/json",
    "Origin": SITE,
    "Referer": SITE + "/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}


# ============ 1. 抓取 ============
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
        time.sleep(random.uniform(*PAGE_DELAY))  # 揭頁之間停一停,扮人
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


# ============ 2 & 4. 狀態記憶 ============
def load_seen():
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_seen(skus):
    STATE_FILE.write_text(
        json.dumps(sorted(skus), ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ============ 3. Telegram 通知 ============
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


# ============ 🛑 停機通知 ============
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


# ============ 🧪 測試模式:列出所有現貨 ============
def run_test():
    log.info("🧪 測試模式:列出所有現貨")
    products = [normalize(it) for it in fetch_all_products()]
    if not products:
        send_telegram("🧪 測試:暫時Scrap唔到任何現貨(可能真係冇貨)")
        log.info("測試:抓唔到任何貨品")
        return
    blocks = [format_item(p, "📦") for p in products]
    send_telegram_chunks(
        blocks,
        header=f"🧪 <b>測試模式</b>:現有 {len(products)} 件貨品\n",
    )
    log.info(f"測試完成,已列出 {len(products)} 件貨品")


# ============ 一次完整檢查 ============
def check_once():
    products = [normalize(it) for it in fetch_all_products()]
    current_skus = {p["sku"] for p in products}

    first_run = not STATE_FILE.exists()
    seen = load_seen()
    new_products = [p for p in products if p["sku"] not in seen]
    existing_products = [p for p in products if p["sku"] in seen]

    if first_run:
        log.info(f"首次運行,建立基準({len(products)} 件),今次唔出通知")
        save_seen(current_skus)
        return

    if new_products:
        log.info(f"🎉 發現 {len(new_products)} 件新貨!")
        new_blocks = [format_item(p, "🆕") for p in new_products]
        existing_blocks = [format_item(p, "•") for p in existing_products]
        send_telegram_chunks(
            new_blocks + existing_blocks,
            header=f"🎉 <b>發現 {len(new_products)} 件新貨!</b>\n"
            f"(🆕 新貨在上,• 現有貨品在下)\n",
        )
    else:
        log.info(f"無新貨(現有 {len(products)} 件)")

    save_seen(seen | current_skus)


# ============ 主迴圈 ============
def main():
    # 🌟 新增：如果偵測到在 GitHub Actions 運行，直接執行單次檢查並退出
    if os.environ.get("GITHUB_ACTIONS") == "true":
        log.info("🤖 偵測到在 GitHub Actions 運行，執行單次檢查...")
        try:
            check_once()
        except Exception as e:
            log.exception(f"GitHub Actions 執行失敗: {e}")
        return

    # 以下為本機（Local）運行的邏輯，保持不變
    if TEST_MODE:
        log.info("陀螺監察機(測試模式)🌀")
        send_telegram("🤖 測試模式 [Test Mode] : 陀螺監察機開機,而家列出所有現貨......")
        try:
            run_test()
        except Exception as e:
            log.exception(f"測試出事: {e}")
        return

    log.info("陀螺監察機啟動 🌀")
    register_shutdown_hooks()
    send_telegram("🤖 Beyblade X 陀螺預訂監察機已開機,有新貨會即時通知你!")

    while True:
        try:
            check_once()
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            log.warning(f"HTTP 錯誤 {code}")
            if code == 429:
                log.warning("被限速(429),停 10 分鐘冷靜下")
                time.sleep(600)
        except Exception as e:
            log.exception(f"出事: {e}")

        wait = CHECK_INTERVAL_MIN * 60 + random.uniform(-JITTER_SEC, JITTER_SEC)
        wait = max(60, wait)
        log.info(f"下次檢查:{wait / 60:.1f} 分鐘後")
        time.sleep(wait)


if __name__ == "__main__":
    main()
