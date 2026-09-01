"""顶层通知模块别名桥接 (收敛至 polymarket.services.notifier)"""
from polymarket.services.notifier import DiscordNotifier, Notifier, get_notifier

__all__ = ["DiscordNotifier", "Notifier", "get_notifier"]
