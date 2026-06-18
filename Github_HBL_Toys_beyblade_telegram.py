"""
Beyblade 預訂監察機（Hobbyland + Toys"R"Us HK）
================================================
功能：定時檢查兩個網站嘅 Beyblade / Takara Tomy 陀螺新貨／預訂，有新貨就 Telegram 通知。

需要嘅環境變數（Environment Variables）：
  TG_BOT_TOKEN  : Telegram bot token（必填）
  TG_CHAT_IDS   : 收通知嘅 chat id，JSON 格式。可以係：
                    - 陣列：[123456789, -1001234567890]
                    - 物件：{"123456789": "我", "-1001234567890": "群組"}
  TEST_MODE     : 設為 1/true → 即刻列晒兩邊現貨一次（用嚟測試）
  DEBUG_TRU     : 設為 1      → 只抓 Toys"R"Us 並印出結構（用嚟 debug selector）

執行環境：
  - GitHub Actions：偵測到 GITHUB_ACTIONS=true → 行一次就 exit（由 cron-jobs.org 觸發）
  - 本機：長駐 loop，每 CHECK_INTERVAL_MIN 分鐘檢查一次
"""

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
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    Retry = None


# ============ Logging（一定要喺讀環境變數之前，等 log 即刻可用）============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("monitor.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ============ 環境變數 / 共用設定 ============
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
if not TG_BOT_TOKEN:
    raise SystemExit("❌ 未設定 TG_BOT_TOKEN（請喺 GitHub Secrets 或本機環境變數加入）")


def _extract_chat_ids(raw):
    """支援 list [id, id] 或 dict {id: label} / {label: id}，統一回傳 ['id', ...]。"""

    def looks_like_id(v):
        s = str(v).strip().lstrip("-")
        return s.isdigit()

    if isinstance(raw, list):
        ids = raw
    elif isinstance(raw, dict):
        if raw and all(looks_like_id(k) for k in raw.keys()):
            ids = list(raw.keys())
        elif raw and all(looks_like_id(v) for v in raw.values()):
            ids = list(raw.values())
        else:
            ids = list(raw.keys())
    else:
        ids = [raw]
    return [str(x).strip() for x in ids if str(x).strip()]


TG_CHAT_IDS = []
_env_chat = os.environ.get("TG_CHAT_IDS")
if _env_chat:
    try:
        TG_CHAT_IDS = _extract_chat_ids(json.loads(_env_chat))
    except Exception as e:
        log.error(f"TG_CHAT_IDS 格式錯誤，應為合法 JSON：{e}")
if not TG_CHAT_IDS:
    raise SystemExit("❌ 未設定 TG_CHAT_IDS（或格式錯誤）")

# 🧪 測試開關：可用環境變數 TEST_MODE=1 開啟（會即刻列晒兩邊現貨一次）
TEST_MODE = os.environ.get("TEST_MODE", "").lower() in ("1", "true", "yes")

# 監察頻率（本機 loop 用；GitHub Actions 由 cron 控制，呢度唔生效）
CHECK_INTERVAL_MIN = 5
JITTER_SEC = 90
PAGE_DELAY = (0.5, 1.5)

# Telegram 單一訊息字數上限約 4096，留啲緩衝
TG_MSG_MAX = 4000

# ---- 來源 1：Hobbyland ----
API_URL = "https://backend.hobbylandeshop.com/api/products"
SITE = "https://www.hobbylandeshop.com"
STATE_FILE = Path("seen_hobbyland.json")

# ---- 來源 2：Toys"R"Us HK ----
TRU_BASE = "https://www.toysrus.com.hk"
TRU_PREORDER_URL = TRU_BASE + "/zh-hk/whats-on/new-arrivals/pre-order/"
TRU_STATE_FILE = Path("seen_toysrus.json")
# 只想收陀螺相關 → 留住關鍵字；想收晒成個 pre-order → 設成空 tuple ()
TRU_KEYWORDS = (
    "Beyblade",
    "beyblade",
    "陀螺",
    "bey",
)


