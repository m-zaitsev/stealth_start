"""Stealth Start OBS companion.

Listens on localhost UDP for messages from the Stealth Start WoT mod and drives OBS
through obs-websocket v5 (built into OBS 28+):

  COVER_ON  -> freeze the game capture + mute the game audio source, reply ACK
  PING      -> keep-alive while the cover is up
  COVER_OFF -> unfreeze + restore the audio mute state

Safety net: the cover is dropped automatically if the mod stops sending PINGs
(game crashed / disconnected) or after a maximum duration.
"""
import argparse
import json
import logging
import os
import socket
import sys
import tempfile
import time

import obsws_python as obs

APP = 'Stealth Start companion'
VERSION = '0.1.0'

DEFAULT_CONFIG = {
    'obs_host': 'localhost',
    'obs_port': 4455,
    'obs_password': '',
    'listen_host': '127.0.0.1',
    'listen_port': 47811,
    # OBS source that captures World of Tanks (Game Capture / Window Capture).
    'game_capture_source': 'Game Capture',
    # OBS source carrying the game's audio. Often the same source as the game
    # capture ("Capture audio" option); set to "" to disable OBS-side muting.
    'game_audio_source': 'Game Capture',
    # "auto": Freeze Filter plugin if available, else screenshot overlay.
    # "filter": Freeze Filter only. "screenshot": screenshot overlay only.
    'freeze_method': 'auto',
    'freeze_filter_name': 'Stealth Start Freeze',
    'freeze_image_source': 'Stealth Start Freeze Image',
    'heartbeat_timeout_seconds': 15,
    'max_cover_seconds': 600,
}

FREEZE_FILTER_KIND = 'freeze_filter'   # Exeldro's Freeze Filter plugin
TRANSFORM_READONLY = ('sourceWidth', 'sourceHeight', 'width', 'height')

log = logging.getLogger('stealthstart')


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in DEFAULT_CONFIG})
    else:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        log.info('Wrote default config to %s - edit it if your OBS source names differ.', path)
    return cfg


class ObsError(Exception):
    pass


