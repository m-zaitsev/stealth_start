"""Tests for the companion against a fake OBS. Run: python -m pytest test_companion.py"""
import json
import socket
import threading
import time

import obsws_python as obs
import pytest

import stealth_start_companion as qc


class FakeObs:
    """Minimal in-memory OBS: one scene with a game capture, optional freeze plugin."""

    def __init__(self, plugin=True):
        self.plugin = plugin
        self.filters = {}            # (source, filter) -> enabled
        self.mute = {'Game Capture': False}
        self.items = {'Game Capture': {'id': 1, 'index': 0, 'enabled': True}}
        self.calls = []

    def send(self, rt, data=None, raw=False):
        self.calls.append(rt)
        d = data or {}
        fail = lambda: (_ for _ in ()).throw(obs.error.OBSSDKRequestError(rt, 600, 'nope'))
        if rt == 'GetSourceFilter':
            return {} if (d['sourceName'], d['filterName']) in self.filters else fail()
        if rt == 'CreateSourceFilter':
            if not self.plugin:
                fail()
            self.filters[(d['sourceName'], d['filterName'])] = True
            return {}
        if rt == 'SetSourceFilterEnabled':
            self.filters[(d['sourceName'], d['filterName'])] = d['filterEnabled']
            return {}
        if rt == 'GetInputMute':
            return {'inputMuted': self.mute[d['inputName']]}
        if rt == 'SetInputMute':
            self.mute[d['inputName']] = d['inputMuted']
            return {}
        if rt == 'GetCurrentProgramScene':
            return {'currentProgramSceneName': 'Main'}
        if rt == 'GetSceneItemId':
            return {'sceneItemId': self.items[d['sourceName']]['id']} if d['sourceName'] in self.items else fail()
        if rt == 'CreateInput':
            self.items[d['inputName']] = {'id': 2, 'index': len(self.items), 'enabled': d['sceneItemEnabled']}
            return {'sceneItemId': 2}
        if rt == 'SetSceneItemTransform':   # real OBS rejects bounds below 1
            tr = d['sceneItemTransform']
            if tr.get('boundsWidth', 1) < 1 or tr.get('boundsHeight', 1) < 1:
                fail()
            return {}
        if rt in ('SaveSourceScreenshot', 'SetInputSettings'):
            return {}
        if rt == 'GetSceneItemTransform':
            return {'sceneItemTransform': {'positionX': 0, 'scaleX': 1, 'width': 1920, 'sourceWidth': 1920,
                                           'boundsType': 'OBS_BOUNDS_NONE', 'boundsWidth': 0, 'boundsHeight': 0}}
        by_id = {v['id']: v for v in self.items.values()}
        if rt == 'GetSceneItemIndex':
            return {'sceneItemIndex': by_id[d['sceneItemId']]['index']}
        if rt == 'SetSceneItemIndex':   # OBS semantics: move item, others shift
            order = sorted(self.items.values(), key=lambda v: v['index'])
            item = by_id[d['sceneItemId']]
            order.remove(item)
            order.insert(d['sceneItemIndex'], item)
            for i, v in enumerate(order):
                v['index'] = i
            return {}
        if rt == 'SetSceneItemEnabled':
            by_id[d['sceneItemId']]['enabled'] = d['sceneItemEnabled']
            return {}
        raise AssertionError('unexpected request ' + rt)


def make(plugin=True, **cfg_over):
    cfg = dict(qc.DEFAULT_CONFIG, listen_port=0, **cfg_over)
    ctl = qc.ObsController(cfg, '/tmp')
    ctl.client = FakeObs(plugin)
    return cfg, ctl


def test_filter_freeze_and_audio_restore():
    cfg, ctl = make(plugin=True)
    ctl.client.mute['Game Capture'] = False
    ctl.cover_up()
    assert ctl.method == 'filter'
    assert ctl.client.filters[('Game Capture', 'Stealth Start Freeze')] is True
    assert ctl.client.mute['Game Capture'] is True
    ctl.cover_down()
    assert ctl.client.filters[('Game Capture', 'Stealth Start Freeze')] is False
    assert ctl.client.mute['Game Capture'] is False


