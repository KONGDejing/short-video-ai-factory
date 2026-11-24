from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models.factory import FactoryConfig, FactoryItem, FactoryPublication, FactoryRun, FactoryWorkMetricsSnapshot
from services import factory_service, factory_work_metrics as metrics, factory_work_metrics_sources as sources


NOW = datetime(2026, 11, 24, 8, tzinfo=timezone.utc)


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "test.db"}')
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def test_kuaishou_collects_only_unique_accepted_works_and_respects_interval(sessions, monkeypatch):
    published = NOW - timedelta(hours=23)
    auth = {'app_id': 'test-app', 'access_token': 'test-token', 'access_expires_at': '2099-01-01T00:00:00+00:00'}
    calls = []

    def answer(request):
        calls.append(request.url.path)
        assert request.url.host == 'open.kuaishou.com'
        assert request.url.params['access_token'] == auth['access_token']
        return httpx.Response(200, json={'result': 1, 'video_list': [
            {'photo_id': 'correct', 'caption': '公司任命新CEO #科技', 'create_time': int(published.timestamp() * 1000),
             'pending': False, 'view_count': 300, 'like_count': 0},
            {'photo_id': 'still-pending', 'caption': '公司任命新CEO', 'create_time': int(published.timestamp() * 1000),
             'pending': True, 'view_count': 0},
            {'photo_id': 'unknown', 'caption': '未知视频', 'create_time': int(published.timestamp() * 1000),
             'pending': False, 'view_count': 5},
        ]})

    monkeypatch.setattr(sources, 'credentials', lambda platform: auth if platform == 'kuaishou' else None)
    with httpx.Client(transport=httpx.MockTransport(answer)) as client, sessions() as db:
        monkeypatch.setattr(sources, 'capture', lambda platform, credentials: sources._kuaishou(client, credentials))
        run = FactoryRun()
        db.add(run)
        db.flush()
        item = FactoryItem(run_id=run.id, position=0, content={'title': '公司任命新CEO'})
        db.add(item)
        db.flush()
        db.add(FactoryPublication(item_id=item.id, platform='kuaishou', action='publish', status='submitted',
            created_at=published.replace(tzinfo=None), result={'evidence': {'type': 'api_acceptance', 'success': True,
                'code': 1, 'url': 'https://cp.kuaishou.com/rest/cp/works/v2/video/pc/submit'}}))
        db.commit()
        metrics.collect_due(db, NOW)
        metrics.collect_due(db, NOW + timedelta(minutes=10))
        assert calls == ['/openapi/photo/list']
        snapshot = db.query(FactoryWorkMetricsSnapshot).one()
        assert snapshot.status == 'connected'
        [work] = snapshot.data['works']
        assert (work['item_id'], work['views'], work['likes']) == (item.id, 300, 0)


def test_douyin_uses_authorized_listing_to_map_canonical_video_ids():
    auth = {'app_id': 'test-app', 'app_secret': 'test-secret', 'open_id': 'test-open', 'account_id': 'account',
            'access_token': 'test-access', 'access_expires_at': '2099-01-01T00:00:00+00:00'}
    replies = [
        {'data': {'error_code': 0, 'access_token': 'test-client'}},
        {'err_no': 0, 'data': {'user_publish_item_list': [{'video_link': 'https://www.douyin.com/video/123'}]}},
        {'err_no': 0, 'data': {'convert_result': {'123': '@test-item'}}},
        {'err_no': 0, 'data': {'extra': {'error_code': 0}, 'data': {'list': [
            {'item_id': '@test-item', 'title': '新模型发布', 'create_time': int(NOW.timestamp()),
             'statistics': {'play_count': 400, 'digg_count': 2}}]}}},
    ]
    paths = []

    def answer(request):
        paths.append(request.url.path)
        return httpx.Response(200, json=replies.pop(0))

    with httpx.Client(transport=httpx.MockTransport(answer)) as client:
        [work] = sources._douyin(client, auth)
    assert work['platform_work_id'] == '123' and work['views'] == 400
    assert paths == ['/oauth/stable_client_token/', '/aweme/v1/video/self/get_user_video_list/',
                     '/api/apps/v1/convert_video_id/video_id_to_open_item_id/', '/api/apps/v1/video/query/']


def test_metrics_require_same_platform_similar_age_and_distinct_works(sessions):
    with sessions() as db:
        published = (NOW - timedelta(hours=25)).isoformat()
        works = [{'item_id': str(index), 'published_at': published, 'views': views,
                  'topic': '重大人事与公司决策' if index < 3 else '其他科技'}
                 for index, views in enumerate([100, 150, 900, 10, 20, 30])]
        db.add(FactoryWorkMetricsSnapshot(platform='douyin', status='connected', data={'works': works[:1] + works[3:]},
                                          collected_at=(NOW - timedelta(hours=2)).replace(tzinfo=None)))
        db.add(FactoryWorkMetricsSnapshot(platform='kuaishou', status='connected', data={'works': works},
                                          collected_at=(NOW - timedelta(hours=1)).replace(tzinfo=None)))
        db.commit()
        assert metrics.recommend_topics(db, 'douyin', NOW) == []
        db.add(FactoryWorkMetricsSnapshot(platform='douyin', status='connected', data={'works': works},
                                          collected_at=(NOW - timedelta(hours=1)).replace(tzinfo=None)))
        db.commit()
        [preference] = metrics.recommend_topics(db, 'douyin', NOW)
        assert preference == {'topic': '重大人事与公司决策', 'sample_count': 3, 'comparison_count': 3,
                              'median_views': 150, 'comparison_median_views': 20}


def test_clock_plan_prioritizes_verified_today_and_uses_platform_observations(sessions, monkeypatch):
    day = NOW.date().isoformat()
    titles = ('机器人训练教程', '大模型发布', '芯片量产发布')
    records = [{'id': str(index), 'kind': 'news', 'title': title, 'published_at': day,
                'url': f'https://example.invalid/source-{index}', 'text': '官方正文'}
               for index, title in enumerate(titles)]
    candidates = [{'title': title, 'editor_track': 'technology_scenes', 'freshness': 'news', 'source_ids': [str(index)]}
                  for index, title in enumerate(titles)]
    with sessions() as db:
        db.add(FactoryConfig(id=1))
        run = FactoryRun(target_count=1, trigger='release_clock', sources=records, plan={'candidates': candidates},
                         settings_snapshot={'production_day': day, 'news_first': True,
                                            'editor_quotas': {'producer': 1, 'producer2': 0, 'producer3': 0}})
        db.add(run)
        db.commit()
        run_id = run.id

    monkeypatch.setattr(factory_service, 'SessionLocal', sessions)
    monkeypatch.setattr(metrics, 'recommend_topics', lambda db, platform: [{'topic': '芯片与智能硬件'}])

    def director(run_id, agent, payload):
        assert agent == 'director'
        assert payload['candidates'] == [candidates[2]]
        return {'selected': [{'candidate_index': 0}]}

    monkeypatch.setattr(factory_service, 'call_agent', director)
    factory_service._make_plan(run_id)
    with sessions() as db:
        assert db.query(FactoryItem).one().brief['title'] == titles[2]
        assert db.get(FactoryRun, run_id).plan['decision']['selected'][0]['candidate_index'] == 2
