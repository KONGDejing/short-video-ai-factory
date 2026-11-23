"""The owner authorizes short-video publishing on three creator accounts.

Credentials and generic switches cannot enable another platform. The runtime
publish switch separately pauses scheduled uploads without changing this scope.
"""
import json
from pathlib import Path

TRANSPORT_STATE = Path(__file__).resolve().parents[2] / '.runtime/creator-transport-status.json'


class AutomationDisabled(ValueError):
    """The requested platform supports manual operation only."""


from services.factory_release_plan import PLATFORM_LIMITS

AUTOMATED_PLATFORMS = frozenset(PLATFORM_LIMITS)


def automation_enabled(platform):
    return str(platform).strip().lower() in AUTOMATED_PLATFORMS


def publication_transport_state(platform):
    try:
        value = json.loads(TRANSPORT_STATE.read_text()).get(platform, {})
        return {key: value[key] for key in ('ready', 'reason', 'checked_at') if key in value}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def effective_automation_enabled(platform):
    return automation_enabled(platform) and publication_transport_state(platform).get('ready') is not False


def analytics_enabled(platform):
    # Restoring upload authorization does not turn on background account browsing.
    return False


def analytics_state(platform):
    return {"platform": str(platform).strip().lower(), "mode": "manual", "enabled": False,
            "reason": "账号数据自动采集保持关闭；由本人提供官方数据，历史统计及采集时间保留。"}


def automation_state(platform):
    platform = str(platform).strip().lower()
    enabled = automation_enabled(platform)
    return {
        "platform": platform,
        "mode": "automated" if enabled else "manual",
        "enabled": enabled,
        "reason": (f'日常排期每天{PLATFORM_LIMITS[platform]}条独立原创，提交时声明AI制作，保留官方回执。'
                   if enabled else '账号自动化已停用，由本人使用平台官方客户端发布。'),
    }


def require_automation(platform):
    state = automation_state(platform)
    if not state["enabled"]:
        raise AutomationDisabled(state["reason"])
