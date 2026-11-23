from datetime import datetime, timezone, timedelta
import json
import os
from unittest.mock import Mock
import pytest

@pytest.fixture(autouse=True)
def historical_transport_capability(monkeypatch):
    from services import factory_platform_policy
    monkeypatch.setattr(factory_platform_policy, "automation_enabled", lambda platform: True)

from services import factory_transport_health as health


def test_probe_is_bounded_keeps_other_platform_and_never_writes_proxy_credentials(tmp_path,monkeypatch):
    now=datetime(2026,11,16,10,tzinfo=timezone.utc)
    path=tmp_path/'kuaishou'/'state.json'
    other=tmp_path/'state.json';other.write_text(json.dumps({'douyin':{'ready':True}}))
    private=tmp_path/'private.env';private.write_text('CREATOR_BROWSER_PROXY_KUAISHOU=http://fixture-user:fixture-secret@localhost:18792\n')
    monkeypatch.setattr(health,'STATUS_FILE',path);monkeypatch.setattr(health,'PROXY_FILE',private)
    probe=Mock(side_effect=[(False,'TLS unavailable'),(True,'TLS restored')]);monkeypatch.setattr(health,'_probe',probe)
    assert not health.refresh_kuaishou_transport(now)['ready']
    assert not health.refresh_kuaishou_transport(now+timedelta(seconds=299))['ready']
    assert probe.call_count==1
    assert health.refresh_kuaishou_transport(now+timedelta(seconds=300))['ready']
    assert json.loads(other.read_text())=={'douyin':{'ready':True}}
    assert json.loads(path.read_text())['ready'] is True
    assert 'fixture-secret' not in path.read_text() and 'fixture-user' not in path.read_text()
    assert probe.call_count==2


@pytest.mark.parametrize('value', ['direct', 'http://localhost:18791', '${HTTPS_PROXY}'])
def test_no_generic_proxy_or_direct_fallback_is_used(tmp_path,monkeypatch,value):
    from services.creator_connector import browser_proxy
    path=tmp_path/'state.json';path.write_text('{}')
    private=tmp_path/'private.env';private.write_text(f'CREATOR_BROWSER_PROXY_KUAISHOU={value}\n')
    monkeypatch.setattr(health,'STATUS_FILE',path);monkeypatch.setattr(health,'PROXY_FILE',private)
    monkeypatch.setenv('HTTPS_PROXY','http://fixture-overseas-proxy:1234')
    probe=Mock();monkeypatch.setattr(health,'_probe',probe)
    assert not health.refresh_kuaishou_transport()['ready']
    probe.assert_not_called()
    with pytest.raises(ValueError, match='快手独立北京代理'):
        browser_proxy('kuaishou')
    assert browser_proxy('douyin')['server']=='http://fixture-overseas-proxy:1234'


def test_restored_transport_resumes_saved_session_once_without_submitting(monkeypatch):
    from services import creator_connector
    monkeypatch.setattr(health,'_last_session_attempt',float('-inf'))
    monkeypatch.setattr(health.time,'monotonic',lambda:1000)
    monkeypatch.setattr(creator_connector,'_alive',lambda platform:False)
    start=Mock();submit=Mock(side_effect=AssertionError('Session restore cannot publish'))
    monkeypatch.setattr(creator_connector,'_ensure_daemon',start)
    monkeypatch.setattr(creator_connector,'submit_command',submit)
    health.restore_saved_session({'ready':False});start.assert_not_called()
    health.restore_saved_session({'ready':True});health.restore_saved_session({'ready':True})
    start.assert_called_once_with('kuaishou');submit.assert_not_called()


def test_changing_private_proxy_invalidates_the_previous_success(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    path = tmp_path / 'state.json'
    private = tmp_path / 'private.env'
    private.write_text('CREATOR_BROWSER_PROXY_KUAISHOU=http://localhost:18792\n')
    path.write_text(json.dumps({'ready': True, 'checked_at': now.isoformat()}))
    monkeypatch.setattr(health, 'STATUS_FILE', path)
    monkeypatch.setattr(health, 'PROXY_FILE', private)
    assert health.transport_state()['ready']
    private.write_text('CREATOR_BROWSER_PROXY_KUAISHOU=https://beijing.example.test\n')
    changed_at = path.stat().st_mtime + 1
    os.utime(private, (changed_at, changed_at))
    assert not health.transport_state()['ready']
