"""
通知告警模块

支持多种通知渠道：
- Telegram
- Discord
- 邮件（可选）
"""

import requests
import smtplib
import json
from typing import Optional, Dict, Any
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from logger import logger, get_logger
from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)


class Notifier:
    """
    统一通知发送器。

    支持：
    - Telegram Bot
    - Discord Webhook
    - SMTP 邮件
    """

    def __init__(
        self,
        telegram_token: str = None,
        telegram_chat_id: str = None,
        discord_webhook: str = None,
        smtp_host: str = None,
        smtp_port: int = None,
        smtp_user: str = None,
        smtp_pass: str = None,
        email_to: str = None,
    ):
        self.telegram_token = telegram_token or TELEGRAM_BOT_TOKEN
        self.telegram_chat_id = telegram_chat_id or TELEGRAM_CHAT_ID
        self.discord_webhook = discord_webhook
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_pass = smtp_pass
        self.email_to = email_to

        self.logger = get_logger("notifier")

    def send_telegram(
        self,
        message: str,
        parse_mode: str = "HTML",
        disable_notification: bool = False,
    ) -> bool:
        """
        发送 Telegram 消息。

        Args:
            message: 消息内容（支持 HTML 格式）
            parse_mode: 解析模式 (HTML/Markdown)
            disable_notification: 静默发送

        Returns:
            是否发送成功
        """
        if not self.telegram_token or not self.telegram_chat_id:
            return False

        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            data = {
                "chat_id": self.telegram_chat_id,
                "text": message,
                "parse_mode": parse_mode,
                "disable_notification": disable_notification,
            }

            response = requests.post(url, json=data, timeout=10)
            response.raise_for_status()

            result = response.json()
            if result.get("ok"):
                self.logger.debug(f"Telegram 消息发送成功：{message[:50]}...")
                return True
            else:
                self.logger.error(f"Telegram API 返回错误：{result}")
                return False

        except Exception as e:
            self.logger.exception(f"发送 Telegram 消息失败：{e}")
            return False

    def send_discord(
        self,
        message: str,
        title: str = None,
        color: int = 0x00FF00,
        fields: list = None,
    ) -> bool:
        """
        发送 Discord Webhook 消息。

        Args:
            message: 消息内容
            title: 嵌入标题
            color: 嵌入颜色（十进制）
            fields: 嵌入字段列表

        Returns:
            是否发送成功
        """
        if not self.discord_webhook:
            return False

        try:
            embed = {
                "title": title or "Polymarket Bot Alert",
                "description": message,
                "color": color,
            }

            if fields:
                embed["fields"] = fields

            data = {
                "content": None,
                "embeds": [embed],
            }

            response = requests.post(
                self.discord_webhook,
                json=data,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            response.raise_for_status()

            self.logger.debug(f"Discord 消息发送成功：{message[:50]}...")
            return True

        except Exception as e:
            self.logger.exception(f"发送 Discord 消息失败：{e}")
            return False

    def send_email(
        self,
        subject: str,
        body: str,
        html: bool = True,
    ) -> bool:
        """
        发送邮件通知。

        Args:
            subject: 邮件主题
            body: 邮件正文
            html: 是否使用 HTML 格式

        Returns:
            是否发送成功
        """
        if not all([self.smtp_host, self.smtp_port, self.smtp_user, self.smtp_pass, self.email_to]):
            return False

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.smtp_user
            msg["To"] = self.email_to

            # 纯文本版本
            msg.attach(MIMEText(body, "plain", "utf-8"))

            # HTML 版本
            if html:
                html_body = f"""
                <html>
                <body>
                    <h3>Polymarket Bot 通知</h3>
                    <p>{body.replace(chr(10), '<br>')}</p>
                </body>
                </html>
                """
                msg.attach(MIMEText(html_body, "html", "utf-8"))

            # 发送邮件
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_pass)
                server.sendmail(self.smtp_user, self.email_to, msg.as_string())

            self.logger.info(f"邮件发送成功：{subject}")
            return True

        except Exception as e:
            self.logger.exception(f"发送邮件失败：{e}")
            return False

    def send_alert(
        self,
        alert_type: str,
        title: str,
        message: str,
        data: Dict[str, Any] = None,
        urgent: bool = False,
    ) -> bool:
        """
        发送告警通知（统一接口）。

        Args:
            alert_type: 告警类型 (trade, risk, error, system)
            title: 告警标题
            message: 告警内容
            data: 附加数据
            urgent: 是否紧急（使用声音通知）

        Returns:
            是否发送成功
        """
        # 构建格式化消息
        emoji_map = {
            "trade": "📊",
            "risk": "⚠️",
            "error": "❌",
            "system": "🔧",
            "profit": "💰",
            "loss": "📉",
        }

        emoji = emoji_map.get(alert_type, "📢")

        formatted_msg = f"""
{emoji} <b>{title}</b>

{message}
"""

        if data:
            formatted_msg += "\n<b>详情:</b>\n"
            for key, value in data.items():
                formatted_msg += f"  • {key}: {value}\n"

        # 发送到 Telegram
        success = self.send_telegram(
            formatted_msg,
            disable_notification=not urgent,
        )

        # 同时发送到 Discord（如果配置）
        if self.discord_webhook:
            color_map = {
                "trade": 0x00FF00,
                "risk": 0xFFA500,
                "error": 0xFF0000,
                "system": 0x0000FF,
            }

            fields = []
            if data:
                for key, value in data.items():
                    fields.append(
                        {
                            "name": key,
                            "value": str(value),
                            "inline": True,
                        }
                    )

            self.send_discord(
                message=message,
                title=f"{emoji} {title}",
                color=color_map.get(alert_type, 0x808080),
                fields=fields,
            )

        return success

    def send_trade_alert(
        self,
        market_id: str,
        side: str,
        price: float,
        amount: float,
        pnl: float = None,
    ) -> bool:
        """发送交易告警。"""
        alert_type = "profit" if pnl and pnl > 0 else ("loss" if pnl and pnl < 0 else "trade")

        message = f"""
市场：{market_id}
方向：{side}
价格：{price:.4f}
数量：{amount:.2f} USDC
"""
        if pnl is not None:
            message += f"盈亏：{pnl:+.4f} USDC\n"

        return self.send_alert(
            alert_type=alert_type,
            title=f"{'盈利' if pnl and pnl > 0 else '亏损' if pnl and pnl < 0 else '交易'}通知",
            message=message,
            data={"market_id": market_id, "side": side, "price": price},
        )

    def send_risk_alert(
        self,
        risk_type: str,
        description: str,
        current_value: float,
        threshold: float,
    ) -> bool:
        """发送风控告警。"""
        message = f"""
风险类型：{risk_type}
描述：{description}
当前值：{current_value:.4f}
阈值：{threshold:.4f}
"""
        return self.send_alert(
            alert_type="risk",
            title="⚠️ 风控告警",
            message=message,
            data={
                "risk_type": risk_type,
                "current_value": f"{current_value:.4f}",
                "threshold": f"{threshold:.4f}",
            },
            urgent=True,
        )

    def send_error_alert(
        self,
        error_type: str,
        error_message: str,
        traceback: str = None,
    ) -> bool:
        """发送错误告警。"""
        message = f"""
错误类型：{error_type}
错误信息：{error_message}
"""
        if traceback:
            message += f"\n堆栈追踪:\n{traceback[:500]}..."

        return self.send_alert(
            alert_type="error",
            title="❌ 系统错误",
            message=message,
            data={"error_type": error_type},
            urgent=True,
        )

    def send_system_alert(
        self,
        event_type: str,
        description: str,
    ) -> bool:
        """发送系统事件通知。"""
        return self.send_alert(
            alert_type="system",
            title="🔧 系统事件",
            message=description,
            data={"event_type": event_type},
        )

    def send_simple_alert(self, message: str, urgent: bool = False) -> bool:
        """
        简化版告警接口（供 risk_guard 直接调用）。
        """
        return self.send_telegram(
            message=message,
            disable_notification=not urgent,
        )

    async def send_confirm_request(
        self,
        market_info: dict,
        size_usdc: float,
        timeout: int = 60,
    ) -> str:
        """
        向 Telegram 发送带 Inline Button 的确认消息，等待用户点击。

        需要 python-telegram-bot >= 20.0（异步版本）。
        若未安装，则退化为仅发送文本通知。

        Args:
            market_info: 市场信息字典（id, question, ev_raw, side, score）
            size_usdc:   建议仓位金额（USDC）
            timeout:     等待用户响应的最大秒数

        Returns:
            "confirmed" | "rejected" | "timeout"
        """
        if not self.telegram_token or not self.telegram_chat_id:
            return "timeout"

        question = market_info.get("question", market_info.get("id", "?"))
        ev_raw = market_info.get("ev_raw", 0)
        side = market_info.get("side", "?")
        score = market_info.get("score", 0)
        market_id = market_info.get("id", "")

        msg = (
            f"📊 <b>新交易机会</b>\n\n"
            f"市场：{question[:80]}\n"
            f"方向：{side}\n"
            f"EV: {ev_raw:.4f}  评分: {score:.6f}\n"
            f"建议仓位：{size_usdc:.2f} USDC\n"
            f"\n请在 {timeout}s 内确认入场："
        )

        try:
            from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

            bot = Bot(token=self.telegram_token)
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ 入场", callback_data=f"confirm:{market_id}"),
                        InlineKeyboardButton("❌ 跳过", callback_data=f"reject:{market_id}"),
                    ]
                ]
            )

            await bot.send_message(
                chat_id=self.telegram_chat_id,
                text=msg,
                parse_mode="HTML",
                reply_markup=keyboard,
            )

            # 轮询 Telegram updates 等待用户点击
            import asyncio as _aio
            import time as _time

            deadline = _time.time() + timeout
            offset = 0

            while _time.time() < deadline:
                updates = await bot.get_updates(offset=offset, timeout=5)
                for update in updates:
                    offset = update.update_id + 1
                    cb = getattr(update, "callback_query", None)
                    if not cb:
                        continue
                    data = cb.data or ""
                    if data == f"confirm:{market_id}":
                        await cb.answer("✅ 已确认入场")
                        return "confirmed"
                    elif data == f"reject:{market_id}":
                        await cb.answer("❌ 已跳过")
                        return "rejected"
                await _aio.sleep(2)

            return "timeout"

        except ImportError:
            # python-telegram-bot 未安装，退化为文本通知
            self.send_telegram(msg, disable_notification=False)
            self.logger.warning(
                "send_confirm_request: python-telegram-bot 未安装，"
                "已退化为文本通知（无法等待用户确认）"
            )
            return "timeout"
        except Exception as e:
            self.logger.exception(f"send_confirm_request 异常: {e}")
            return "timeout"


# 全局通知器实例
_notifier: Optional[Notifier] = None


def get_notifier() -> Notifier:
    """获取全局通知器实例。"""
    global _notifier
    if _notifier is None:
        _notifier = Notifier()
    return _notifier


def init_notifier(
    telegram_token: str = None,
    telegram_chat_id: str = None,
    discord_webhook: str = None,
) -> Notifier:
    """初始化全局通知器。"""
    global _notifier
    _notifier = Notifier(
        telegram_token=telegram_token,
        telegram_chat_id=telegram_chat_id,
        discord_webhook=discord_webhook,
    )
    return _notifier


# 便捷函数
def notify_trade(**kwargs):
    """发送交易通知。"""
    return get_notifier().send_trade_alert(**kwargs)


def notify_risk(**kwargs):
    """发送风控通知。"""
    return get_notifier().send_risk_alert(**kwargs)


def notify_error(**kwargs):
    """发送错误通知。"""
    return get_notifier().send_error_alert(**kwargs)


def notify_system(**kwargs):
    """发送系统通知。"""
    return get_notifier().send_system_alert(**kwargs)

from polymarket.notifier import *  # noqa

