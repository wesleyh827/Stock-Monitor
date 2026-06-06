"""
台股大漲大跌監測器（twstock 即時報價 + Telegram 版）
-------------------------------------------------
資料來源改用 twstock，直接讀證交所盤中即時報價，延遲比 Yahoo 短很多。
通知方式維持 Telegram。

注意：
  * 即時報價只有在台股交易時間（09:00~13:30）才有意義；非交易時間抓到的是最後狀態。
  * twstock 是讀證交所的非公開即時介面，呼叫太頻繁可能被擋，CHECK_INTERVAL 不要設太小。
"""

import time
import requests
import twstock
from datetime import datetime

# ======================== 設定區（改這裡就好）========================

# 想監測的股票：代號 -> 顯示名稱
# 用 twstock 不需要加 .TW / .TWO，直接填數字代號即可，上市櫃會自動判斷
WATCHLIST = {
    "2330": "台積電",
    "2317": "鴻海",
    "2454": "聯發科",
    "2382": "廣達",
    "5274": "信驊",
    "0050": "元大台灣50",
}

# 漲跌幅門檻（%）
THRESHOLD = 5.0

# 每隔幾秒檢查一次（建議 60 秒以上，避免被證交所擋）
CHECK_INTERVAL = 60

# 只在台股交易時間內檢查（09:00~13:30，週一到週五）
ONLY_TRADING_HOURS = True

# 是否在「跌破月線（20日均線）」時通知
ALERT_BELOW_MA20 = True

# Telegram 設定（把你之前已經設好的兩個值複製過來）
TELEGRAM_BOT_TOKEN = "8959204141:AAETl_EYqSsObDVyf23_imBCfA_XWU-1bdE"
TELEGRAM_CHAT_ID = "8408767515"   # 不知道就先留空，執行程式時會自動幫你抓出來

# ===================================================================


_notified = set()       # 記錄今天已通知過的「大漲大跌」標的，避免重複轟炸
_ma20_cache = {}        # 月線快取，每天只抓一次：{ "2025-06-06-2330": 月線值 }
_below_ma20 = {}        # 記錄各股「上次是否在月線下方」，用來偵測跌破的那一刻


# ----------------------- Telegram 相關 -----------------------

def get_chat_id_hint() -> None:
    """沒填 chat id 時，自動呼叫 getUpdates 幫忙找出來。"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        data = requests.get(url, timeout=10).json()
        updates = data.get("result", [])
        if not updates:
            print("找不到訊息紀錄。請先用手機在 Telegram 對你的機器人傳一句話（例如 hi），再重跑一次。")
            return
        chat = updates[-1]["message"]["chat"]
        print(f"找到你的 chat id 了：{chat['id']}")
        print("請把它填到程式最上面的 TELEGRAM_CHAT_ID，再重新執行。")
    except Exception as e:
        print(f"抓取 chat id 時發生錯誤：{e}")


def send_telegram(message: str) -> None:
    """透過 Telegram 機器人推播一則文字訊息。"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print("  -> Telegram 通知已送出")
        else:
            print(f"  -> Telegram 發送失敗：{resp.status_code} {resp.text}")
    except Exception as e:
        print(f"  -> Telegram 發送發生錯誤：{e}")


# ----------------------- 股價抓取 -----------------------

def _to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _first_quote(s):
    """從 '848.0_847.0_..._' 這種五檔字串取第一個價格。"""
    if not s:
        return None
    parts = s.strip("_").split("_")
    return _to_float(parts[0]) if parts else None


def _parse(d: dict):
    """
    從證交所原始單筆資料算出 (現價, 漲跌幅%)。
    欄位：y=昨收, z=最新成交價, o=開盤, a=最佳賣價, b=最佳買價
    """
    prev = _to_float(d.get("y"))        # 昨收
    last = _to_float(d.get("z"))        # 最新成交價

    # 尚無成交（z 為 '-'）時，改用買賣五檔中間價，再退而用開盤價
    if last is None:
        bid = _first_quote(d.get("b"))
        ask = _first_quote(d.get("a"))
        if bid and ask:
            last = (bid + ask) / 2
        else:
            last = _to_float(d.get("o"))

    if last is None or not prev:
        return None
    return last, (last - prev) / prev * 100


