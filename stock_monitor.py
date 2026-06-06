"""
台股大漲大跌監測器
-------------------------------------------------
功能：定時抓取你關注的台股股價，當單日漲跌幅超過設定門檻時，
      透過 Telegram 機器人推播通知給你。

使用前請先到下方「設定區」填好兩個東西：
  1. TELEGRAM_BOT_TOKEN   跟 @BotFather 申請的機器人 token
  2. TELEGRAM_CHAT_ID      你的 chat id(不知道的話可以讓程式自動幫你抓，見最下面說明）
"""

import time
import requests
import yfinance as yf
from datetime import datetime

# ======================== 設定區（改這裡就好）========================

# 想監測的股票：代號 -> 顯示名稱
# 上市股票用 .TW，上櫃（櫃買）股票用 .TWO
WATCHLIST = {
    "2330.TW": "台積電",
    "2317.TW": "鴻海",
    "0050.TW": "元大台灣50",
}

# 漲跌幅門檻（%）。例如設 5，代表漲超過 +5% 或跌超過 -5% 就通知
THRESHOLD = 0

# 每隔幾秒檢查一次（300 秒 = 5 分鐘）
CHECK_INTERVAL = 300

# 只在台股交易時間內檢查（09:00~13:30，週一到週五）。設 False 則全天檢查
ONLY_TRADING_HOURS = False

# Telegram 設定
TELEGRAM_BOT_TOKEN = "8959204141:AAETl_EYqSsObDVyf23_imBCfA_XWU-1bdE"
TELEGRAM_CHAT_ID = "8408767515"   # 不知道就先留空，執行程式時會自動幫你抓出來

# ===================================================================


_notified = set()   # 記錄今天已通知過的標的，避免重複轟炸


def get_chat_id_hint() -> None:
    """
    若沒填 chat id，自動呼叫 Telegram getUpdates 幫忙找出來。
    前提：你要先用手機在 Telegram 裡對你的機器人傳任何一句話（例如 hi）。
    """
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


def get_price_change(symbol: str):
    """
    取得某檔股票的「現價」與「相對前一交易日收盤的漲跌幅(%)」。
    回傳 (price, pct)；抓不到資料時回傳 None。
    """
    ticker = yf.Ticker(symbol)

    last = prev = None
    try:
        last = ticker.fast_info.last_price
        prev = ticker.fast_info.previous_close
    except Exception:
        pass

    if not last or not prev:
        try:
            hist = ticker.history(period="2d")
            if len(hist) >= 2:
                last = float(hist["Close"].iloc[-1])
                prev = float(hist["Close"].iloc[-2])
            elif len(hist) == 1:
                last = float(hist["Close"].iloc[-1])
                prev = float(hist["Open"].iloc[-1])
        except Exception:
            return None

    if not last or not prev:
        return None

    pct = (last - prev) / prev * 100
    return last, pct


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
    for symbol, name in WATCHLIST.items():
        result = get_price_change(symbol)
        if result is None:
            print(f"{datetime.now():%H:%M:%S}  {name}({symbol})  抓不到資料，略過")
            continue

        price, pct = result
        print(f"{datetime.now():%H:%M:%S}  {name}({symbol})  {price:.2f}  {pct:+.2f}%")

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


def main() -> None:
    # 沒填 chat id 就先幫忙抓
    if not TELEGRAM_CHAT_ID:
        print("尚未設定 TELEGRAM_CHAT_ID，幫你嘗試自動抓取...")
        get_chat_id_hint()
        return

    print("=" * 40)
    print(f"台股監測啟動，門檻 ±{THRESHOLD}%，每 {CHECK_INTERVAL} 秒檢查一次")
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