class ObsController:
    def __init__(self, cfg, screenshot_dir):
        self.cfg = cfg
        # BMP: OBS writes a 1440p frame in ~30 ms vs ~1.8 s for PNG (measured), and it's lossless.
        # ~10 MB per frame, so keep it out of the project folder.
        self.screenshot_path = os.path.join(tempfile.gettempdir(), 'stealth_start_freeze.bmp')
        self.client = None
        self.method = None            # resolved freeze method
        self.cover_on = False
        self.audio_was_muted = None   # None = we did not touch audio
        self.image_item = None        # (scene, itemId) of the screenshot overlay

    # ----- connection -----
    def req(self, request_type, data=None):
        if self.client is None:
            self.connect()
        try:
            return self.client.send(request_type, data, raw=True) or {}
        except obs.error.OBSSDKRequestError as e:
            raise ObsError('%s failed: %s' % (request_type, e))
        except Exception as e:   # socket dropped etc.
            self.client = None
            raise ObsError('OBS connection lost (%s)' % e)

    def connect(self):
        try:
            self.client = obs.ReqClient(host=self.cfg['obs_host'], port=self.cfg['obs_port'],
                                        password=self.cfg['obs_password'], timeout=3)
        except Exception as e:
            self.client = None
            raise ObsError('cannot connect to OBS at %s:%s (%s)' % (
                self.cfg['obs_host'], self.cfg['obs_port'], e))
        log.info('Connected to OBS.')
        self.method = None

    # ----- freeze method -----
    def resolve_method(self):
        if self.method:
            return self.method
        wanted = self.cfg['freeze_method']
        src, flt = self.cfg['game_capture_source'], self.cfg['freeze_filter_name']
        if wanted in ('auto', 'filter'):
            try:
                self.req('GetSourceFilter', {'sourceName': src, 'filterName': flt})
                self.method = 'filter'
            except ObsError:
                try:
                    self.req('CreateSourceFilter', {'sourceName': src, 'filterName': flt,
                                                    'filterKind': FREEZE_FILTER_KIND,
                                                    # 0 = hold the frame until disabled, never refresh it
                                                    'filterSettings': {'duration': 0, 'refresh_interval': 0}})
                    self.req('SetSourceFilterEnabled', {'sourceName': src, 'filterName': flt,
                                                        'filterEnabled': False})
                    log.info('Added Freeze Filter "%s" to "%s".', flt, src)
                    self.method = 'filter'
                except ObsError as e:
                    if wanted == 'filter':
                        raise ObsError('Freeze Filter plugin not available (%s)' % e)
                    log.info('Freeze Filter plugin not available, using screenshot overlay.')
                    self.method = 'screenshot'
        else:
            self.method = 'screenshot'
        return self.method

    def freeze(self):
        if self.resolve_method() == 'filter':
            self.req('SetSourceFilterEnabled', {'sourceName': self.cfg['game_capture_source'],
                                                'filterName': self.cfg['freeze_filter_name'],
                                                'filterEnabled': True})
        else:
            self.freeze_screenshot()

    def unfreeze(self):
        if self.method == 'filter':
            self.req('SetSourceFilterEnabled', {'sourceName': self.cfg['game_capture_source'],
                                                'filterName': self.cfg['freeze_filter_name'],
                                                'filterEnabled': False})
        elif self.image_item:
            scene, item_id = self.image_item
            self.req('SetSceneItemEnabled', {'sceneName': scene, 'sceneItemId': item_id,
                                             'sceneItemEnabled': False})
            self.image_item = None

    def freeze_screenshot(self):
        src, img = self.cfg['game_capture_source'], self.cfg['freeze_image_source']
        scene = self.req('GetCurrentProgramScene')['currentProgramSceneName']
        try:
            game_id = self.req('GetSceneItemId', {'sceneName': scene, 'sourceName': src})['sceneItemId']
        except ObsError:
            raise ObsError('"%s" is not in the current scene "%s"' % (src, scene))
        try:
            self.req('SaveSourceScreenshot', {'sourceName': src, 'imageFormat': 'bmp',
                                              'imageFilePath': self.screenshot_path})
        except ObsError as e:
            if 'code 702' in str(e):   # OBS: source has nothing to render
                raise ObsError('"%s" shows no picture - is the game running and captured?' % src)
            raise
        try:
            img_id = self.req('GetSceneItemId', {'sceneName': scene, 'sourceName': img})['sceneItemId']
            self.req('SetInputSettings', {'inputName': img, 'inputSettings': {'file': self.screenshot_path}})
        except ObsError:
            img_id = self.req('CreateInput', {'sceneName': scene, 'inputName': img, 'inputKind': 'image_source',
                                              'inputSettings': {'file': self.screenshot_path},
                                              'sceneItemEnabled': False})['sceneItemId']
        # Same position/scale as the game capture, directly above it.
        t = self.req('GetSceneItemTransform', {'sceneName': scene, 'sceneItemId': game_id})['sceneItemTransform']
        t = {k: v for k, v in t.items() if k not in TRANSFORM_READONLY}
        # OBS reports 0x0 bounds when the item has no bounding box, but rejects
        # bounds below 1 on Set. Without a bounding box they are unused anyway.
        if t.get('boundsType') == 'OBS_BOUNDS_NONE' or t.get('boundsWidth', 1) < 1 or t.get('boundsHeight', 1) < 1:
            t.pop('boundsWidth', None)
            t.pop('boundsHeight', None)
        self.req('SetSceneItemTransform', {'sceneName': scene, 'sceneItemId': img_id, 'sceneItemTransform': t})
        game_idx = self.req('GetSceneItemIndex', {'sceneName': scene, 'sceneItemId': game_id})['sceneItemIndex']
        img_idx = self.req('GetSceneItemIndex', {'sceneName': scene, 'sceneItemId': img_id})['sceneItemIndex']
        if img_idx <= game_idx:
            self.req('SetSceneItemIndex', {'sceneName': scene, 'sceneItemId': img_id, 'sceneItemIndex': game_idx})
        self.req('SetSceneItemEnabled', {'sceneName': scene, 'sceneItemId': img_id, 'sceneItemEnabled': True})
        self.image_item = (scene, img_id)

    # ----- audio -----
    def mute_audio(self):
        name = self.cfg['game_audio_source']
        if not name or self.audio_was_muted is not None:
            return
        self.audio_was_muted = self.req('GetInputMute', {'inputName': name})['inputMuted']
        self.req('SetInputMute', {'inputName': name, 'inputMuted': True})

    def restore_audio(self):
        name = self.cfg['game_audio_source']
        if not name or self.audio_was_muted is None:
            return
        was = self.audio_was_muted
        self.audio_was_muted = None
        self.req('SetInputMute', {'inputName': name, 'inputMuted': was})

    # ----- public -----
    def cover_up(self):
        if self.cover_on:
            return
        self.freeze()
        self.cover_on = True
        try:
            self.mute_audio()
        except ObsError as e:
            log.warning('Could not mute "%s": %s', self.cfg['game_audio_source'], e)

    def cover_down(self):
        errors = []
        for step in (self.unfreeze, self.restore_audio):
            try:
                step()
            except ObsError as e:
                errors.append(str(e))
        self.cover_on = False
        if errors:
            raise ObsError('; '.join(errors))


