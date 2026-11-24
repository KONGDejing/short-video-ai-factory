"""Hourly source checks and durable priority work for confirmed technology news."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import re
from urllib.parse import urlsplit

from models.factory import FactoryConfig, FactoryItem, FactoryRelease, FactoryRun, FactoryWorkOrder


WATCH_KEY = 'hourly-tech-news-watch'
KIND = 'breaking_news'
INTERVAL = timedelta(hours=1)
FRESHNESS = timedelta(hours=12)
PLATFORM = 'douyin'
PRESS_FEEDS = ('https://feeds.bbci.co.uk/news/technology/rss.xml',
               'https://feeds.arstechnica.com/arstechnica/index')


def utc(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def event_keys(source):
    address = urlsplit(source.get('url') or '')
    path = address.path.rstrip('/').removesuffix('/default.aspx').rstrip('/')
    title = re.sub(r'[\W_]+', '', (source.get('title') or '').casefold())
    return {key for key in (source.get('id'), path.rsplit('/', 1)[-1].casefold() if path else None, title)
            if key and len(key) >= 12}


def _known_events(db, now, *, include_orders=False):
    cutoff = utc(now - timedelta(days=7))
    keys = set()
    if include_orders:
        for order in db.query(FactoryWorkOrder).filter_by(kind=KIND).filter(FactoryWorkOrder.created_at >= cutoff):
            keys.update(event_keys(order.evidence.get('source') or {}))
    recent = db.query(FactoryItem, FactoryRun).join(FactoryRun, FactoryRun.id == FactoryItem.run_id).filter(
        FactoryItem.status.notin_(['failed', 'deferred']), FactoryItem.created_at >= cutoff)
    for item, run in recent:
        refs = set(item.brief.get('source_ids', []))
        keys.update(refs)
        for source in run.sources or []:
            if source.get('id') in refs:
                keys.update(event_keys(source))
    return keys


def _priority(source, now):
    from services.factory_distribution import TECH_LAUNCH
    from services.factory_research import _parse_date, _validated_url, is_major_event

    if (source.get('kind') != 'news' or not source.get('id') or not source.get('url')
            or len((source.get('text') or '').strip()) < 200):
        return None
    try:
        _validated_url(source['url'])
    except ValueError:
        return None
    published = _parse_date(source.get('published_at'))
    if not published or not now - FRESHNESS <= published <= now:
        return None
    if is_major_event(source):
        return 'producer3', published
    if TECH_LAUNCH.search(source.get('title') or ''):
        return 'producer', published
    return None


def record_scan(db, now, result):
    known = _known_events(db, now, include_orders=True)
    discovered = 0
    matches = [(priority, source) for source in result['sources'] if (priority := _priority(source, now))]
    for (editor, published), source in sorted(matches, key=lambda pair: pair[0][1], reverse=True):
        keys = event_keys(source)
        key = 'breaking:' + hashlib.sha256(source['id'].encode()).hexdigest()[:28]
        if known & keys or db.get(FactoryWorkOrder, key):
            continue
        related = [row for row in result['sources'] if row.get('watch_company') == source.get('watch_company')
                   and row.get('id') != source['id']][:3] if source.get('watch_company') else []
        db.add(FactoryWorkOrder(key=key, kind=KIND, owner=editor, due_at=utc(published + FRESHNESS),
            title=source['title'][:200], acceptance='引用本事件原文并通过成片质检，收到平台提交回执后验收。',
            evidence={'source': source, 'related': related, 'discovered_at': now.isoformat(), 'platform': PLATFORM}))
        known.update(keys)
        discovered += 1
    watch = db.get(FactoryWorkOrder, WATCH_KEY)
    if not watch:
        watch = FactoryWorkOrder(key=WATCH_KEY, kind='breaking_news_watch', owner='researcher3',
            title='每小时核查科技热点', acceptance='按公开来源记录本次巡检、发现的事件与证据。')
        db.add(watch)
    watch.due_at, watch.status = utc(now + INTERVAL), 'in_progress'
    watch.evidence = {'checked_at': now.isoformat(), 'new_events': discovered,
                      'source_count': len(result['sources']), 'warning_count': len(result.get('warnings', [])),
                      'press_lead_count': result.get('press_lead_count', 0),
                      'result': 'scanned' if result['sources'] else 'sources_unavailable'}
    db.commit()
    return watch.evidence


def scan_if_due(now=None):
    from database import SessionLocal
    from services.factory_news_intake import collect_company_news
    from services.factory_research import _load_feed, _parse_date
    from services.factory_resources import memory_state

    now = now or datetime.now(timezone.utc)
    with SessionLocal() as db:
        config = db.get(FactoryConfig, 1)
        watch = db.get(FactoryWorkOrder, WATCH_KEY)
        if not config or not config.schedule_enabled or (watch and watch.due_at > utc(now)):
            return None
    if memory_state()['blocked']:
        return None
    with ThreadPoolExecutor(max_workers=len(PRESS_FEEDS)) as network:
        feeds = list(network.map(lambda url: _load_feed(url, now), PRESS_FEEDS))
    headlines = [row['title'] for rows, _ in feeds for row in rows
                 if (published := _parse_date(row.get('published_at'))) and now - FRESHNESS <= published <= now]
    # Press headlines only order retrieval. A dated first-party original is needed to start production.
    result = collect_company_news(limit=14, search_hints=headlines)
    result['press_lead_count'] = len(headlines)
    result['warnings'].extend(warning for _, warning in feeds if warning)
    with SessionLocal() as db:
        return record_scan(db, now, result)


def registered_order(db, run):
    """Validate the persistent pairing behind a rush slot or quota exception."""
    snapshot = run.settings_snapshot or {}
    key = snapshot.get('breaking_news_key')
    order = db.get(FactoryWorkOrder, key) if key else None
    if not order or order.kind != KIND or order.status not in ('in_progress', 'completed', 'blocked'):
        return None
    evidence = order.evidence or {}
    slot_id = evidence.get('release_id')
    if not slot_id or evidence.get('run_id') != run.id or snapshot.get('release_slot_ids') != [slot_id]:
        return None
    slot = db.get(FactoryRelease, slot_id)
    if (not slot or slot.run_id != run.id or slot.platforms != [PLATFORM]
            or evidence.get('source', {}).get('id') != snapshot.get('breaking_news_source_id')):
        return None
    return order


def _regular_slot(db, config, now, owner):
    from services.factory_release_plan import scheduled_times
    from services.factory_schedule import nominal_time
    from services.factory_team import DAILY_EDITOR_ORDER

    if not config.production_on_release_clock:
        return None
    times = [utc(now.replace(hour=int(clock[:2]), minute=int(clock[3:]), second=0, microsecond=0))
             for (clock, platform), editor in zip(scheduled_times(config)[:config.daily_target], DAILY_EDITOR_ORDER)
             if platform == PLATFORM and editor == owner]
    return db.query(FactoryRelease).filter(nominal_time().in_(times), FactoryRelease.run_id.is_(None),
        FactoryRelease.item_id.is_(None), FactoryRelease.status.in_(['scheduled', 'queued', 'waiting_media']),
        FactoryRelease.due_at >= utc(now)).order_by(FactoryRelease.due_at).first() if times else None


def _reconcile(db, now):
    from services.factory_service import TERMINAL

    for order in db.query(FactoryWorkOrder).filter(FactoryWorkOrder.kind == KIND,
            FactoryWorkOrder.status.in_(['in_progress', 'blocked'])):
        evidence = order.evidence or {}
        run = db.get(FactoryRun, evidence['run_id'])
        slot = db.get(FactoryRelease, evidence['release_id'])
        if slot and slot.status == 'submitted':
            order.status = 'completed'
            order.evidence = {**evidence, 'resolution': '已取得抖音官方提交回执。'}
        elif slot and (slot.status in ('missed', 'partial', 'cancelled') or
                       run and run.status in TERMINAL and not run.completed_count):
            order.status = 'blocked'
            detail = slot.results.get(PLATFORM, {}).get('message', '')
            order.evidence = {**evidence, 'resolution': run.error if run and run.error else detail or '待检查制作日志。'}
        elif slot and slot.status == 'blocked':
            message = slot.results.get(PLATFORM, {}).get('message')
            if message and evidence.get('blocker') != message:
                order.evidence = {**evidence, 'blocker': message}
    for order in db.query(FactoryWorkOrder).filter_by(kind=KIND, status='pending').filter(
            FactoryWorkOrder.due_at <= utc(now)):
        order.status = 'expired'
        order.evidence = {**order.evidence, 'resolution': '已超过热点制作时效。'}
    db.commit()


def dispatch(db, config, now):
    from services.factory_service import start_run

    _reconcile(db, now)
    if not (config.schedule_enabled and config.publish_enabled and PLATFORM in config.platforms):
        return None
    if db.query(FactoryRun.id).filter(FactoryRun.active_slot.isnot(None)).first():
        return None
    pending = db.query(FactoryWorkOrder).filter_by(kind=KIND, status='pending').order_by(
        FactoryWorkOrder.due_at.desc()).all()
    if not pending:
        return None
    fixed = _known_events(db, now)
    for order in pending:
        if fixed & event_keys(order.evidence['source']):
            order.status = 'completed'
            order.evidence = {**order.evidence, 'resolution': '本事件已有生产记录。'}
            continue
        slot = _regular_slot(db, config, now, order.owner)
        try:
            run = start_run(db, target_count=1, trigger='release_clock', schedule_key=order.key,
                            release_slot_ids=[slot.id] if slot else None, breaking_order=order, now=now,
                            editor_quota_override={owner: int(owner == order.owner)
                                for owner in ('producer', 'producer2', 'producer3')})
        except (ValueError, RuntimeError) as exc:
            db.rollback()
            return {'status': 'deferred', 'reason': str(exc)}
        return {'status': run.status, 'run_id': run.id}
    db.commit()
    return None


def overview(db):
    watch = db.get(FactoryWorkOrder, WATCH_KEY)
    rows = db.query(FactoryWorkOrder).filter_by(kind=KIND).order_by(FactoryWorkOrder.due_at.desc()).limit(8)
    return {'interval_minutes': 60, 'last_check': watch.evidence if watch else None,
            'next_check': watch.due_at.replace(tzinfo=timezone.utc).isoformat() if watch else None,
            'events': [{'title': row.title, 'status': row.status,
                        'source_url': row.evidence.get('source', {}).get('url'),
                        'message': row.evidence.get('resolution') or row.evidence.get('blocker')}
                       for row in rows]}
