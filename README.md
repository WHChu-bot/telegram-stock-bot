# Telegram Stock Bot

这是一个 Telegram 股票分析机器人，支持：

- `/ping` 检查机器人是否在线
- `/chatid` 获取当前聊天 ID
- `/analyze TSLA` 分析单只股票
- `/watchlist` 扫描观察池
- `/risk` 查看市场风险模式
- `/daily` 立即执行每日扫描

## 本地运行前需要准备

你需要准备这些环境变量：

- `BOT_TOKEN`
- `FINNHUB_KEY`
- `WATCHLIST`
- `TELEGRAM_CHAT_ID`
- `ALERT_ENABLED`
- `ALERT_TIME`
- `ALERT_TIMEZONE`

## Render 部署

后面可以把这个项目部署到 Render。

## 免费版注意事项

Render 免费版会休眠，因此不能保证绝对 24 小时不间断运行。