class Companion:
    def __init__(self, cfg, controller):
        self.cfg = cfg
        self.obs = controller
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((cfg['listen_host'], int(cfg['listen_port'])))
        self.sock.settimeout(0.5)
        self.cover_since = 0.0
        self.last_seen = 0.0

    def reply(self, addr, msg_id, ok, error=None):
        payload = {'v': 1, 'type': 'ACK', 'id': msg_id, 'ok': ok}
        if error:
            payload['error'] = error
        self.sock.sendto(json.dumps(payload).encode('utf-8'), addr)

    def handle(self, data, addr):
        try:
            msg = json.loads(data.decode('utf-8'))
            kind, msg_id = msg['type'], msg.get('id')
        except (ValueError, KeyError, UnicodeDecodeError):
            return
        now = time.time()
        if kind == 'COVER_ON':
            try:
                if not self.obs.cover_on:
                    self.obs.cover_up()
                    self.cover_since = now
                    log.info('Stream frozen (%s, %d ms).', self.obs.method, (time.time() - now) * 1000)
                self.last_seen = now
                self.reply(addr, msg_id, True)
            except ObsError as e:
                log.error('Freeze failed: %s', e)
                self.reply(addr, msg_id, False, str(e))
        elif kind == 'PING':
            self.last_seen = now
            self.reply(addr, msg_id, self.obs.cover_on)
        elif kind == 'COVER_OFF':
            self.drop_cover('game asked')
            self.reply(addr, msg_id, True)

    def drop_cover(self, reason):
        if not self.obs.cover_on:
            return
        try:
            self.obs.cover_down()
            log.info('Stream unfrozen (%s).', reason)
        except ObsError as e:
            log.error('Unfreeze problem (%s): %s', reason, e)

    def watchdog(self):
        if not self.obs.cover_on:
            return
        now = time.time()
        if now - self.last_seen > self.cfg['heartbeat_timeout_seconds']:
            self.drop_cover('no heartbeat from the game')
        elif now - self.cover_since > self.cfg['max_cover_seconds']:
            self.drop_cover('maximum cover time reached')

    def run(self):
        log.info('%s %s listening on %s:%s', APP, VERSION, self.cfg['listen_host'], self.cfg['listen_port'])
        try:
            self.obs.connect()
            log.info('Freeze method: %s', self.obs.resolve_method())
        except ObsError as e:
            log.warning('%s - will retry when the game asks.', e)
        try:
            while True:
                try:
                    data, addr = self.sock.recvfrom(4096)
                    self.handle(data, addr)
                except socket.timeout:
                    pass
                except ConnectionResetError:
                    pass   # Windows: ICMP port unreachable from a previous reply
                self.watchdog()
        except KeyboardInterrupt:
            pass
        finally:
            self.drop_cover('companion exiting')
            self.sock.close()


def main(argv=None):
    base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
    ap = argparse.ArgumentParser(description=APP)
    ap.add_argument('--config', default=os.path.join(base, 'companion_config.json'))
    ap.add_argument('--selftest', type=float, metavar='SECONDS', nargs='?', const=5.0,
                    help='freeze the stream for SECONDS (default 5) and exit; no game needed')
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s.%(msecs)03d %(levelname)s %(message)s',
                        datefmt='%H:%M:%S')
    logging.getLogger('obsws_python').setLevel(logging.CRITICAL)
    cfg = load_config(args.config)
    ctl = ObsController(cfg, base)
    if args.selftest is not None:
        sys.exit(selftest(ctl, args.selftest))
    Companion(cfg, ctl).run()


def selftest(ctl, seconds):
    """Freeze + mute, wait, restore. Returns a process exit code."""
    try:
        ctl.connect()
        log.info('Freeze method: %s', ctl.resolve_method())
        ctl.cover_up()
        log.info('Frozen. Move something in the game window: the OBS preview should stay still for %.0f s.',
                 seconds)
        time.sleep(seconds)
        ctl.cover_down()
        log.info('Unfrozen. Self-test passed.')
        return 0
    except ObsError as e:
        log.error('Self-test failed: %s', e)
        try:
            ctl.cover_down()
        except ObsError:
            pass
        return 1


if __name__ == '__main__':
    main()
