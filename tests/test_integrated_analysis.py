"""Offline integration: validated stream, staff-only wording and degraded coverage."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from analysis_scaffold.analyzer import Flag
from analysis_scaffold.deterministic_analyzer import MESSAGES
from analysis_scaffold.llm_analyzer import FakeLLMClient, LLMAnalyzer
from body_heat_demo import server, views
from body_heat_demo.engine import build_demo
from f1_sideline_demo.hub import Rejected
from test_demo_prototype import packet


class Spy:
    def __init__(self):
        self.calls = []

    def ingest(self, pkt, t):
        self.calls.append((pkt, t))
        return [Flag(t, pkt['device_id'], 'trend', MESSAGES['trend'])]


def test_only_accepted_packets_reach_analyzer():
    _, hub, _, roster, _ = build_demo(seed_history=False)
    spy = hub.analyzer = Spy()
    for bad in [{}, packet(dev='pod-99'), packet(tag='bad'), packet(dev='pod-33')]:
        with pytest.raises(Rejected):
            hub.accept(bad, 7)
    hub.accept(packet(), 8)
    with pytest.raises(Rejected):
        hub.accept(packet(), 9)
    assert spy.calls == [(packet(), 8)]
    tile = views.build_tile(hub, roster[0], 8)
    assert tile['hub_analysis']['flag'] == {'t': 8, 'kind': 'trend', 'message': MESSAGES['trend']}
    assert 'hub_analysis' not in views.build_tile(hub, roster[0], 8, staff=False)


@pytest.mark.parametrize('name', ['deterministic', 'llm-explain', 'llm-rolling'])
def test_script_flags_exact_wording_and_fake_without_key(name, monkeypatch):
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    _, hub, _, roster, engine = build_demo(seed_history=False, analyzer=name)
    if name.startswith('llm'):
        assert isinstance(hub.analyzer.client, FakeLLMClient)
    member = next(m for m in roster if m['device_id'] == 'pod-11')
    seen = {}
    for _ in range(200):
        engine.step()
        flag = views.build_tile(hub, member, engine.t)['hub_analysis']['flag']
        if flag:
            seen[flag['kind']] = flag['message']
    assert seen == MESSAGES


@pytest.mark.parametrize('scope', ['wearer', 'session'])
def test_budget_visibly_surfaces(scope):
    _, hub, _, roster, engine = build_demo(seed_history=False)
    hub.analyzer = LLMAnalyzer(variant='rolling', **{f'max_calls_{"per_wearer" if scope == "wearer" else "session"}': 0})
    engine.step()
    for m in roster[:-1]:
        analysis = views.build_tile(hub, m, engine.t)['hub_analysis']
        assert analysis['flag'] is None
        assert analysis['status'] == 'Analysis unavailable'
        notice = analysis['notice']
        assert notice['kind'] == 'budget_exhausted'
        assert notice['message'] == 'Model analysis unavailable: call budget exhausted.'


def test_unavailable_never_looks_like_ok():
    class Failing(FakeLLMClient):
        def complete(self, window):
            raise RuntimeError('private provider detail')
    _, hub, _, roster, engine = build_demo(seed_history=False)
    hub.analyzer = LLMAnalyzer(Failing(), variant='rolling')
    engine.step()
    a = views.build_tile(hub, roster[0], engine.t)['hub_analysis']
    assert a['notice'] == {'t': 0, 'kind': 'analysis unavailable', 'message': 'analysis unavailable'}
    assert a['flag'] is None
    assert a['status'] == 'Analysis unavailable'


def test_relay_prior_payload():
    _, hub, _, roster, engine = build_demo(seed_history=False, analyzer='relay')
    engine.step()
    before = views.build_roster(hub, roster, engine.t)
    hub.analyzer = None
    assert before == views.build_roster(hub, roster, engine.t)
    assert all('hub_analysis' not in tile for tile in before)


def test_athlete_json_and_sse_have_no_analysis():
    store, hub, accounts, roster, engine = build_demo(seed_history=False)
    engine.step()
    server.STATE.update(store=store, hub=hub, accounts=accounts, roster=roster, engine=engine)
    srv = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    token = accounts.sign_in('athlete11', 'demo')
    base = f'http://127.0.0.1:{srv.server_port}'
    try:
        for path in ['/api/wearer/pod-11', '/api/events']:
            req = urllib.request.Request(base+path, headers={'Cookie': f'bh_session={token}'})
            with urllib.request.urlopen(req, timeout=3) as response:
                raw = response.readline() if path.endswith('events') else response.read()
                assert b'hub_analysis' not in raw
                assert b'analysis unavailable' not in raw
        req = urllib.request.Request(base+'/api/roster', headers={'Cookie': f'bh_session={token}'})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_analysis_status_wording_and_flag_precedence():
    _, hub, _, roster, _ = build_demo(seed_history=False)
    member = roster[0]
    dev = member['device_id']
    assert views.build_tile(hub, member, 0)['hub_analysis']['status'] == 'Awaiting analysis output'
    hub.analysis_notices[dev] = {'t': 0, 'kind': 'analysis unavailable', 'message': 'analysis unavailable'}
    hub.analysis_flags[dev] = {'t': 0, 'kind': 'trend', 'message': MESSAGES['trend']}
    assert views.build_tile(hub, member, 0)['hub_analysis']['status'] == 'Latest analysis flag'


def test_pod_guidance_exact_wording_served(monkeypatch):
    store, hub, accounts, roster, engine = build_demo(seed_history=False)
    monkeypatch.setattr(server, 'STATE', dict(store=store, hub=hub, accounts=accounts, roster=roster, engine=engine))
    srv = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{srv.server_port}/api/strings', timeout=3) as response:
            strings = json.load(response)['strings']
        for guidance in [
            'Readings are above the team-set limit; staff review requested.',
            'Readings show a trend above the team-set limit; staff review requested.',
            'Your readings are above the team-set limit. Your staff can see this too.',
            'Your readings are rising faster than the team-set limit. Your staff can see this too.',
        ]:
            assert strings.count(guidance) == 1
    finally:
        srv.shutdown()
        srv.server_close()
