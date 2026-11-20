import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone
import pandas as pd
import subprocess, sys
try:
    import t_tech.invest
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "t-tech-investments", "--extra-index-url", "https://" + "opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple"])

from t_tech.invest.sandbox.client import SandboxClient
from t_tech.invest import CandleInterval, OrderDirection, OrderType
from t_tech.invest.schemas import MoneyValue
from t_tech.invest.utils import quotation_to_decimal, money_to_decimal, now

SANDBOX_TOKEN = os.getenv("SANDBOX_TOKEN", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

RUB_PER_LOT = 14500.0
FAST_EMA = 20
SLOW_EMA = 60
EMERGENCY_STOP_ATR = 3.5
DAILY_LOSS_PCT = 0.05
MAX_TRADES_PER_DAY = 2
BUFFER_PER_LOT = 4500.0
CASH_BUFFER_MIN = 2500.0
STATE_FILE = "ema_bot_sandbox_state.json"
LOG_FILE = "trades_log.txt"


def msk_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=3)))


def write_log(text: str):
    line = f"[{msk_now().strftime('%Y-%m-%d %H:%M:%S МСК')}] {text}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def send_tg(text: str):
    if TG_BOT_TOKEN and TG_CHAT_ID:
        try:
            requests.post(f"https://telegram.org{TG_BOT_TOKEN}/sendMessage", json={"chat_id": TG_CHAT_ID, "text": text}, timeout=10)
        except Exception:
            pass


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"entry_price": None, "last_candle": None, "day": None, "start_bal": None, "trades": 0, "rep_day": None}


def save_state(st: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)


def is_futures_time() -> bool:
    cur = msk_now()
    if cur.weekday() >= 5:
        return False
    t = cur.time()
    s1 = datetime.strptime("10:30", "%H:%M").time() <= t <= datetime.strptime("13:55", "%H:%M").time()
    s2 = datetime.strptime("14:10", "%H:%M").time() <= t <= datetime.strptime("18:45", "%H:%M").time()
    s3 = datetime.strptime("19:10", "%H:%M").time() <= t <= datetime.strptime("23:45", "%H:%M").time()
    return s1 or s2 or s3


def is_etf_time() -> bool:
    cur = msk_now()
    if cur.weekday() >= 5:
        return False
    t = cur.time()
    return datetime.strptime("10:05", "%H:%M").time() <= t <= datetime.strptime("18:35", "%H:%M").time()


def get_account(client: SandboxClient) -> str:
    accs = client.users.get_accounts().accounts
    if accs:
        return next(iter(accs)).id
    acc_id = client.sandbox.open_sandbox_account().account_id
    client.sandbox.sandbox_pay_in(account_id=acc_id, amount=MoneyValue(currency="rub", units=30000, nano=0))
    return acc_id


def get_cny_future(client: SandboxClient) -> tuple[str, str]:
    cur_utc = now()
    cands = []
    for f in client.instruments.futures().instruments:
        if f.basic_asset == "CNY/RUB" and f.ticker != "CNYRUBF" and f.api_trade_available_flag:
            days = (f.expiration_date - cur_utc).days
            if days >= 5:
                cands.append((days, f.ticker, f.uid))
    _, best_ticker, best_uid = min(cands)
    return best_ticker, best_uid


def find_tmon_uid(client: SandboxClient) -> str | None:
    for etf in client.instruments.etfs().instruments:
        if etf.ticker in ("TMON", "TMON@") and etf.api_trade_available_flag:
            return etf.uid
    return None

def get_portfolio_info(client: SandboxClient, acc_id: str, fut_uid: str, tmon_uid: str | None) -> tuple[float, float, int, int]:
    portfolio = client.operations.get_portfolio(account_id=acc_id)
    total_bal = float(money_to_decimal(portfolio.total_amount_portfolio))
    free_rub = float(money_to_decimal(portfolio.total_amount_currencies))

    fut_pos = 0
    tmon_lots = 0
    for p in portfolio.positions:
        qty = int(quotation_to_decimal(p.quantity))
        if p.instrument_uid == fut_uid:
            fut_pos = qty
        elif tmon_uid and p.instrument_uid == tmon_uid:
            tmon_lots = qty

    return total_bal, free_rub, fut_pos, tmon_lots


