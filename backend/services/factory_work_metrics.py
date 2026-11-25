"""Match platform work observations and adjust future editorial priority."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import re
from statistics import median

from models.factory import FactoryItem, FactoryPublication, FactoryWorkMetricsSnapshot, FactoryWorkOrder
from services import factory_work_metrics_sources as sources


PLATFORMS = ('douyin', 'kuaishou', 'xiaohongshu')
INTERVAL = timedelta(minutes=30)
COMPARISON_DAYS = 14
TOPICS = {
    '重大人事与公司决策': r'复职|回归|拍板|离职|卸任|任命|董事会|并购|收购|合并|重组|诉讼|调查|reinstat|acquisition|\bCEO\b',
    '科技股与产业': r'股价|股市|科技股|股大涨|收涨|收跌|市场反应|财报|关税|出口管制|上市|stock|earnings|\bIPO\b',
    'AI场景对照': r'记住场景|场景变化|时序一致|实时改画面|实时变换|AI视频对照',
    '大模型与AI应用': r'大模型|模型.{0,8}(?:发布|更新|能力)|(?:GPT|Claude|Gemini|DeepSeek|千问|豆包|智能体|AI软件|AI应用|生成视频)',
    '机器人与前沿场景': r'机器人|机器手|具身|无人机|无人船|自动驾驶|航天|太空|火箭|robot|spacecraft',
    '芯片与智能硬件': r'芯片|算力|显卡|硬件|端侧|存储|\bGPU\b|chip',
}

PLATFORM_DIRECTIONS = {
    'douyin': (
        (('重大人事与公司决策', '机器人与前沿场景'),
         '优先挑有新进展的知名人物与公司决策，或一眼能看懂的机器人真实操作。首镜头给当事人、原始公告或动作结果，开场一句说清变化和观众要追的问题。'),
        (('芯片与智能硬件', '科技股与产业'),
         '芯片能力与产业事件需要可核对的具体数字或前后差异，交代比较对象、口径与影响。'),
    ),
    'kuaishou': (
        (('机器人与前沿场景', 'AI场景对照'),
         '优先展示机器人怎么做、哪里失败、怎样改对，或模型对同一场景的前后差异。开场先给动作或结果，中间用日常话解释原因和能否用在真实环境。'),
        (('科技股与产业',),
         '产业和行情只用可核对的数字与具体影响开场，给出当天变化、比较基准及跟涨或回撤范围。'),
    ),
}


def platform_direction(brief, platform):
    """Tie-break equally fresh stories using the owner's account review."""
    rules = PLATFORM_DIRECTIONS.get(platform, ())
    name = topic(brief)
    return next((rank for rank, (topics, _) in enumerate(rules) if name in topics), len(rules))


def platform_direction_brief(platform):
    return [guidance for _, guidance in PLATFORM_DIRECTIONS.get(platform, ())]


def topic(brief):
    for field in ('title', 'category'):
        text = brief.get(field) or ''
        for category, pattern in TOPICS.items():
            if re.search(pattern, text, re.I):
                return category
    return '其他科技'


