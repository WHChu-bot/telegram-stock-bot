import asyncio
import logging
import os
import threading
import time
from datetime import time as dt_time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf
from flask import Flask, jsonify
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)




class ConfigError(RuntimeError):
    pass


class DataFetchError(RuntimeError):
    pass


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
FINNHUB_KEY = os.getenv("FINNHUB_KEY", "").strip()
WATCHLIST = os.getenv("WATCHLIST", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
ALERT_TIME = os.getenv("ALERT_TIME", "09:30").strip()
ALERT_TIMEZONE = os.getenv("ALERT_TIMEZONE", "America/New_York").strip()
ALERT_ENABLED = os.getenv("ALERT_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def parse_watchlist(raw: str) -> list[str]:
    seen = set()
    symbols = []
    for item in raw.replace(" ", "").split(","):
        if not item:
            continue
        symbol = item.upper()
        if symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    return symbols


DEFAULT_WATCHLIST = parse_watchlist(WATCHLIST or "TSLA,AAPL,NVDA,MSFT")


def mask_secret(text: str) -> str:
    if FINNHUB_KEY:
        text = text.replace(FINNHUB_KEY, "***")
    return text


def format_public_error(exc: Exception) -> str:
    if isinstance(exc, ConfigError):
        return f"配置错误：{exc}"
    if isinstance(exc, DataFetchError):
        return str(exc)
    return "系统繁忙或数据源暂时不可用，请稍后再试。"


def parse_alert_clock(value: str, timezone_name: str) -> dt_time:
    try:
        hour_str, minute_str = value.split(":", 1)
        tz = ZoneInfo(timezone_name)
        return dt_time(hour=int(hour_str), minute=int(minute_str), tzinfo=tz)
    except Exception as exc:
        raise ConfigError("ALERT_TIME 需使用 HH:MM，且 ALERT_TIMEZONE 必须是有效时区") from exc


def validate_runtime_config() -> None:
        if not BOT_TOKEN:
                raise ConfigError("未设置 BOT_TOKEN")
        if ALERT_ENABLED:
                parse_alert_clock(ALERT_TIME, ALERT_TIMEZONE)
        parse_alert_clock(ALERT_TIME, ALERT_TIMEZONE 
        if ALERT_ENABLED:
        parse_alert_clock(ALERT_TIME, ALERT_TIMEZONE)


flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "Bot is running"


@flask_app.route("/healthz")
def healthz():
    return jsonify(
        {
            "ok": True,
            "watchlist_size": len(DEFAULT_WATCHLIST),
            "alert_enabled": ALERT_ENABLED,
            "alert_time": ALERT_TIME,
            "alert_timezone": ALERT_TIMEZONE,
            "chat_configured": bool(TELEGRAM_CHAT_ID),
        }
    )


def get_data(symbol: str) -> pd.DataFrame:
    def get_data(symbol: str) -> pd.DataFrame:
    symbol = symbol.upper()

    try:
        df = yf.download(
            symbol,
            period="6mo",
            interval="1d",
            auto_adjust=False,
            progress=False,
        )
    except Exception as exc:
        raise DataFetchError(f"{symbol}：网络请求失败") from exc

    if df is None or df.empty:
        raise DataFetchError(f"{symbol}：暂无足够日线数据")

    df = df.reset_index()

    if "Date" not in df.columns:
        raise DataFetchError(f"{symbol}：数据格式异常")

    df = df.rename(
        columns={
            "Date": "time",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )

    needed = ["time", "open", "high", "low", "close", "volume"]
    for col in needed:
        if col not in df.columns:
            raise DataFetchError(f"{symbol}：缺少字段 {col}")

    df = df[needed].copy()

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna().reset_index(drop=True)

    if len(df) < 60:
        raise DataFetchError(f"{symbol}：数据不足，至少需要 60 根日线")

    df["ema9"] = df["close"].ewm(span=9).mean()
    df["ema21"] = df["close"].ewm(span=21).mean()
    df["ema50"] = df["close"].ewm(span=50).mean()

    delta = df["close"].diff()
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = pd.Series(gain).rolling(14).mean()
    avg_loss = pd.Series(loss).rolling(14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    tr_components = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - df["close"].shift()).abs(),
            (df["low"] - df["close"].shift()).abs(),
        ],
        axis=1,
    )
    df["atr"] = tr_components.max(axis=1).rolling(14).mean()
    df["vol_ma"] = df["volume"].rolling(20).mean()

    return df.dropna().reset_index(drop=True)


def analyze(symbol: str) -> dict:
    df = get_data(symbol)
    last = df.iloc[-1]

    trend = last["close"] > last["ema21"] and last["ema21"] > last["ema50"]
    breakout = df.iloc[-20:]["close"].max() > df.iloc[-30:-20]["high"].max()
    pullback = abs(last["close"] - last["ema21"]) / last["ema21"] < 0.015
    shrink = df.iloc[-5:]["volume"].mean() < last["vol_ma"] * 0.85
    confirm = last["close"] > last["ema9"] and last["rsi"] > 50

    score = sum([trend * 2, breakout, pullback, shrink * 0.5, confirm * 0.5])

    if score >= 3.5:
        action = "可优先关注"
    elif score >= 2:
        action = "等待确认"
    else:
        action = "暂不考虑"

    return {
        "symbol": symbol.upper(),
        "close": round(float(last["close"]), 2),
        "ema9": round(float(last["ema9"]), 2),
        "ema21": round(float(last["ema21"]), 2),
        "ema50": round(float(last["ema50"]), 2),
        "rsi": round(float(last["rsi"]), 2),
        "atr": round(float(last["atr"]), 2),
        "score": round(float(score), 2),
        "action": action,
        "trend": bool(trend),
        "breakout": bool(breakout),
        "pullback": bool(pullback),
        "shrink": bool(shrink),
        "confirm": bool(confirm),
    }


def format_analyze_result(result: dict) -> str:
    return (
        f"📊 {result['symbol']} 分析结果\n"
        f"收盘价: {result['close']}\n"
        f"EMA9: {result['ema9']}\n"
        f"EMA21: {result['ema21']}\n"
        f"EMA50: {result['ema50']}\n"
        f"RSI: {result['rsi']}\n"
        f"ATR: {result['atr']}\n"
        f"评分: {result['score']}\n"
        f"结论: {result['action']}\n\n"
        f"趋势: {'是' if result['trend'] else '否'}\n"
        f"突破: {'是' if result['breakout'] else '否'}\n"
        f"回踩: {'是' if result['pullback'] else '否'}\n"
        f"缩量: {'是' if result['shrink'] else '否'}\n"
        f"确认: {'是' if result['confirm'] else '否'}"
    )


def get_market_mode():
    df = get_data("SPY")
    last = df.iloc[-1]
    risk = 0
    reasons = []

    if last["close"] < last["ema21"]:
        risk += 1
        reasons.append("SPY 跌破 EMA21")
    else:
        reasons.append("SPY 站上 EMA21")

    if last["ema21"] < last["ema50"]:
        risk += 1
        reasons.append("EMA21 低于 EMA50")
    else:
        reasons.append("EMA21 高于 EMA50")

    if last["rsi"] < 45:
        risk += 1
        reasons.append("RSI 偏弱")
    else:
        reasons.append("RSI 尚可")

    if risk >= 2:
        mode = "防守"
        tip = "控制仓位，少做追高，优先等待强确认信号"
    else:
        mode = "进攻"
        tip = "可以优先关注强势股，但仍需做好止损"
    return mode, reasons, tip


def scan_watchlist(symbols: list[str]):
    priority = []
    wait = []
    exclude = []

    for symbol in symbols:
        try:
            result = analyze(symbol)
            line = f"{result['symbol']} | 评分 {result['score']} | {result['action']}"
            if result["action"] == "可优先关注":
                priority.append(line)
            elif result["action"] == "等待确认":
                wait.append(line)
            else:
                exclude.append(line)
        except Exception as exc:
            exclude.append(f"{symbol.upper()} | 获取失败 | {format_public_error(exc)}")

    return priority, wait, exclude


def build_daily_report() -> str:
    if not DEFAULT_WATCHLIST:
        raise ConfigError("未设置 WATCHLIST，示例：TSLA,AAPL,NVDA")

    mode, reasons, tip = get_market_mode()
    priority, wait, exclude = scan_watchlist(DEFAULT_WATCHLIST)
    lines = [
        "⏰ 开盘前策略扫描",
        f"市场模式: {mode}",
        f"建议: {tip}",
        "原因: " + "；".join(reasons),
        "",
        "🟢 可优先关注:",
        "\n".join(priority[:5]) if priority else "（无）",
        "",
        "🟡 等确认:",
        "\n".join(wait[:5]) if wait else "（无）",
        "",
        "🔴 暂不考虑:",
        "\n".join(exclude[:5]) if exclude else "（无）",
    ]
    return "\n".join(lines)


async def reply_safely(update: Update, text: str):
    if update.message:
        await update.message.reply_text(text)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_safely(
        update,
        "你好，我是你的股票分析 Bot。\n\n"
        "可用命令：\n"
        "/ping - 检查机器人是否在线\n"
        "/chatid - 获取当前聊天 Chat ID\n"
        "/analyze TSLA - 分析单只股票\n"
        "/watchlist - 扫描观察池\n"
        "/risk - 查看市场风险模式\n"
        "/daily - 立即执行每日扫描",
    )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_safely(update, "pong")


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id if update.effective_chat else "未知"
    await reply_safely(
        update,
        f"当前 Chat ID: `{chat_id}`\n\n把它填到 Render 环境变量 `TELEGRAM_CHAT_ID`，机器人才能主动推送每日提醒。",
    )


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await reply_safely(update, "用法：/analyze TSLA")
        return

    symbol = context.args[0].upper()
    await reply_safely(update, f"⏳ 正在分析 {symbol} ...")
    try:
        result = await asyncio.to_thread(analyze, symbol)
        await reply_safely(update, format_analyze_result(result))
    except Exception as exc:
        logging.exception("分析 %s 失败: %s", symbol, mask_secret(str(exc)))
        await reply_safely(update, f"❌ {format_public_error(exc)}")


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_safely(update, "⏳ 正在扫描观察池 ...")
    try:
        priority, wait, exclude = await asyncio.to_thread(scan_watchlist, DEFAULT_WATCHLIST)
        lines = [
            "📋 观察池扫描结果",
            "",
            "🟢 可优先关注:",
            "\n".join(priority[:5]) if priority else "（无）",
            "",
            "🟡 等确认:",
            "\n".join(wait[:5]) if wait else "（无）",
            "",
            "🔴 暂不考虑:",
            "\n".join(exclude[:5]) if exclude else "（无）",
        ]
        await reply_safely(update, "\n".join(lines))
    except Exception as exc:
        logging.exception("观察池扫描失败: %s", mask_secret(str(exc)))
        await reply_safely(update, f"❌ {format_public_error(exc)}")


async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_safely(update, "⏳ 正在评估市场风险 ...")
    try:
        mode, reasons, tip = await asyncio.to_thread(get_market_mode)
        text = (
            f"🌍 市场模式: {mode}\n"
            f"建议: {tip}\n"
            f"原因: {'；'.join(reasons)}"
        )
        await reply_safely(update, text)
    except Exception as exc:
        logging.exception("市场风险评估失败: %s", mask_secret(str(exc)))
        await reply_safely(update, f"❌ {format_public_error(exc)}")


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_safely(update, "⏳ 正在执行每日扫描 ...")
    try:
        text = await asyncio.to_thread(build_daily_report)
        await reply_safely(update, text)
    except Exception as exc:
        logging.exception("每日扫描失败: %s", mask_secret(str(exc)))
        await reply_safely(update, f"❌ {format_public_error(exc)}")


async def scheduled_market_open(context: ContextTypes.DEFAULT_TYPE):
    if not TELEGRAM_CHAT_ID:
        logging.warning("已跳过定时推送：未设置 TELEGRAM_CHAT_ID")
        return

    try:
        text = await asyncio.to_thread(build_daily_report)
    except Exception as exc:
        logging.exception("定时扫描失败: %s", mask_secret(str(exc)))
        text = f"❌ 每日开盘扫描失败：{format_public_error(exc)}"

    await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)


def register_jobs(app):
    if not ALERT_ENABLED:
        logging.info("定时推送已关闭")
        return

    if not TELEGRAM_CHAT_ID:
        logging.warning("未设置 TELEGRAM_CHAT_ID，定时推送不会自动发送")
        return

    if not DEFAULT_WATCHLIST:
        logging.warning("未设置 WATCHLIST，定时推送不会自动发送")
        return

    alert_clock = parse_alert_clock(ALERT_TIME, ALERT_TIMEZONE)
    app.job_queue.run_daily(
        scheduled_market_open,
        time=alert_clock,
        days=(0, 1, 2, 3, 4),
        name="market_open_scan",
    )


def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))


async def run_bot():
    validate_runtime_config()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("risk", cmd_risk))
    app.add_handler(CommandHandler("daily", cmd_daily))
    register_jobs(app)

    await app.initialize()
    await app.updater.start_polling()
    await app.start()
    logging.info("Bot 启动成功")
    while True:
        await asyncio.sleep(3600)


def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()