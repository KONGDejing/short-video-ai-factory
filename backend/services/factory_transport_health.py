"""Bounded, unauthenticated public connectivity checks for the restored Kuaishou route.

No account cookie, browser, upload or generic overseas proxy is used. A healthy
network is separate from account login, the official location control and a
successful upload receipt.
"""
from datetime import datetime, timezone
import fcntl
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
STATUS_FILE = ROOT / '.runtime/creator-connectors/kuaishou/transport.json'
PROXY_FILE = ROOT / '.runtime/kuaishou-publishing.env'
INTERVAL_SECONDS = 300
_last_session_attempt = float('-inf')


def proxy_url():
    """The browser and health probe use the same private, Kuaishou-only route."""
    try:
        value = dotenv_values(PROXY_FILE, interpolate=False).get('CREATOR_BROWSER_PROXY_KUAISHOU') if PROXY_FILE.is_file() else None
        parts = urlsplit(value) if value else None
        if parts and parts.scheme in {'http', 'https', 'socks5'} and parts.hostname:
            port = parts.port
            if parts.hostname not in {'127.0.0.1', 'localhost', '::1'} or port == 18792:
                return value
    except (OSError, ValueError):
        pass
    return None


def _recent_state(now, max_age):
    try:
        state = json.loads(STATUS_FILE.read_text())
        if not isinstance(state, dict) or not isinstance(state.get('ready'), bool):
            return None
        age = (now - datetime.fromisoformat(state['checked_at'])).total_seconds()
        route_unchanged = (PROXY_FILE.stat().st_mtime <= STATUS_FILE.stat().st_mtime
                           if PROXY_FILE.exists() else state.get('ready') is False)
        if 0 <= age < max_age and route_unchanged:
            return state
    except (OSError, KeyError, TypeError, ValueError):
        pass
    return None


def transport_state():
    state = _recent_state(datetime.now(timezone.utc), INTERVAL_SECONDS * 2)
    if state:
        return {key: state[key] for key in ('ready', 'reason', 'checked_at') if key in state}
    return {'ready': False, 'reason': '等待快手独立线路连通性核验'}


def restore_saved_session(state):
    """Resume the owned profile after recovery, without uploading or submitting."""
    from services.factory_platform_policy import automation_enabled
    if not state or not state.get('ready') or not automation_enabled('kuaishou'):
        return
    from services.creator_connector import _alive, _ensure_daemon
    if _alive('kuaishou'):
        return
    global _last_session_attempt
    now = time.monotonic()
    if now - _last_session_attempt < INTERVAL_SECONDS:
        return
    _last_session_attempt = now
    _ensure_daemon('kuaishou')


def _probe(proxy):
    try:
        with httpx.Client(proxy=proxy, trust_env=False, timeout=8, follow_redirects=False) as client:
            with client.stream('GET', 'https://cp.kuaishou.com/') as response:
                if 200 <= response.status_code < 400:
                    return True, '快手官方公共入口HTTPS已连通；登录、北京地点控件及发布回执分别核验'
                return False, f'快手官方公共入口返回HTTP {response.status_code}，自动上传继续等待'
    except (httpx.HTTPError, ValueError, ImportError) as error:
        return False, f'快手独立北京线路连接官方后台失败（{type(error).__name__}），作品保留待发'


def refresh_kuaishou_transport(now=None):
    from services.factory_platform_policy import automation_enabled
    if not automation_enabled('kuaishou'):
        return None
    current = now or datetime.now(timezone.utc)
    STATUS_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = STATUS_FILE.with_suffix('.health.lock')
    with lock_path.open('a') as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        prior = _recent_state(current, INTERVAL_SECONDS)
        if prior:
            return prior
        proxy = proxy_url()
        if not proxy:
            ready, reason = False, '缺少快手独立北京代理配置'
        else:
            ready, reason = _probe(proxy)
        value = {'ready': ready, 'reason': reason, 'checked_at': current.isoformat(),
                 'check_scope': 'unauthenticated_public_homepage_headers_only', 'retry_seconds': INTERVAL_SECONDS}
        temporary = STATUS_FILE.with_suffix('.health.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
        temporary.chmod(0o600); temporary.replace(STATUS_FILE)
        return value


if __name__ == '__main__':
    result = refresh_kuaishou_transport()
    if result and result.get('ready'):
        from database import SessionLocal
        from models.factory import FactoryConfig
        with SessionLocal() as db:
            config = db.get(FactoryConfig, 1)
            if config and config.publish_enabled and 'kuaishou' in config.platforms:
                restore_saved_session(result)
