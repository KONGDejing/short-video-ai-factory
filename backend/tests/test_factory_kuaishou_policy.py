"""Independent, date-bound routes for the restored short-video platforms."""

from collections import Counter
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models.factory import FactoryConfig, FactoryItem, FactoryPublication, FactoryRelease, FactoryRun, FactoryWorkOrder
from services import factory_platform_policy, factory_schedule
from services.factory_distribution import assign_distribution, guard_publication
from services.factory_release_plan import PLATFORM_LIMITS
from services.factory_release_trial import require_live_trial, trial_key
from services.factory_service import config_dict
from services.factory_team import DAILY_EDITOR_ORDER


@pytest.fixture
def db():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        yield session
    engine.dispose()


@pytest.mark.parametrize('with_crosspost', [False, True])
def test_seven_clock_slots_keep_unique_platforms_and_reserve_each_cap(db, with_crosspost):
    start = datetime(2026, 11, 24, tzinfo=ZoneInfo('Asia/Shanghai'))
    config = FactoryConfig(daily_target=7, publish_jitter_seconds=0, auto_publish_xiaohongshu=True,
                           publish_times=['05:00', '05:30', '06:00', '06:30'])
    db.add(config)
    db.commit()
    factory_schedule.plan_slots(db, config, start)
    day_start = factory_schedule.utc(start)
    slots = db.query(FactoryRelease).filter(FactoryRelease.nominal_at < day_start + timedelta(days=1)) \
        .order_by(FactoryRelease.nominal_at).all()
    assert Counter(slot.platforms[0] for slot in slots) == PLATFORM_LIMITS
    scheduled = list(zip(slots, DAILY_EDITOR_ORDER))
    if with_crosspost:
        first_kuaishou = next(pair for pair in scheduled if pair[0].platforms == ['kuaishou'])
        scheduled.remove(first_kuaishou)
        scheduled.insert(0, first_kuaishou)
    for position, (slot, editor) in enumerate(scheduled):
        platform = slot.platforms[0]
        run = FactoryRun(active_slot=None, target_count=1, status='completed',
                         settings_snapshot={**config_dict(config), 'release_slot_ids': [slot.id]})
        db.add(run)
        db.flush()
        run.active_slot = None
        item = FactoryItem(run_id=run.id, position=0, brief={'title': slot.id, 'editor_id': editor},
                           media={'fingerprint': slot.id})
        db.add(item)
        db.flush()
        route = assign_distribution(db, run.id, day=start.date())[item.id]
        assert route['platform'] == platform
        factory_schedule.bind_editor_releases(db, run)
        assert slot.item_id == item.id and slot.platforms == [platform]
        result = guard_publication(db, item, platform, 'prepare', now=start)
        assert result['day'] == start.date().isoformat()
        for other in PLATFORM_LIMITS.keys() - {platform}:
            with pytest.raises(ValueError, match='没有分配'):
                guard_publication(db, item, other, 'prepare', now=start)
        receipt = FactoryPublication(item_id=item.id, platform=platform, action='publish', status='submitted',
                                     created_at=slot.due_at)
        db.add(receipt)
        if with_crosspost and position == 0:
            receipt.result = {'evidence': {'type': 'api_acceptance',
                'url': 'https://cp.kuaishou.com/rest/cp/works/v2/video/pc/submit', 'success': True, 'code': 1}}
            db.flush()
            crosspost_run = FactoryRun(active_slot=None, settings_snapshot=config_dict(config))
            db.add(crosspost_run)
            db.flush()
            crosspost_run.active_slot = None
            target = FactoryItem(run_id=crosspost_run.id, position=0, media=item.media,
                brief={**item.brief, 'origin_item_id': item.id, 'distribution': {
                    **route, 'platform': 'douyin', 'douyin_selected': True, 'kuaishou_selected': False}})
            db.add(target)
            db.flush()
            db.add(FactoryWorkOrder(key=f'owner-crosspost:douyin:{item.id}', kind='owner_crosspost',
                owner='publisher', title='转发试验', acceptance='官方回执', due_at=slot.due_at,
                status='completed', evidence={'day': start.date().isoformat(),
                    'destination_item_id': target.id, 'source_receipt_id': receipt.id}))
            db.add(FactoryPublication(item_id=target.id, platform='douyin', action='publish',
                                      status='submitted', created_at=slot.due_at))
        db.flush()
    assert len({slot.item_id for slot in slots}) == 7


def test_legacy_archive_cannot_be_sent_to_restored_platform(db):
    config = FactoryConfig(daily_target=6)
    db.add(config)
    db.flush()
    run = FactoryRun(settings_snapshot=config_dict(config))
    db.add(run)
    db.flush()
    item = FactoryItem(run_id=run.id, position=0, brief={'editor_id': 'producer'})
    db.add(item)
    db.flush()
    today = datetime.now(timezone.utc)
    assign_distribution(db, run.id, day=today.date())
    with pytest.raises(ValueError):
        guard_publication(db, item, 'kuaishou', 'prepare', now=today)


