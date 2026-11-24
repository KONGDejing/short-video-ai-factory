"""Read-only connectors for platform-authorized work statistics."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

import httpx


CONFIG = Path(__file__).resolve().parents[2] / '.runtime/operations/work-metrics.json'
FIELDS = {'douyin': ('app_id', 'app_secret', 'account_id', 'open_id', 'refresh_token'),
          'kuaishou': ('app_id', 'app_secret', 'refresh_token')}


def _config():
    try:
        if CONFIG.is_file() and not CONFIG.stat().st_mode & 0o077:
            value = json.loads(CONFIG.read_text())
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def credentials(platform):
    entry = _config().get(platform)
    if platform in FIELDS and isinstance(entry, dict) and all(
            isinstance(entry.get(key), str) and entry[key].strip() for key in FIELDS[platform]):
        return entry


def _oauth_payload(response):
    if response.status_code != 200:
        raise ValueError('抖音开放平台凭证服务未响应')
    data = response.json().get('data') or {}
    if type(data.get('error_code')) is not int or data['error_code'] != 0:
        raise ValueError('抖音开放平台凭证无效或已过期')
    return data


def _access_token(client, platform, auth):
    now = datetime.now(timezone.utc)
    try:
        valid_until = datetime.fromisoformat(auth.get('access_expires_at', ''))
        if valid_until - now > timedelta(minutes=15) and auth.get('access_token'):
            return auth['access_token']
    except (ValueError, TypeError):
        pass
    if platform == 'douyin':
        token = _oauth_payload(client.post('https://open.douyin.com/oauth/refresh_token/',
            data={'client_key': auth['app_id'], 'grant_type': 'refresh_token', 'refresh_token': auth['refresh_token']}))
        if token.get('open_id') != auth['open_id']:
            raise ValueError('抖音刷新授权返回的账号不一致')
    else:
        token = _payload(client.get('https://open.kuaishou.com/oauth2/refresh_token', params={
            'app_id': auth['app_id'], 'app_secret': auth['app_secret'],
            'grant_type': 'refresh_token', 'refresh_token': auth['refresh_token']}), 'result', 1)
    access, refresh, seconds = token['access_token'], token['refresh_token'], int(token['expires_in'])
    if not (isinstance(access, str) and access and isinstance(refresh, str) and refresh and seconds > 0):
        raise ValueError('开放平台返回的凭证信息无效')
    current = _config()
    if current.get(platform) != auth:
        raise ValueError('采集凭证已被更新，等待下一轮')
    current[platform] = {**auth, 'access_token': access, 'refresh_token': refresh,
                         'access_expires_at': (now + timedelta(seconds=seconds)).isoformat()}
    with NamedTemporaryFile(mode='w', encoding='utf-8', dir=CONFIG.parent, delete=False) as temp:
        try:
            json.dump(current, temp)
            temp.flush()
            Path(temp.name).replace(CONFIG)
        finally:
            Path(temp.name).unlink(missing_ok=True)
    return access


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _work(identifier, title, timestamp, **metrics):
    if not isinstance(identifier, (str, int)) or isinstance(identifier, bool) or not identifier:
        return None
    if not isinstance(title, str) or not title.strip() or type(timestamp) is not int or timestamp <= 0:
        return None
    try:
        published = datetime.fromtimestamp(timestamp, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    if _count(metrics.get('views')) is None:
        return None
    return {'platform_work_id': str(identifier), 'title': title, 'published_at': published.isoformat(),
            **{key: _count(value) for key, value in metrics.items()}}


def _payload(response, status_key, success):
    # Keep token-bearing request URLs and raw response bodies out of logs and storage.
    if response.status_code != 200:
        raise ValueError('开放接口 HTTP 请求失败')
    value = response.json()
    if not isinstance(value, dict) or type(value.get(status_key)) is not int or value[status_key] != success:
        raise ValueError('开放接口未授权或数据查询失败')
    return value


def _douyin_video_id(url):
    address = urlsplit(url or '')
    if address.scheme == 'https' and address.hostname in {'www.douyin.com', 'douyin.com'}:
        match = re.fullmatch(r'/video/(\d+)/?', address.path)
        if match:
            return match[1]
    return None


def _douyin(client, auth):
    host = 'https://open.douyin.com'
    token = _oauth_payload(client.post(host + '/oauth/stable_client_token/', json={
        'client_key': auth['app_id'], 'client_secret': auth['app_secret'], 'grant_type': 'client_credential'}))
    headers = {'access-token': token['access_token']}
    index = _payload(client.post(host + '/aweme/v1/video/self/get_user_video_list/', headers=headers,
        json={'aweme_id': auth['account_id'], 'cursor': 0, 'count': 40}), 'err_no', 0)
    ids = list(dict.fromkeys(identifier for entry in index['data']['user_publish_item_list']
        if (identifier := _douyin_video_id(entry.get('video_link')))))
    works = []
    access_token = _access_token(client, 'douyin', auth) if ids else None
    for offset in range(0, len(ids), 20):
        converted = _payload(client.post(host + '/api/apps/v1/convert_video_id/video_id_to_open_item_id/',
            headers=headers, json={'video_ids': ids[offset:offset + 20],
                                   'app_id': auth['app_id'], 'access_key': auth['app_id']}), 'err_no', 0)
        conversion = {item_id: video_id for video_id, item_id in converted['data']['convert_result'].items()
                      if isinstance(item_id, str) and item_id and video_id in ids}
        if not conversion:
            continue
        detail = _payload(client.post(host + '/api/apps/v1/video/query/',
            headers={'access-token': access_token}, params={'open_id': auth['open_id']},
            json={'item_ids': list(conversion)}), 'err_no', 0)['data']
        if detail['extra'].get('error_code') != 0:
            raise ValueError('抖音作品查询未获授权或未返回数据')
        for entry in detail['data']['list']:
            stats = entry.get('statistics') or {}
            works.append(_work(conversion.get(entry.get('item_id')), entry.get('title'), entry.get('create_time'),
                views=stats.get('play_count'), likes=stats.get('digg_count'),
                comments=stats.get('comment_count'), shares=stats.get('share_count')))
    return [work for work in works if work]


def _kuaishou(client, auth):
    payload = _payload(client.get('https://open.kuaishou.com/openapi/photo/list',
        params={'app_id': auth['app_id'], 'access_token': _access_token(client, 'kuaishou', auth), 'count': 40}), 'result', 1)
    works = []
    for entry in payload['video_list']:
        timestamp = entry.get('create_time')
        if entry.get('pending') is False and type(timestamp) is int:
            works.append(_work(entry.get('photo_id'), entry.get('caption'), timestamp // 1000,
                views=entry.get('view_count'), likes=entry.get('like_count'), comments=entry.get('comment_count')))
    return [work for work in works if work]


def capture(platform, auth):
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        if platform == 'douyin':
            return _douyin(client, auth)
        if platform == 'kuaishou':
            return _kuaishou(client, auth)
    raise ValueError('该平台未接入官方作品数据接口')