# ============ 共用 session（自動重試，淨係用喺「讀取」請求）============
session = requests.Session()
if Retry is not None:
    try:
        _retry = Retry(
            total=3,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "POST"]),
        )
    except TypeError:  # 舊版 urllib3
        _retry = Retry(
            total=3,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            method_whitelist=frozenset(["GET", "POST"]),
        )
    _adapter = HTTPAdapter(max_retries=_retry)
    session.mount("https://", _adapter)
    session.mount("http://", _adapter)

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
    "Accept-Language": "zh-HK,zh;q=0.9,en-HK;q=0.8,en;q=0.7",
    "Referer": TRU_BASE + "/zh-hk/",
}


def _make_soup(html):
    """優先用 lxml，冇裝就 fallback html.parser，兩者都解析得到。"""
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


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
    lines = [f"{tag} <b>{p.get('title', '')}</b>"]

    if p.get("sell_type"):
        lines.append(f"類型: {p['sell_type']}")

    price = str(p.get("price", "")).strip()
    reg = str(p.get("regular_price", "")).strip()
    if price and price != "?":
        if reg and reg not in ("0", "") and reg != price:
            lines.append(f"價錢: ${price} (原價 ${reg})")
        else:
            lines.append(f"價錢: ${price}")

    stock = p.get("stock", "")
    if stock not in ("", "-", None):
        limit_raw = p.get("sale_limit", 0)
        limit = "無限制" if not limit_raw else f"{limit_raw} 件"
        lines.append(f"庫存: {stock} | 限購: {limit}")

    if p.get("url"):
        lines.append(p["url"])

    return "\n".join(lines)


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


# ============ 來源 2：Toys"R"Us 抓取（HTML scrape，自動偵測）============
def _clean_price(text):
    if not text:
        return ""
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(text))
    return m.group(0).replace(",", "") if m else ""


def _tru_pick_link(tile):
    """喺 tile 入面揀最似『產品頁』嘅 <a>。"""
    anchors = tile.find_all("a", href=True)
    if not anchors:
        return None
    for a in anchors:
        href = a["href"].lower()
        if ".html" in href or "/product" in href or "pid=" in href:
            return a
    return anchors[0]


def _tru_extract_name(tile, link_el):
    """試多個常見 selector，再 fallback 去 link 文字 / 屬性 / 圖片 alt。"""
    name_selectors = [
        ".pdp-link a",
        ".product-name a",
        "a.product-name",
        ".product-name",
        ".pdp-link",
        "a.link",
        ".tile-body a",
        ".name a",
        ".name",
        ".product-tile__name",
        ".product-title",
        "[itemprop='name']",
        ".card-title",
        ".tile-title",
        "h2 a",
        "h3 a",
        "h2",
        "h3",
    ]
    for sel in name_selectors:
        el = tile.select_one(sel)
        if el:
            txt = el.get_text(strip=True)
            if txt:
                return txt
    if link_el is not None:
        for attr in ("title", "aria-label", "data-name"):
            v = link_el.get(attr)
            if v and v.strip():
                return v.strip()
        txt = link_el.get_text(strip=True)
        if txt:
            return txt
    img = tile.select_one("img[alt]")
    if img and img.get("alt", "").strip():
        return img["alt"].strip()
    return ""


def _tru_extract_price(tile):
    price_selectors = [
        ".price .sales .value",
        ".price .value",
        ".sales .value",
        "[itemprop='price']",
        ".price-sales",
        ".product-price",
        ".sales",
        ".price",
        "[class*='price']",
    ]
    for sel in price_selectors:
        el = tile.select_one(sel)
        if el is None:
            continue
        val = el.get("content") or el.get_text(" ", strip=True)
        cleaned = _clean_price(val)
        if cleaned:
            return cleaned
    return ""