def _utc(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def _naive(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _normalized_title(text):
    result = re.sub(r'#\S+', '', text or '')
    result = re.sub(r'内容由\s*AI\s*生成(?:（[^）]*）)?', '', result, flags=re.I)
    return re.sub(r'[\W_]+', '', result, flags=re.UNICODE).casefold()


def match_works(db, platform, works, now):
    from services.factory_publishing import _valid_evidence

    accepted = []
    jobs = db.query(FactoryPublication, FactoryItem).join(FactoryItem, FactoryItem.id == FactoryPublication.item_id)
    jobs = jobs.filter(FactoryPublication.platform == platform, FactoryPublication.action == 'publish',
                       FactoryPublication.status == 'submitted',
                       FactoryPublication.created_at >= _naive(now - timedelta(days=COMPARISON_DAYS + 1)))
    for job, item in jobs:
        evidence = (job.result or {}).get('evidence') or {}
        if not _valid_evidence(platform, evidence):
            continue
        titles = {item.content.get('title'), item.brief.get('title')}
        for package in item.packages or []:
            if package.get('platform') == platform:
                titles.update((package.get('title'), package.get('caption')))
        try:
            submitted = _utc((job.result or {}).get('updated_at') or '')
        except (ValueError, AttributeError):
            submitted = job.created_at.replace(tzinfo=timezone.utc)
        identifier = evidence.get('item_id') or evidence.get('photo_id') or evidence.get('note_id')
        accepted.append((item, str(identifier) if identifier else None, submitted, set(filter(None, map(_normalized_title, titles)))))

    matches = defaultdict(list)
    for work in works:
        published = _utc(work['published_at'])
        if not now - timedelta(days=COMPARISON_DAYS + 1) <= published <= now:
            continue
        exact = [item for item, identifier, _, _ in accepted if identifier == work['platform_work_id']]
        title = _normalized_title(work['title'])
        matching = exact or [item for item, identifier, submitted, titles in accepted
            if not identifier and title in titles and abs(submitted - published) <= timedelta(hours=24)]
        if len(matching) == 1:
            item = matching[0]
            brief = {**item.brief, 'title': item.content.get('title') or item.brief.get('title', '')}
            matches[item.id].append({**work, 'title': brief['title'], 'item_id': item.id,
                                     'topic': topic(brief)})
    return [entries[0] for entries in matches.values() if len(entries) == 1]


def collect_due(db, now=None):
    now = now or datetime.now(timezone.utc)
    for platform in PLATFORMS:
        auth = sources.credentials(platform)
        if not auth:
            continue
        last = _latest(db, platform)
        if last and last.collected_at > _naive(now - INTERVAL):
            continue
        try:
            works = sources.capture(platform, auth)
            matched = match_works(db, platform, works, now)
            status, data = 'connected', {'works': matched, 'platform_returned': len(works)}
        except Exception:
            status, data = 'unavailable', {'message': '开放接口采集失败，请核对授权与接口状态'}
        db.add(FactoryWorkMetricsSnapshot(platform=platform, status=status, data=data, collected_at=_naive(now)))
        db.commit()


def _latest(db, platform, *, connected=False):
    query = db.query(FactoryWorkMetricsSnapshot).filter_by(platform=platform)
    if connected:
        query = query.filter_by(status='connected')
    return query.order_by(FactoryWorkMetricsSnapshot.collected_at.desc(), FactoryWorkMetricsSnapshot.id.desc()).first()


def recommend_topics(db, platform, now=None):
    """Compare each work once, using its closest available observation to 24h."""
    if platform not in PLATFORMS:
        return []
    now = now or datetime.now(timezone.utc)
    rows = db.query(FactoryWorkMetricsSnapshot).filter_by(platform=platform, status='connected').filter(
        FactoryWorkMetricsSnapshot.collected_at >= _naive(now - timedelta(days=COMPARISON_DAYS)),
        FactoryWorkMetricsSnapshot.collected_at <= _naive(now)).all()
    window = {}
    for row in rows:
        observed = row.collected_at.replace(tzinfo=timezone.utc)
        for work in row.data.get('works', []):
            published = _utc(work['published_at'])
            if published < now - timedelta(days=COMPARISON_DAYS):
                continue
            age_hours = (observed - published).total_seconds() / 3600
            if 18 <= age_hours <= 30:
                error = (abs(age_hours - 24), row.id)
                key = work['item_id']
                if key not in window or error < window[key][0]:
                    window[key] = error, work
    groups = defaultdict(list)
    for _, work in window.values():
        groups[work['topic']].append(work['views'])
    boosts = []
    for name, own in groups.items():
        others = [views for other, values in groups.items() if other != name for views in values]
        if len(own) < 3 or len(others) < 3:
            continue
        typical, baseline = median(own), median(others)
        if typical > 0 and typical >= baseline * 1.2 and typical > baseline:
            boosts.append({'topic': name, 'sample_count': len(own), 'comparison_count': len(others),
                           'median_views': typical, 'comparison_median_views': baseline})
    return sorted(boosts, key=lambda row: (-row['median_views'], row['topic']))


def overview(db):
    result = []
    now = datetime.now(timezone.utc)
    for platform in PLATFORMS:
        latest = _latest(db, platform)
        good = _latest(db, platform, connected=True)
        if not sources.credentials(platform):
            status = 'awaiting_authorization' if platform in sources.FIELDS else 'not_connected'
        elif not latest:
            status = 'waiting'
        elif good and (latest.status != 'connected' or good.collected_at < _naive(now - 4 * INTERVAL)):
            status = 'stale'
        else:
            status = latest.status
        result.append({'platform': platform, 'status': status,
                       'collected_at': good.collected_at.replace(tzinfo=timezone.utc).isoformat() if good else None,
                       'works': sorted(good.data.get('works', []), key=lambda w: w['published_at'], reverse=True)[:10] if good else [],
                       'preferred_topics': recommend_topics(db, platform, now)})
    feedback = db.query(FactoryWorkOrder).filter_by(kind='editorial_feedback').order_by(
        FactoryWorkOrder.created_at.desc()).limit(5).all()
    return {'platforms': result, 'refresh_minutes': 30,
            'owner_observations': [order.evidence for order in feedback]}
