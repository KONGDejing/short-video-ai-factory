"""Persistent non-paper six-to-four sampling and conservative daily admission accounting.

Content scores are editorial heuristics, never claims about platform algorithms.
No connector, model, scheduler, or production database is invoked by this module.
Callers commit the assignment in the same transaction as their release records.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo

from sqlalchemy import update

from models.factory import FactoryItem, FactoryPublication, FactoryRelease, FactoryRun
from services.factory_release_plan import DAILY_OUTPUT, PLATFORM_LIMITS, THREE_PLATFORM_PLAN, video_platform_limits

CURRENT_DISTRIBUTION_POLICY = "douyin-four-random-v1"
DISTRIBUTION_PLATFORMS = ("douyin", "xiaohongshu", "kuaishou")
DAILY_PLATFORM_LIMIT = 4
RESERVATION_KEY = "quota_reservation"
NEWS_PRIORITY_DAYS = 3
TECH_LAUNCH = re.compile(
    r'(?=.*\b(?:new|introduc\w*|launch\w*|releas\w*|unveil\w*|debut\w*)\b)'
    r'(?=.*\b(?:model|agent|chip|processor|gpu|robot|rocket|spacecraft)\b)|'
    r'(?:发布|推出|首飞|首秀|量产|实测).*(?:模型|智能体|芯片|机器人|火箭)|'
    r'(?:模型|智能体|芯片|机器人|火箭).*(?:发布|推出|首飞|首秀|量产)', re.I)


def _day(value):
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("发行日期必须为 YYYY-MM-DD")
    return value


def local_day(now=None, timezone_name="Asia/Shanghai"):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ZoneInfo(timezone_name)).date().isoformat()


def recent_news_sources(brief, sources, day, timezone_name='Asia/Shanghai', *, max_days=NEWS_PRIORITY_DAYS):
    """Only dated reporting sources cited for facts can give a story priority."""
    if brief.get('freshness') != 'news':
        return []
    day = date.fromisoformat(day)
    cutoff = day - timedelta(days=max_days - 1)
    cited = set(brief.get('source_ids') or [])
    verified = []
    for source in sources:
        if source.get('id') not in cited or source.get('kind') != 'news':
            continue
        try:
            published = source['published_at']
            if len(published) == 10:
                source_day = date.fromisoformat(published)
            else:
                timestamp = datetime.fromisoformat(published.replace('Z', '+00:00'))
                source_day = date.fromisoformat(local_day(timestamp, timezone_name))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if cutoff <= source_day <= day:
            verified.append(source['id'])
    return verified


def priority_news_sources(brief, sources, day, timezone_name='Asia/Shanghai'):
    """Give verified, recent events production priority, independently of measured audience demand."""
    from services.factory_research import is_major_event
    cited = set(recent_news_sources(brief, sources, day, timezone_name, max_days=2))
    return [s['id'] for s in sources if s['id'] in cited and s.get('url') and s.get('text')
            and (is_major_event(s) or TECH_LAUNCH.search(s.get('title') or ''))]


def _no_submit_proven(job):
    result = job.result or {}
    return (job.status == "failed" and result.get("submission_attempted") is False
            and not result.get("submission_uncertain"))


def quota_day(job, timezone_name="Asia/Shanghai"):
    reservation = (job.result or {}).get(RESERVATION_KEY)
    if isinstance(reservation, dict):
        try:
            return _day(reservation.get("day"))
        except (TypeError, ValueError):
            return None  # Unknown dates remain reserved, rather than disappear.
    return local_day(job.created_at, timezone_name) if job.created_at else None


def reserves_quota(job, include_prepares=False):
    if job.action == "publish":
        # queued/running/submitted and unknown results all consume capacity.
        return not _no_submit_proven(job)
    if not include_prepares or job.action != "prepare":
        return False
    result = job.result or {}
    return not (job.status == "failed" and result.get("uploaded") is False
                and not result.get("submission_uncertain"))


def reserved_item_ids(db, platform, day, *, include_prepares=False,
                      timezone_name="Asia/Shanghai", exclude_item_id=None):
    day = _day(day)
    jobs = db.query(FactoryPublication).filter_by(platform=platform).all()
    return {job.item_id for job in jobs
            if job.item_id != exclude_item_id and reserves_quota(job, include_prepares)
            and quota_day(job, timezone_name) in {None, day}}


def publication_usage(db, platform, day, *, include_prepares=False,
                      timezone_name="Asia/Shanghai"):
    """Unique reserved originals; prepare+publish for one item count only once."""
    return len(reserved_item_ids(db, platform, day, include_prepares=include_prepares,
                                 timezone_name=timezone_name))


def guard_publication(db, item, platform, action, *, timezone_name='Asia/Shanghai', now=None):
    from services.factory_team import editor_id
    from services.factory_makeup import authorized_makeup, authorized_owner_crosspost
    from services.factory_breaking_news import registered_order
    if platform not in PLATFORM_LIMITS:
        raise ValueError('此平台没有日常短视频自动发布配额')
    db.execute(update(FactoryRun).where(FactoryRun.id == item.run_id).values(heartbeat_at=FactoryRun.heartbeat_at))
    db.refresh(item, ['brief'])
    run = db.get(FactoryRun, item.run_id)
    route = (item.brief or {}).get('distribution') or {}
    if (not run or run.settings_snapshot.get('distribution_policy') != CURRENT_DISTRIBUTION_POLICY
            or route.get('policy') != CURRENT_DISTRIBUTION_POLICY):
        raise ValueError('作品需要有效的日常分发计划')
    today = local_day(now, timezone_name)
    owner = editor_id(item)
    if (owner not in {'producer', 'producer3'} or run.settings_snapshot.get('production_kind') == 'weekly_papers'
            or item.brief.get('editor_track') == 'paper_explainer'
            or item.content.get('editor_id') == 'producer2'):
        raise ValueError('论文视频走每周人工交付')
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    breaking = registered_order(db, run)
    if breaking and (platform != 'douyin'
            or timestamp.astimezone(timezone.utc).replace(tzinfo=None) >= breaking.due_at
            or run.settings_snapshot.get('breaking_news_source_id') not in (item.content or {}).get('source_ids', [])):
        raise ValueError('突发热点已超过时效或作品缺少指定的事件原文')
    extra = bool(breaking and (breaking.evidence.get('extra') or route.get('day') != today))
    if _day(route.get('day')) != today and not extra:
        raise ValueError('发行日期与今天不符，请显式重新排期后再操作')
    permitted = platform == 'douyin' or (run.settings_snapshot.get('release_plan') == THREE_PLATFORM_PLAN
                                        and route.get('release_plan') == THREE_PLATFORM_PLAN)
    if (not permitted or route.get(f'{platform}_selected') is not True or route.get('platform') != platform
            or route.get('editor_id') != owner):
        raise ValueError('本条没有分配给这个平台的自动发布时段')
    identifiers = {item.id}
    origin = item.brief.get('origin_item_id')
    if origin:
        identifiers.add(origin)
    crosspost = authorized_owner_crosspost(db, item, platform, today)
    prior = db.query(FactoryPublication).filter(FactoryPublication.item_id.in_(identifiers)).all()
    if any((job.platform != platform or (job.item_id != item.id and reserves_quota(job, True)))
           and not (crosspost and job.item_id == origin and job.platform == 'kuaishou') for job in prior):
        raise ValueError('本作品或原始版本已有平台任务，须核对，禁止重复提交')
    occupied = reserved_item_ids(db, platform, today, include_prepares=True,
        timezone_name=timezone_name, exclude_item_id=item.id)
    if platform == 'douyin' and occupied:
        peers = db.query(FactoryItem).filter(FactoryItem.id.in_(occupied))
        occupied.difference_update(peer.id for peer in peers
            if _extra_breaking_item(db, peer, today) or
            (not crosspost and authorized_owner_crosspost(db, peer, platform, today, include_completed=True)))
    makeup = authorized_makeup(db, run, today) if platform == 'douyin' else None
    limits = video_platform_limits(run.settings_snapshot.get('platforms', PLATFORM_LIMITS))
    if platform not in limits:
        raise ValueError('此平台未启用短视频发布')
    limit = (makeup[1] if makeup else limits[platform]) + int(crosspost)
    if not extra and len(occupied) >= limit:
        raise ValueError(f'该平台当天已预留或提交{limit}条作品，未知结果也占额度')
    return {'policy': CURRENT_DISTRIBUTION_POLICY, 'day': today, 'editor_id': owner,
            'timezone': timezone_name, 'reserved_at': timestamp.isoformat(), 'action': action}


def _extra_breaking_item(db, item, day):
    from services.factory_breaking_news import registered_order
    route = (item.brief or {}).get('distribution') or {}
    if route.get('production_kind') != 'breaking_news' and route.get('day') == day:
        return False
    run = db.get(FactoryRun, item.run_id)
    order = registered_order(db, run) if run else None
    return bool(order and (order.evidence.get('extra') or route.get('day') != day))


def assign_distribution(db, run_id, day=None, slots=None, proposal=None):
    """Persist the selection, drawing within each priority group without replacement.

    Explicit owner choices and any account reservation pin membership. Paper
    batches always remain manual. Call under the caller's write transaction.
    """
    from secrets import SystemRandom
    from services.factory_team import editor_id
    from services.factory_editorial_policy import MANUAL_PLATFORMS
    from services.factory_makeup import authorized_makeup, authorized_owner_crosspost
    db.execute(update(FactoryRun).where(FactoryRun.id == run_id).values(heartbeat_at=FactoryRun.heartbeat_at))
    run = db.get(FactoryRun, run_id)
    if not run or run.settings_snapshot.get('distribution_policy') != CURRENT_DISTRIBUTION_POLICY:
        raise ValueError('新增分发必须使用日常非论文6选4规则')
    items = db.query(FactoryItem).order_by(FactoryItem.run_id, FactoryItem.position, FactoryItem.id).all()
    selected = [i for i in items if i.run_id == run_id and i.status != 'deferred']
    by_id = {i.id: i for i in selected}
    requested = {i.id: _day(day) for i in selected} if slots is None else {key: _day(value) for key, value in slots.items()}
    if not requested or set(requested) - set(by_id):
        raise ValueError('日期映射必须引用本批次的有效作品')
    proposal = proposal or {}
    if (not isinstance(proposal, dict) or set(proposal) - set(requested)
            or any(value not in ('douyin', 'manual') for value in proposal.values())):
        raise ValueError('仅可提议入选抖音或保留人工素材')
    if run.settings_snapshot.get('production_kind') == 'breaking_news':
        from services.factory_breaking_news import registered_order
        order = registered_order(db, run)
        if not order or order.status not in {'in_progress', 'completed'} or len(selected) != 1 or len(requested) != 1 or proposal:
            raise ValueError('热点加发必须绑定已核验事件和单条抖音档期')
        item = selected[0]
        if run.settings_snapshot['breaking_news_source_id'] not in item.brief.get('source_ids', []):
            raise ValueError('热点成片必须引用触发本轮的事件原文')
        route = {'policy': CURRENT_DISTRIBUTION_POLICY, 'day': requested[item.id], 'production_kind': 'breaking_news',
                 'editor_id': editor_id(item), 'platform': 'douyin', 'douyin_selected': True,
                 'kuaishou_selected': False, 'reason': '已核验的突发科技热点，成片通过质检后提交抖音。'}
        previous = item.brief.get('distribution')
        if previous and previous != route:
            raise ValueError('已有热点分发日期不能自动覆盖')
        item.brief = {**item.brief, 'distribution': route}
        db.flush()
        return {item.id: route}
    weekly = run.settings_snapshot.get('production_kind') == 'weekly_papers'
    multi = run.settings_snapshot.get('release_plan') == THREE_PLATFORM_PLAN and not weekly
    limits = video_platform_limits(run.settings_snapshot.get('platforms', PLATFORM_LIMITS))
    slot_keys = run.settings_snapshot.get('release_slot_ids', [])
    only_slot = db.get(FactoryRelease, slot_keys[0]) if multi and run.target_count == 1 and len(slot_keys) == 1 else None
    fixed = only_slot.platforms[0] if only_slot and len(only_slot.platforms) == 1 else None
    if fixed and fixed != 'douyin' and 'douyin' in proposal.values():
        raise ValueError('该时段已分配给其他平台')
    news_first = run.settings_snapshot.get('news_first') is True
    output = {}
    for key, planned in requested.items():
        route = (by_id[key].brief or {}).get('distribution')
        if route:
            if route.get('policy') != CURRENT_DISTRIBUTION_POLICY or route.get('day') != planned:
                raise ValueError('已有历史规则或日期不能静默改期，请显式迁移')
            if route.get('editor_id') != editor_id(by_id[key]):
                raise ValueError('已有分发的剪辑师归属不一致')
            if key in proposal and (proposal[key] == 'douyin') != bool(route.get('douyin_selected')):
                raise ValueError('已有随机抽选不可静默覆盖')
            output[key] = dict(route)
    selections = dict(run.settings_snapshot.get('douyin_random_selections') or {})
    for planned in sorted(set(requested.values())):
        group = [i for i in selected if requested.get(i.id) == planned]
        makeup = authorized_makeup(db, run, planned)
        source_day, limit = makeup or (None, DAILY_PLATFORM_LIMIT)
        carried_from = source_day if source_day and source_day != planned else None
        if weekly:
            if any(editor_id(i) != 'producer2' or proposal.get(i.id) == 'douyin' for i in group):
                raise ValueError('每周论文批次只接小影2，禁止选择抖音')
            nominations = set()
        else:
            if any(editor_id(i) == 'producer2' for i in group):
                raise ValueError('论文只能走每周独立批次，不能混入日常6条')
            daily = [i for i in items if i.id not in {x.id for x in group}
                and i.brief.get('distribution', {}).get('policy') == CURRENT_DISTRIBUTION_POLICY
                and i.brief.get('distribution', {}).get('day') == planned
                and i.brief.get('distribution', {}).get('production_kind') != 'weekly_papers'] + group
            crossposts = {i.id for i in daily if authorized_owner_crosspost(
                db, i, 'douyin', planned, include_completed=True)}
            extras = {i.id for i in daily if _extra_breaking_item(db, i, planned)}
            group_ids = {i.id for i in group}
            originals = [i for i in daily if i.id not in crossposts | extras
                         and (i.id in group_ids or i.brief['distribution'].get('platform') is None
                              or i.brief['distribution'].get('platform') in limits)
                         and not i.brief.get('distribution', {}).get('carried_from')
                         and not (carried_from and i.run_id == run.id)]
            if (len(originals) > (DAILY_OUTPUT if multi else 6)
                    or sum(editor_id(i) == 'producer' for i in originals) > 4
                    or sum(editor_id(i) == 'producer3' for i in originals) > (3 if multi else 2)):
                raise ValueError('当天原创数量或剪辑岗位配额已用完')
            reserved = reserved_item_ids(db, 'douyin', planned, include_prepares=True,
                timezone_name=run.settings_snapshot.get('timezone', 'Asia/Shanghai'))
            outside = reserved - group_ids
            outside.update(i.id for i in daily if i.id not in group_ids and i.brief.get('distribution', {}).get('douyin_selected'))
            outside.difference_update(crossposts | extras)
            pinned = {i.id for i in group if output.get(i.id, {}).get('douyin_selected')
                or i.id in reserved or proposal.get(i.id) == 'douyin'}
            capacity = max(0, limit - len(outside))
            if len(pinned) > capacity:
                raise ValueError(f'固定选择与已有任务超过每日{limit}条，保留历史记录等待核对')
            candidates = [i.id for i in group if i.id not in output and i.id not in pinned and proposal.get(i.id) != 'manual'
                          and fixed in (None, 'douyin')]
            draw_count = min(len(candidates), capacity-len(pinned))
            recent = [key for key in candidates if news_first and recent_news_sources(
                by_id[key].brief, run.sources or [], planned, run.settings_snapshot.get('timezone', 'Asia/Shanghai'))]
            urgent = [key for key in recent if priority_news_sources(
                by_id[key].brief, run.sources or [], planned, run.settings_snapshot.get('timezone', 'Asia/Shanghai'))]
            random = SystemRandom()
            priority_draw = random.sample(urgent, min(len(urgent), draw_count)) if urgent and draw_count else []
            ordinary_news = [key for key in recent if key not in urgent]
            news_count = min(len(ordinary_news), draw_count - len(priority_draw))
            news_draw = random.sample(ordinary_news, news_count) if news_count else []
            remaining = [key for key in candidates if key not in recent]
            remaining_count = draw_count - len(priority_draw) - news_count
            drawn = priority_draw + news_draw + (random.sample(remaining, remaining_count) if remaining_count else [])
            nominations = pinned | set(drawn)
            if drawn or planned not in selections:
                selections[planned] = {'method': ('priority_news_then_recent_then_random' if urgent else
                    'recent_news_then_random' if news_first else 'system_random_sample_without_replacement'),
                    'candidate_ids': candidates, 'drawn_ids': drawn,
                    'recent_news_candidate_ids': recent, 'priority_news_candidate_ids': urgent,
                    'selected_ids': sorted(nominations), 'owner_pinned_ids': sorted({k for k,v in proposal.items() if v == 'douyin'} | {i.id for i in group if output.get(i.id,{}).get('owner_pinned')}),
                    'recorded_at': datetime.now(timezone.utc).isoformat()}
        secondary = {}
        if multi and fixed != 'douyin':
            available = [i.id for i in group if i.id not in output and i.id not in nominations
                         and proposal.get(i.id) != 'manual']
            for platform in (fixed,) if fixed else ('kuaishou', 'xiaohongshu'):
                if platform not in run.settings_snapshot.get('platforms', ()):
                    continue
                occupied = reserved_item_ids(db, platform, planned, include_prepares=True,
                    timezone_name=run.settings_snapshot.get('timezone', 'Asia/Shanghai'))
                occupied.update(i.id for i in items
                    if i.brief.get('distribution', {}).get('day') == planned
                    and i.brief['distribution'].get('platform') == platform)
                remaining = max(0, limits[platform] - len(occupied))
                for key in available[:remaining]:
                    secondary[key] = platform
                del available[:remaining]
        for item in group:
            if item.id in output:
                continue
            chosen = item.id in nominations
            platform = 'douyin' if chosen else secondary.get(item.id)
            output[item.id] = {'policy': CURRENT_DISTRIBUTION_POLICY, 'day': planned,
                'production_kind': 'weekly_papers' if weekly else 'daily',
                'editor_id': editor_id(item), 'platform': platform,
                'douyin_selected': chosen, 'kuaishou_selected': platform == 'kuaishou',
                **({'release_plan': THREE_PLATFORM_PLAN,
                    'xiaohongshu_selected': platform == 'xiaohongshu'} if multi else {}),
                'owner_pinned': proposal.get(item.id) == 'douyin',
                'manual_platforms': list(MANUAL_PLATFORMS),
                **({'carried_from': carried_from} if carried_from else {}),
                'reason': ('每周论文独立人工交付。' if weekly else
                    '已分配唯一自动发布平台，保留成片和发布回执。' if multi and platform else
                    '成片与发布文案已保存，等待可用排期。' if multi else
                    '用户明确指定今天上传抖音；保留官方核验、AI声明、署名与禁止下载。' if proposal.get(item.id) == 'douyin' else
                    '抖音优先近两日有原文的重大事件和技术发布，其次近期新闻，同优先级随机。' if chosen and news_first else
                    '日常非论文6条中持久随机抽4条上传抖音，刷新不重抽；保留完整下载包。' if chosen else
                    '未入选抖音，完整成片与文案保留，由本人手动上传快手或小红书。')}
    for key, route in output.items():
        by_id[key].brief = {**(by_id[key].brief or {}), 'distribution': route}
    run.settings_snapshot = {**run.settings_snapshot, 'douyin_random_selections': selections}
    db.flush()
    return output