def _tru_parse_tiles(soup):
    """由 HTML 抽出產品 tiles（已對應 TRU HK 嘅實際結構）。"""
    nodes = soup.select("div.product-tile")
    if not nodes:
        nodes = soup.select("div.product[data-pid], li.product, div.tile")
    if not nodes:
        # 最後手段：所有帶 data-pid 嘅「外層」元素
        nodes = [
            n
            for n in soup.select("[data-pid]")
            if not n.find_parent(attrs={"data-pid": True})
        ]

    out = []
    seen_pids = set()
    for node in nodes:
        pid = node.get("data-pid")
        if not pid:
            inner = node.select_one("[data-pid]")
            pid = inner.get("data-pid") if inner else None
        if pid:
            if pid in seen_pids:
                continue
            seen_pids.add(pid)

        link_el = _tru_pick_link(node)
        href = link_el["href"] if link_el is not None else ""
        url = href if href.startswith("http") else (TRU_BASE + href if href else "")

        title = _tru_extract_name(node, link_el)
        price = _tru_extract_price(node)

        if not (title or pid):
            continue

        sku = pid or url or title
        out.append(
            {
                "title": title or "(未取得名稱)",
                "sku": f"tru:{sku}",  # 加前綴，確保唔同 Hobbyland 撞 key
                "price": price or "?",
                "regular_price": "",
                "url": url,
                "sell_type": "預訂 Pre-order",
                "stock": "-",
                "sale_limit": 0,
            }
        )
    return out


def fetch_toysrus_preorder():
    """揭晒所有分頁，回傳已 filter 嘅 list。"""
    seen_sku = set()
    items = []
    start = 0
    page_size = 48
    pages = 0

    while True:
        params = {} if start == 0 else {"start": start, "sz": page_size}
        try:
            resp = session.get(
                TRU_PREORDER_URL, params=params, headers=TRU_HEADERS, timeout=20
            )
            resp.raise_for_status()
        except requests.HTTPError:
            if start == 0:
                raise  # 第一頁都失敗 → 真係出事，往上拋
            break  # 後續分頁失敗 → 當揭完

        tiles = _tru_parse_tiles(_make_soup(resp.text))
        if not tiles:
            break

        new = 0
        for t in tiles:
            if t["sku"] not in seen_sku:
                seen_sku.add(t["sku"])
                items.append(t)
                new += 1
        pages += 1

        if new == 0:  # 呢頁全部見過 → 到尾
            break

        start += len(tiles)
        if start > 3000:  # 安全掣
            break
        time.sleep(random.uniform(*PAGE_DELAY))

    total = len(items)
    if TRU_KEYWORDS:
        kw = tuple(k.lower() for k in TRU_KEYWORDS)
        items = [
            p
            for p in items
            if any(
                k in (p.get("title", "") + " " + p.get("url", "")).lower() for k in kw
            )
        ]
    log.info(f"[ToysRUs] 揭咗 {pages} 頁，共抓 {total} 件，符合關鍵字 {len(items)} 件")
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


# ============ 🔧 DEBUG_TRU：抓 TRU 並印結構（debug 用）============
def debug_toysrus():
    log.info("🔧 DEBUG_TRU：抓取 Toys'R'Us 並列印結構")
    resp = session.get(TRU_PREORDER_URL, headers=TRU_HEADERS, timeout=20)
    log.info(f"status={resp.status_code} len={len(resp.text)}")
    soup = _make_soup(resp.text)
    tiles = soup.select("div.product-tile")
    log.info(f"div.product-tile 抓到 {len(tiles)} 個")
    if tiles:
        log.info("第一個 tile 結構（頭 2500 字）：\n" + tiles[0].prettify()[:2500])
    parsed = _tru_parse_tiles(soup)
    log.info(f"_tru_parse_tiles 解析到 {len(parsed)} 件，頭 5 件：")
    for p in parsed[:5]:
        log.info(json.dumps(p, ensure_ascii=False))


# ============ 主程式 ============
def main():
    # 🔧 Debug Toys"R"Us（最高優先）
    if os.environ.get("DEBUG_TRU"):
        debug_toysrus()
        return

    # 🌟 GitHub Actions：行一次就 exit
    if os.environ.get("GITHUB_ACTIONS") == "true":
        if TEST_MODE:
            log.info("🤖 GitHub Actions（測試模式）：列出兩邊現貨...")
            run_test()
        else:
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
    send_telegram(
        "🤖 Beyblade X 陀螺預訂監察機已開機（Hobbyland + Toys'R'Us），有新貨即時通知!"
    )

    while True:
        check_all_sources()
        wait = CHECK_INTERVAL_MIN * 60 + random.uniform(-JITTER_SEC, JITTER_SEC)
        wait = max(60, wait)
        log.info(f"下次檢查：{wait / 60:.1f} 分鐘後")
        time.sleep(wait)


if __name__ == "__main__":
    main()