def get_ma20(symbol: str):
    """
    取得某檔股票的月線（20 日均線）。
    月線用日收盤計算、盤中不變，所以每天只抓一次，之後直接用快取。
    抓不到或資料不足 20 天時回傳 None。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    key = f"{today}-{symbol}"
    if key in _ma20_cache:
        return _ma20_cache[key]

    try:
        stock = twstock.Stock(symbol)          # 預設抓最近約 31 個交易日
        closes = [c for c in stock.price if c is not None]
    except Exception as e:
        print(f"  抓 {symbol} 月線資料失敗：{e}")
        _ma20_cache[key] = None                # 當天記為失敗，避免一直重抓
        return None

    if len(closes) < 20:
        _ma20_cache[key] = None
        return None

    ma20 = sum(closes[-20:]) / 20
    _ma20_cache[key] = ma20
    return ma20


def get_all_changes(symbols):
    """一次抓所有股票，回傳 {代號: (現價, 漲跌幅%)}。"""
    result = {}
    try:
        raw = twstock.realtime.get_raw(list(symbols))
    except Exception as e:
        print(f"  抓取即時報價發生錯誤：{e}")
        return result

    for d in raw.get("msgArray", []):
        code = d.get("c")
        parsed = _parse(d)
        if parsed:
            result[code] = parsed
    return result


# ----------------------- 主邏輯 -----------------------

def is_trading_hours() -> bool:
    """判斷現在是否為台股交易時間（週一到週五 09:00~13:30）。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 9 * 60 <= t <= 13 * 60 + 30


def check_once() -> None:
    """檢查一輪所有關注的股票。"""
    today = datetime.now().strftime("%Y-%m-%d")
    changes = get_all_changes(WATCHLIST.keys())

    for symbol, name in WATCHLIST.items():
        if symbol not in changes:
            print(f"{datetime.now():%H:%M:%S}  {name}({symbol})  抓不到資料，略過")
            continue

        price, pct = changes[symbol]
        print(f"{datetime.now():%H:%M:%S}  {name}({symbol})  {price:.2f}  {pct:+.2f}%")

        # (1) 大漲大跌通知（每檔每天一次）
        key = f"{today}-{symbol}"
        if abs(pct) >= THRESHOLD and key not in _notified:
            arrow = "📈 大漲" if pct > 0 else "📉 大跌"
            msg = (
                f"{arrow}通知\n"
                f"{name}（{symbol}）\n"
                f"現價：{price:.2f}\n"
                f"漲跌幅：{pct:+.2f}%\n"
                f"時間：{datetime.now():%Y-%m-%d %H:%M:%S}"
            )
            send_telegram(msg)
            _notified.add(key)

        # (2) 跌破月線通知（只在從月線上方跌到下方的那一刻通知一次）
        if ALERT_BELOW_MA20:
            ma20 = get_ma20(symbol)
            if ma20 is not None:
                is_below = price < ma20
                was_below = _below_ma20.get(symbol)   # 第一次看到時為 None
                if is_below and was_below is not True:
                    msg = (
                        f"⚠️ 跌破月線通知\n"
                        f"{name}（{symbol}）\n"
                        f"現價：{price:.2f}\n"
                        f"月線(MA20)：{ma20:.2f}\n"
                        f"時間：{datetime.now():%Y-%m-%d %H:%M:%S}"
                    )
                    send_telegram(msg)
                _below_ma20[symbol] = is_below


def main() -> None:
    if not TELEGRAM_CHAT_ID:
        print("尚未設定 TELEGRAM_CHAT_ID，幫你嘗試自動抓取...")
        get_chat_id_hint()
        return

    print("=" * 40)
    print(f"台股即時監測啟動（twstock），門檻 ±{THRESHOLD}%，每 {CHECK_INTERVAL} 秒檢查一次")
    print("按 Ctrl+C 可停止")
    print("=" * 40)
    while True:
        try:
            if ONLY_TRADING_HOURS and not is_trading_hours():
                print(f"{datetime.now():%H:%M:%S}  非交易時間，休息中...")
            else:
                check_once()
        except KeyboardInterrupt:
            print("\n已停止監測。")
            break
        except Exception as e:
            print(f"檢查時發生錯誤：{e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()