def test_owner_crosspost_admits_only_the_named_version_after_kuaishou_acceptance(db):
    from services.factory_distribution import CURRENT_DISTRIBUTION_POLICY

    day = '2026-11-24'
    now = datetime(2026, 11, 24, 7, tzinfo=timezone.utc)
    run = FactoryRun(id='crosspost', settings_snapshot={'distribution_policy': CURRENT_DISTRIBUTION_POLICY})
    db.add(run)
    db.flush()
    original = FactoryItem(id='original', run_id=run.id, position=0, media={'fingerprint': 'approved'})
    route = {'policy': CURRENT_DISTRIBUTION_POLICY, 'day': day, 'platform': 'douyin',
             'douyin_selected': True, 'editor_id': 'producer'}
    target = FactoryItem(id='target', run_id=run.id, position=1, media={'fingerprint': 'approved'},
                         brief={'origin_item_id': original.id, 'distribution': route})
    db.add_all([original, target])
    db.flush()
    receipt = FactoryPublication(item_id=original.id, platform='kuaishou', action='publish', status='submitted',
        result={'evidence': {'type': 'api_acceptance', 'url': 'https://cp.kuaishou.com/rest/cp/works/v2/video/pc/submit',
                            'success': True, 'code': 1}})
    db.add(receipt)
    db.flush()
    for index in range(4):
        db.add(FactoryPublication(item_id=f'other-{index}', platform='douyin', action='publish',
                                  status='submitted', created_at=now.replace(tzinfo=None)))
    db.flush()
    with pytest.raises(ValueError, match='禁止重复提交'):
        guard_publication(db, target, 'douyin', 'prepare', now=now)
    db.add(FactoryWorkOrder(key=f'owner-crosspost:douyin:{original.id}', kind='owner_crosspost',
                            owner='publisher', title='转发试验', acceptance='官方回执', due_at=now,
                            status='authorized', evidence={'day': day, 'destination_item_id': target.id,
                                                           'source_receipt_id': receipt.id}))
    db.flush()
    assert guard_publication(db, target, 'douyin', 'prepare', now=now)['day'] == day
    target.media = {'fingerprint': 'different'}
    db.flush()
    with pytest.raises(ValueError, match='禁止重复提交'):
        guard_publication(db, target, 'douyin', 'prepare', now=now)


def test_failed_kuaishou_network_keeps_slot_for_its_own_platform(db, monkeypatch):
    start = datetime(2026, 11, 24, 0)
    slot = FactoryRelease(due_at=start, platforms=['kuaishou'])
    item = SimpleNamespace(id='planned-original')
    monkeypatch.setattr(factory_platform_policy, 'publication_transport_state',
                        lambda platform: {'ready': False, 'reason': '专用线路连接失败'})
    begin = Mock()
    monkeypatch.setattr(factory_schedule.publishing, 'begin_publication', begin)
    assert factory_schedule.step_platform(db, slot, item, 'kuaishou', start, True) == {
        'status': 'blocked', 'message': '专用线路连接失败'}
    assert slot.platforms == ['kuaishou']
    begin.assert_not_called()


def test_scheduled_publication_waits_for_the_actual_trial_receipt(db):
    run = FactoryRun(active_slot=None)
    db.add(run)
    db.flush()
    item = FactoryItem(run_id=run.id, position=0)
    db.add(item)
    db.flush()
    now = datetime(2026, 11, 23, 8)
    slot = FactoryRelease(due_at=now, item_id=item.id, platforms=['xiaohongshu'])
    db.add(slot)
    db.flush()
    order = FactoryWorkOrder(key=trial_key('xiaohongshu'), kind='platform_release_trial',
                             owner='publisher', title='发布验证', acceptance='官方回执',
                             due_at=now, status='validating', evidence={'release_id': slot.id})
    db.add(order)
    db.flush()
    require_live_trial(db, 'xiaohongshu', item.id)
    with pytest.raises(ValueError, match='等待本次试发'):
        require_live_trial(db, 'xiaohongshu', 'another-original')
    receipt = FactoryPublication(item_id=item.id, platform='xiaohongshu', action='publish',
                                 status='submitted', result={})
    db.add(receipt)
    db.flush()
    with pytest.raises(ValueError, match='等待本次试发'):
        require_live_trial(db, 'xiaohongshu', 'another-original')
    receipt.result = {'evidence': {'type': 'api_receipt', 'note_id': 'official-fixture'}}
    db.flush()
    require_live_trial(db, 'xiaohongshu', 'another-original')
    assert order.status == 'completed' and order.evidence['receipt_job_id'] == receipt.id