def manage_liquidity(client: SandboxClient, acc_id: str, tmon_uid: str | None, free_rub: float, tmon_lots: int, target_lots: int):
    if not tmon_uid or not is_etf_time():
        return
    try:
        last_prices = client.market_data.get_last_prices(instrument_id=[tmon_uid]).last_prices
        if not last_prices:
            return
        tmon_price = float(quotation_to_decimal(next(iter(last_prices)).price))
        if tmon_price <= 0:
            return
        buy_buffer = BUFFER_PER_LOT * target_lots
        if free_rub > buy_buffer + tmon_price:
            lots_to_buy = int((free_rub - buy_buffer) // tmon_price)
            if lots_to_buy >= 1:
                client.orders.post_order(instrument_id=tmon_uid, quantity=lots_to_buy, direction=OrderDirection.ORDER_DIRECTION_BUY, account_id=acc_id, order_type=OrderType.ORDER_TYPE_MARKET)
                msg = f"ЛИКВИДНОСТЬ | Куплено {lots_to_buy} паев TMON@ по ~{tmon_price:.2f} руб."
                write_log(msg)
                send_tg(f"💰 {msg}")
        elif free_rub < CASH_BUFFER_MIN and tmon_lots > 0:
            deficit = CASH_BUFFER_MIN - free_rub
            lots_to_sell = min(tmon_lots, int(deficit // tmon_price) + 1)
            if lots_to_sell >= 1:
                client.orders.post_order(instrument_id=tmon_uid, quantity=lots_to_sell, direction=OrderDirection.ORDER_DIRECTION_SELL, account_id=acc_id, order_type=OrderType.ORDER_TYPE_MARKET)
                msg = f"ЛИКВИДНОСТЬ | Продано {lots_to_sell} паев TMON@ по ~{tmon_price:.2f} руб."
                write_log(msg)
                send_tg(f"💸 {msg}")
    except Exception as e:
        print(f"Примечание по TMON@: {e}")


def get_candles(client: SandboxClient, uid: str) -> pd.DataFrame:
    rows = []
    for c in client.get_all_candles(instrument_id=uid, from_=now() - timedelta(days=45), to=now(), interval=CandleInterval.CANDLE_INTERVAL_4_HOUR):
        if c.is_complete:
            rows.append({"time": c.time.isoformat(), "high": float(quotation_to_decimal(c.high)), "low": float(quotation_to_decimal(c.low)), "close": float(quotation_to_decimal(c.close))})
    df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
    df["fast"] = df["close"].ewm(span=FAST_EMA, adjust=False).mean()
    df["slow"] = df["close"].ewm(span=SLOW_EMA, adjust=False).mean()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - df["close"].shift(1)).abs(), (df["low"] - df["close"].shift(1)).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    return df.dropna().reset_index(drop=True)


def send_order(client: SandboxClient, acc_id: str, uid: str, ticker: str, lots: int, is_buy: bool, reason: str, price: float):
    direction = OrderDirection.ORDER_DIRECTION_BUY if is_buy else OrderDirection.ORDER_DIRECTION_SELL
    resp = client.orders.post_order(instrument_id=uid, quantity=abs(lots), direction=direction, account_id=acc_id, order_type=OrderType.ORDER_TYPE_MARKET)
    side = "ПОКУПКА (ЛОНГ)" if is_buy else "ПРОДАЖА (ШОРТ)"
    msg = f"СДЕЛКА | {reason} | {ticker}: {side} {abs(lots)} лот(ов) | Цена ~{price:.3f} | ID: {resp.order_id}"
    write_log(msg)
    send_tg(f"🔔 {msg}")


def run_bot():
    st = load_state()
    with SandboxClient(SANDBOX_TOKEN) as client:
        acc_id = get_account(client)
        ticker, uid = get_cny_future(client)
        tmon_uid = find_tmon_uid(client)
        bal, free_rub, pos, tmon_lots = get_portfolio_info(client, acc_id, uid, tmon_uid)
        target_lots = max(1, int(bal // RUB_PER_LOT))
        tmon_status = f"найден ({tmon_lots} паев)" if tmon_uid else "не найден"
        start_msg = f"СТАРТ | EMA {FAST_EMA}/{SLOW_EMA} (4H) + TMON@ | Фьючерс: {ticker} (поз: {pos}, расч. лотов: {target_lots}) | Баланс: {bal:.2f} руб. | TMON@: {tmon_status}"
        write_log(start_msg)
        send_tg(f"✅ {start_msg}")

        while True:
            try:
                cur = msk_now()
                today = cur.strftime("%Y-%m-%d")
                if not is_futures_time():
                    print(f"[{cur.strftime('%H:%M')}] Вне торгового времени / клиринг. Ждем...")
                    time.sleep(60)
                    continue
                bal, free_rub, pos, tmon_lots = get_portfolio_info(client, acc_id, uid, tmon_uid)
                target_lots = max(1, int(bal // RUB_PER_LOT))
                daily_loss_limit = bal * DAILY_LOSS_PCT
                if st["day"] != today or st["start_bal"] is None:
                    st["day"] = today
                    st["start_bal"] = bal
                    st["trades"] = 0
                    save_state(st)
                if cur.hour == 19 and cur.minute >= 15 and st["rep_day"] != today:
                    rep = f"ОТЧЕТ ЗА ДЕНЬ | {ticker} | Баланс: {bal:.2f} руб. ({bal - st['start_bal']:+.2f}) | Позиция: {pos} (цель: {target_lots}) | TMON@: {tmon_lots} | Кэш: {free_rub:.2f} руб."
                    write_log(rep)
                    send_tg(f"📊 {rep}")
                    st["rep_day"] = today
                    save_state(st)
                df = get_candles(client, uid)
                last = df.iloc[-1]
                price, fast, slow, atr, c_time = last["close"], last["fast"], last["slow"], last["atr"], last["time"]
                if st["start_bal"] - bal >= daily_loss_limit:
                    if pos > 0:
                        send_order(client, acc_id, uid, ticker, pos, False, f"Дневной стоп-лимит ({daily_loss_limit:.0f} руб.)", price)
                    elif pos < 0:
                        send_order(client, acc_id, uid, ticker, pos, True, f"Дневной стоп-лимит ({daily_loss_limit:.0f} руб.)", price)
                    st["entry_price"] = None
                    st["trades"] = MAX_TRADES_PER_DAY
                    save_state(st)
                    time.sleep(300)
                    continue
                if pos != 0 and st["entry_price"] is None:
                    st["entry_price"] = price
                    save_state(st)
                if pos > 0 and st["entry_price"] and price <= st["entry_price"] - EMERGENCY_STOP_ATR * atr:
                    send_order(client, acc_id, uid, ticker, pos, False, "Аварийный стоп лонга", price)
                    st["entry_price"] = None
                    save_state(st)
                    pos = 0
                elif pos < 0 and st["entry_price"] and price >= st["entry_price"] + EMERGENCY_STOP_ATR * atr:
                    send_order(client, acc_id, uid, ticker, pos, True, "Аварийный стоп шорта", price)
                    st["entry_price"] = None
                    save_state(st)
                    pos = 0
                if st["last_candle"] != c_time:
                    st["last_candle"] = c_time
                    trend = "ЛОНГ" if fast > slow else "ШОРТ"
                    print(f"[{cur.strftime('%H:%M')}] Свеча 4H {c_time} | Цена: {price:.3f} | EMA20: {fast:.3f} / EMA60: {slow:.3f} ({trend}) | Фьюч: {pos} (цель: {target_lots}) | TMON@: {tmon_lots}")
                    if st["trades"] < MAX_TRADES_PER_DAY:
                        if fast > slow and pos <= 0:
                            send_order(client, acc_id, uid, ticker, target_lots + abs(pos), True, f"Тренд вверх (EMA20 > EMA60, объем {target_lots} лот.)", price)
                            st["entry_price"] = price
                            st["trades"] += 1
                        elif fast < slow and pos >= 0:
                            send_order(client, acc_id, uid, ticker, target_lots + abs(pos), False, f"Тренд вниз (EMA20 < EMA60, объем {target_lots} лот.)", price)
                            st["entry_price"] = price
                            st["trades"] += 1
                    save_state(st)
                bal, free_rub, pos, tmon_lots = get_portfolio_info(client, acc_id, uid, tmon_uid)
                target_lots = max(1, int(bal // RUB_PER_LOT))
                manage_liquidity(client, acc_id, tmon_uid, free_rub, tmon_lots, target_lots)
            except Exception as e:
                err = f"ОШИБКА: {e}"
                write_log(err)
                send_tg(f"⚠️ {err}")
            time.sleep(60)


if __name__ == "__main__":
    run_bot()
