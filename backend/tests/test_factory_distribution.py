"""Publication-ledger invariants. Three-track routing is covered by test_factory_three_tracks."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models.factory import FactoryItem, FactoryPublication, FactoryRelease, FactoryRun, FactoryWorkOrder
from services import factory_distribution as distribution

DAY = '2026-11-11'
NOW = datetime(2026, 11, 11, 4, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'distribution.db'}")
    Base.metadata.create_all(engine, tables=[FactoryRun.__table__, FactoryItem.__table__, FactoryPublication.__table__,
                                             FactoryRelease.__table__, FactoryWorkOrder.__table__])
    with sessionmaker(bind=engine)() as session:
        yield session
        session.rollback()
    engine.dispose()


def run_items(db, titles, policy=True):
    run = FactoryRun(active_slot=None, settings_snapshot={'timezone': 'Asia/Shanghai',
        **({'distribution_policy': distribution.CURRENT_DISTRIBUTION_POLICY} if policy else {})})
    db.add(run); db.flush()
    run.active_slot = None
    db.flush()
    items = [FactoryItem(run_id=run.id, position=i, brief={'title': title, 'editor_id': 'producer2'})
             for i, title in enumerate(titles)]
    db.add_all(items); db.flush()
    return run, items


def route(platform, day=DAY):
    return {'platform': platform, 'day': day, 'reason': 'fixture', 'policy': distribution.CURRENT_DISTRIBUTION_POLICY}


def job(db, item, platform='douyin', action='publish', status='queued', result=None, at=NOW):
    entry = FactoryPublication(item_id=item.id, platform=platform, action=action,
                               status=status, result=result or {}, created_at=at.replace(tzinfo=None))
    db.add(entry); db.flush()
    return entry


def test_two_platform_kuaishou_quota_accepts_third_item_but_blocks_fourth(db):
    from services.factory_release_plan import THREE_PLATFORM_PLAN

    run, items = run_items(db, ['一', '二', '三', '四'])
    run.settings_snapshot = {**run.settings_snapshot, 'release_plan': THREE_PLATFORM_PLAN,
                             'platforms': ['douyin', 'kuaishou']}
    for item in items:
        item.brief = {'editor_id': 'producer', 'distribution': {
            **route('kuaishou'), 'editor_id': 'producer', 'kuaishou_selected': True,
            'release_plan': THREE_PLATFORM_PLAN}}
    job(db, items[0], platform='kuaishou', status='submitted')
    job(db, items[1], platform='kuaishou', status='submitted')
    assert distribution.guard_publication(db, items[2], 'kuaishou', 'prepare', now=NOW)['day'] == DAY
    job(db, items[2], platform='kuaishou', action='prepare')
    with pytest.raises(ValueError, match='3条作品'):
        distribution.guard_publication(db, items[3], 'kuaishou', 'prepare', now=NOW)












@pytest.mark.parametrize('status,result,expected', [
    ('queued', {}, True), ('running', {}, True), ('submitted', {}, True),
    ('failed', {}, True), ('uncertain', {}, True),
    ('failed', {'submission_attempted': False}, False),
    ('failed', {'submission_attempted': False, 'submission_uncertain': True}, True),
])
def test_publish_unknown_is_reserved_not_zero(status, result, expected):
    assert distribution.reserves_quota(SimpleNamespace(action='publish', status=status, result=result)) is expected


def test_quota_uses_actual_reservation_day_and_deduplicates_prepare_publish(db):
    run, items = run_items(db, ['A', 'B', 'C'])
    old = datetime(2026, 11, 9, 17, tzinfo=timezone.utc)
    job(db, items[0], action='prepare', status='prepared')
    job(db, items[0], result={distribution.RESERVATION_KEY: {'day': DAY}}, at=old)
    job(db, items[1], status='failed')
    job(db, items[2], status='failed', result={'submission_attempted': False})
    assert distribution.publication_usage(db, 'douyin', DAY) == 2
    assert distribution.publication_usage(db, 'douyin', DAY, include_prepares=True) == 2
    assert distribution.publication_usage(db, 'douyin', '2026-11-10') == 0
    assert distribution.quota_day(SimpleNamespace(result={}, created_at=old.replace(tzinfo=None))) == '2026-11-10'


def test_legacy_publish_jobs_reduce_assignment_capacity(db):
    old_run, old_items = run_items(db, ['old'] * 4, policy=False)
    for item in old_items:
        job(db, item, 'douyin', status='submitted')
    run, items = run_items(db, ['新品发布', '新品发布'])
    for item in items: item.brief={**item.brief,'editor_id':'producer'}
    assigned = distribution.assign_distribution(db, run.id, DAY)
    assert all(r['platform'] != 'douyin' for r in assigned.values())


def test_makeup_slots_raise_cap_only_for_named_runs_on_the_authorized_day(db):
    when = NOW.replace(tzinfo=None)
    old_run, old_items = run_items(db, ['原任务'] * 4, policy=False)
    for item in old_items:
        job(db, item, status='submitted')
    run, (item,) = run_items(db, ['补发'])
    run.target_count = 1
    run.settings_snapshot = {**run.settings_snapshot, 'production_day': '2026-11-10'}
    item.brief = {**item.brief, 'editor_id': 'producer'}
    releases = [FactoryRelease(due_at=when + timedelta(minutes=i), platforms=['douyin'],
                               run_id=run.id if i == 0 else None) for i in range(5)]
    db.add_all(releases)
    db.flush()
    order = FactoryWorkOrder(key=f'douyin-makeup:{DAY}', kind='douyin_makeup', owner='director',
                             title='补发', acceptance='仅限指定抖音时段', due_at=when, status='authorized',
                             evidence={'day': DAY, 'release_ids': [slot.id for slot in releases]})
    db.add(order)
    db.flush()
    assigned = distribution.assign_distribution(db, run.id, DAY, proposal={item.id: 'douyin'})
    assert assigned[item.id]['carried_from'] == '2026-11-10'
    assert distribution.guard_publication(db, item, 'douyin', 'prepare', now=NOW)['day'] == DAY
    job(db, item, status='submitted')

    unrelated, (other,) = run_items(db, ['未经授权的作品'])
    other.brief = {**other.brief, 'editor_id': 'producer', 'distribution': {**assigned[item.id]}}
    db.flush()
    with pytest.raises(ValueError, match='4条'):
        distribution.guard_publication(db, other, 'douyin', 'prepare', now=NOW)
    with pytest.raises(ValueError, match='发行日期'):
        distribution.guard_publication(db, item, 'douyin', 'prepare', now=NOW + timedelta(days=1))




@pytest.mark.parametrize('result,expected', [({}, True), ({'uploaded': False}, False),
    ({'uploaded': False, 'submission_uncertain': True}, True)])
def test_failed_prepare_unknown_upload_still_reserves_admission(result, expected):
    item = SimpleNamespace(action='prepare', status='failed', result=result)
    assert distribution.reserves_quota(item, include_prepares=True) is expected
