from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, null
from sqlalchemy.orm import sessionmaker

from database import Base
from models.factory import FactoryConfig, FactoryItem, FactoryPublication, FactoryRelease, FactoryRun, FactoryWorkOrder
from services import factory_breaking_news as hot, factory_schedule as schedule, factory_service
from services.factory_distribution import CURRENT_DISTRIBUTION_POLICY, assign_distribution, guard_publication
from tasks.factory_tasks import produce_run


NOW = datetime(2026, 11, 24, 15, tzinfo=ZoneInfo('Asia/Shanghai'))


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "hot.db"}')
    Base.metadata.create_all(engine)
    monkeypatch.setattr(factory_service, 'queue_available', lambda: True)
    monkeypatch.setattr(factory_service, 'ensure_memory_available', lambda: None)
    monkeypatch.setattr(produce_run, 'delay', Mock(return_value=SimpleNamespace(id='queued')))
    with sessionmaker(bind=engine)() as session:
        session.add(FactoryConfig(id=1, daily_target=7, production_on_release_clock=True, publish_enabled=True,
            publish_times=['05:00', '05:30', '06:00', '06:30'], publish_jitter_seconds=0))
        session.commit()
        yield session
    engine.dispose()


def source(now, *, identifier='hot-source-1234', title='NVIDIA Board Reinstates Jensen Huang as CEO'):
    return {'id': identifier, 'kind': 'news', 'url': 'https://nvidianews.nvidia.com/news/' + identifier,
            'title': title, 'text': 'Verified announcement text. ' * 10,
            'published_at': (now - timedelta(minutes=20)).isoformat()}


def test_hourly_scan_records_only_fresh_confirmed_tech_events(db):
    fresh = source(NOW)
    duplicate = source(NOW, identifier='other-source-1234')
    stale = source(NOW - timedelta(days=1), identifier='stale-source-1234')
    ordinary = source(NOW, identifier='normal-source-1234', title='A guide to AI terminology')
    summary = hot.record_scan(db, NOW, {'sources': [fresh, duplicate, stale, ordinary]})
    assert summary['new_events'] == 1
    again = hot.record_scan(db, NOW + hot.INTERVAL, {'sources': [duplicate, fresh]})
    assert again['new_events'] == 0
    assert db.query(FactoryWorkOrder).filter_by(kind=hot.KIND).one().owner == 'producer3'
    assert db.get(FactoryWorkOrder, hot.WATCH_KEY).due_at == hot.utc(NOW + 2 * hot.INTERVAL)


def test_watch_does_not_fetch_again_until_next_hour(db, monkeypatch):
    import database
    from services import factory_news_intake, factory_research, factory_resources

    monkeypatch.setattr(database, 'SessionLocal', sessionmaker(bind=db.get_bind()))
    monkeypatch.setattr(factory_resources, 'memory_state', lambda: {'blocked': False})
    monkeypatch.setattr(factory_research, '_load_feed', lambda *_: ([], None))
    collect = Mock(return_value={'sources': [source(NOW)], 'warnings': []})
    monkeypatch.setattr(factory_news_intake, 'collect_company_news', collect)
    assert hot.scan_if_due(NOW)['new_events'] == 1
    assert hot.scan_if_due(NOW + timedelta(minutes=59)) is None
    assert hot.scan_if_due(NOW + timedelta(hours=1))['new_events'] == 0
    assert collect.call_count == 2


def test_rush_advances_matching_regular_slot_without_adding_a_daily_original(db):
    morning = NOW.replace(hour=4, minute=30)
    config = db.get(FactoryConfig, 1)
    schedule.plan_slots(db, config, morning)
    nominal = hot.utc(morning.replace(hour=5, minute=30))
    slot = db.query(FactoryRelease).filter_by(nominal_at=nominal).one()
    count = db.query(FactoryRelease).count()
    hot.record_scan(db, morning, {'sources': [source(morning)]})
    run = db.get(FactoryRun, hot.dispatch(db, config, morning)['run_id'])
    assert db.query(FactoryRelease).count() == count
    assert slot.run_id == run.id and slot.due_at == hot.utc(morning)
    assert slot.nominal_at == nominal
    assert run.settings_snapshot['production_kind'] == 'daily'
    assert hot.registered_order(db, run).evidence['extra'] is False
    assert hot.dispatch(db, config, morning) is None
    assert produce_run.delay.call_count == 1


def test_hotspot_adds_slot_after_daily_caps_without_granting_other_items_permission(db):
    config = db.get(FactoryConfig, 1)
    day = NOW.date().isoformat()
    regular = FactoryRun(active_slot=null(), status='completed', target_count=7,
        settings_snapshot={'distribution_policy': CURRENT_DISTRIBUTION_POLICY, 'production_kind': 'daily', 'production_day': day})
    db.add(regular)
    db.flush()
    for position in range(4):
        item = FactoryItem(run_id=regular.id, position=position)
        db.add(item)
        db.flush()
        db.add(FactoryPublication(item_id=item.id, platform='douyin', action='publish', status='submitted',
                                  created_at=hot.utc(NOW)))
    db.commit()

    story = source(NOW)
    hot.record_scan(db, NOW, {'sources': [story]})
    run = db.get(FactoryRun, hot.dispatch(db, config, NOW)['run_id'])
    assert run.settings_snapshot['production_kind'] == 'breaking_news'
    order = hot.registered_order(db, run)
    assert order.evidence['extra'] is True
    slot = db.get(FactoryRelease, order.evidence['release_id'])
    assert slot.due_at == hot.utc(NOW)
    item = FactoryItem(run_id=run.id, position=0, brief={'editor_id': 'producer3', 'source_ids': [story['id']]}, content={})
    db.add(item)
    db.flush()
    assert assign_distribution(db, run.id, day)[item.id]['platform'] == 'douyin'
    schedule.bind_editor_releases(db, run)
    with pytest.raises(ValueError, match='缺少指定'):
        guard_publication(db, item, 'douyin', 'prepare', now=NOW)
    item.content = {'source_ids': [story['id']]}
    db.flush()
    assert guard_publication(db, item, 'douyin', 'publish', now=NOW)['day'] == day
    with pytest.raises(ValueError, match='超过时效'):
        guard_publication(db, item, 'douyin', 'publish', now=NOW + hot.FRESHNESS)
    slot.status = 'submitted'
    db.commit()
    assert hot.dispatch(db, config, NOW + timedelta(minutes=25)) is None
    assert order.status == 'completed'