def test_audio_stays_muted_if_it_was_muted_before():
    cfg, ctl = make()
    ctl.client.mute['Game Capture'] = True
    ctl.cover_up()
    ctl.cover_down()
    assert ctl.client.mute['Game Capture'] is True


def test_screenshot_fallback_puts_image_above_game():
    cfg, ctl = make(plugin=False)
    ctl.cover_up()
    assert ctl.method == 'screenshot'
    img = ctl.client.items['Stealth Start Freeze Image']
    game = ctl.client.items['Game Capture']
    assert img['enabled'] and img['index'] > game['index']
    assert 'SaveSourceScreenshot' in ctl.client.calls
    ctl.cover_down()
    assert img['enabled'] is False


def test_screenshot_moves_existing_image_above_game():
    cfg, ctl = make(plugin=False)
    # user-created overlay that sits below the game capture
    ctl.client.items['Game Capture']['index'] = 1
    ctl.client.items['Stealth Start Freeze Image'] = {'id': 2, 'index': 0, 'enabled': False}
    ctl.cover_up()
    assert ctl.client.items['Stealth Start Freeze Image']['index'] > ctl.client.items['Game Capture']['index']


def test_filter_only_mode_fails_without_plugin():
    cfg, ctl = make(plugin=False, freeze_method='filter')
    with pytest.raises(qc.ObsError):
        ctl.cover_up()
    assert ctl.cover_on is False


def test_audio_disabled():
    cfg, ctl = make(game_audio_source='')
    ctl.cover_up()
    assert 'SetInputMute' not in ctl.client.calls


# ---------- UDP protocol + watchdog ----------
def run_companion(ctl, cfg):
    comp = qc.Companion(cfg, ctl)
    ctl.connect = lambda: None     # keep the fake client
    port = comp.sock.getsockname()[1]
    t = threading.Thread(target=comp.run, daemon=True)
    t.start()
    return comp, port


def rpc(port, kind, msg_id):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2)
    s.sendto(json.dumps({'v': 1, 'type': kind, 'id': msg_id}).encode(), ('127.0.0.1', port))
    resp = json.loads(s.recvfrom(4096)[0])
    s.close()
    return resp


def test_udp_ack_and_off():
    cfg, ctl = make()
    comp, port = run_companion(ctl, cfg)
    r = rpc(port, 'COVER_ON', 7)
    assert r == {'v': 1, 'type': 'ACK', 'id': 7, 'ok': True}
    assert ctl.cover_on
    assert rpc(port, 'PING', 8)['ok'] is True
    rpc(port, 'COVER_OFF', 9)
    assert not ctl.cover_on


def test_udp_nack_when_obs_fails():
    cfg, ctl = make(plugin=False, freeze_method='filter')
    comp, port = run_companion(ctl, cfg)
    r = rpc(port, 'COVER_ON', 1)
    assert r['ok'] is False and 'Freeze Filter' in r['error']


def test_watchdog_drops_cover_without_heartbeat():
    cfg, ctl = make(heartbeat_timeout_seconds=1)
    comp, port = run_companion(ctl, cfg)
    rpc(port, 'COVER_ON', 1)
    assert ctl.cover_on
    time.sleep(2.2)
    assert not ctl.cover_on
    assert ctl.client.mute['Game Capture'] is False


def test_watchdog_max_duration():
    cfg, ctl = make(max_cover_seconds=1)
    comp, port = run_companion(ctl, cfg)
    rpc(port, 'COVER_ON', 1)
    for i in range(4):
        time.sleep(0.5)
        rpc(port, 'PING', 2 + i)
    assert not ctl.cover_on


def test_selftest_freezes_then_restores():
    cfg, ctl = make()
    ctl.connect = lambda: None
    assert qc.selftest(ctl, 0.1) == 0
    assert ctl.client.filters[('Game Capture', 'Stealth Start Freeze')] is False
    assert ctl.client.mute['Game Capture'] is False


def test_selftest_reports_failure():
    cfg, ctl = make(plugin=False, freeze_method='filter')
    ctl.connect = lambda: None
    assert qc.selftest(ctl, 0.1) == 1
