# -*- coding: utf-8 -*-
"""Stealth Start - anti stream-sniping queue delay for World of Tanks (Wargaming client).

Flow:
  hotkey -> ask the OBS companion to freeze the stream (fail closed if no ACK)
         -> mute selected in-game sound categories
         -> wait a random delay -> join the Random Battle queue
         -> battle loading starts -> unmute + ask the companion to unfreeze.

Hooks verified against client scripts v2.4.1.1 (IzeBerg/wot-src):
  gui.g_keyEventHandlers, gui.impl.lobby.page.prebattle_presenter (Battle button
  path), gui.prb_control entities, PlayerEvents.g_playerEvents, SoundGroups.
"""
import json
import os
import random
import socket
import time

import BigWorld
import Keys
from adisp import adisp_process
from constants import QUEUE_TYPE
from CurrentVehicle import g_currentVehicle
from helpers import dependency
from PlayerEvents import g_playerEvents
from skeletons.gui.lobby_context import ILobbyContext

MOD_NAME = 'Stealth Start'
MOD_VERSION = '0.1.0'
CONFIG_DIR = os.path.join('mods', 'configs', 'stealthstart')
CONFIG_PATH = os.path.join(CONFIG_DIR, 'config.json')

DEFAULT_CONFIG = {
    'enabled': True,
    # Key name from the game's Keys module (KEY_0, KEY_NUMPAD0, KEY_F9, ...).
    'hotkey': 'KEY_0',
    # Modifiers that must be held: any of "ctrl", "shift", "alt". Exact match.
    # Keep at least one for keys you might type in chat (letters, digits).
    'hotkey_modifiers': ['ctrl'],
    'delay_min_seconds': 1.0,
    'delay_max_seconds': 60.0,
    # Fail closed: refuse to start unless the companion confirms the freeze.
    'require_companion': True,
    'companion_host': '127.0.0.1',
    'companion_port': 47811,
    # How long to wait for the companion to confirm the freeze before failing closed.
    'companion_ack_timeout_seconds': 4.0,
    'heartbeat_interval_seconds': 2.0,
    # In-game sound categories muted from hotkey press until battle loading.
    # Known categories: gui, music, ambient, effects, vehicles.
    'mute_sound_categories': ['gui'],
    # Notification with the rolled delay. Safe because the stream is frozen.
    'show_delay_in_notification': True,
    # Grace period for the server to confirm we are in the queue.
    'enqueue_confirm_timeout_seconds': 10.0,
    # Extra '[Stealth Start] DEBUG' lines in game.log (key presses, state changes).
    'debug_log': False,
}

# States
IDLE = 'IDLE'
ARMING = 'ARMING'        # COVER_ON sent, waiting for the companion's ACK
WAITING = 'WAITING'      # cover up, random timer running
QUEUED = 'QUEUED'        # enqueue requested / in queue
LOADING = 'LOADING'      # arena created, waiting for the battle to load

POLL_INTERVAL = 0.5


def _log(*parts):
    print '[%s] %s' % (MOD_NAME, ' '.join(str(p) for p in parts))


_DEBUG = [False]   # set from config 'debug_log' at startup


def _dbg(*parts):
    if _DEBUG[0]:
        _log('DEBUG', *parts)


def _notify(text, error=False):
    _dbg('notify:', text)
    try:
        from gui import SystemMessages
        msgType = SystemMessages.SM_TYPE.Error if error else SystemMessages.SM_TYPE.Information
        SystemMessages.pushMessage('%s: %s' % (MOD_NAME, text), type=msgType)
    except Exception as e:
        _log('notify failed:', e, text)


def _loadConfig():
    cfg = dict(DEFAULT_CONFIG)
    try:
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH, 'r') as f:
                user = json.load(f)
            for k, v in user.items():
                if k in cfg:
                    cfg[k] = v
        else:
            if not os.path.isdir(CONFIG_DIR):
                os.makedirs(CONFIG_DIR)
            with open(CONFIG_PATH, 'w') as f:
                json.dump(DEFAULT_CONFIG, f, indent=2, sort_keys=True)
            _log('wrote default config to', CONFIG_PATH)
    except Exception as e:
        _log('config error, using defaults:', e)
    lo = max(0.0, float(cfg['delay_min_seconds']))
    hi = max(lo, float(cfg['delay_max_seconds']))
    cfg['delay_min_seconds'], cfg['delay_max_seconds'] = lo, hi
    return cfg


class CompanionLink(object):
    """Fire-and-forget UDP to the OBS companion, with ACK matching by id."""

    def __init__(self, host, port):
        self._addr = (host, int(port))
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self._nextId = 1

    def send(self, msgType, **extra):
        msgId = self._nextId
        self._nextId += 1
        payload = {'v': 1, 'type': msgType, 'id': msgId}
        payload.update(extra)
        try:
            self._sock.sendto(json.dumps(payload), self._addr)
        except socket.error as e:
            _log('send failed:', msgType, e)
        return msgId

    def pollAcks(self):
        acks = []
        while True:
            try:
                data, _ = self._sock.recvfrom(4096)
            except socket.error:
                break
            try:
                msg = json.loads(data)
                if msg.get('type') == 'ACK':
                    acks.append(msg)
            except ValueError:
                pass
        return acks

    def close(self):
        try:
            self._sock.close()
        except Exception:
            pass


class StealthStart(object):
    lobbyContext = dependency.descriptor(ILobbyContext)

    def __init__(self):
        self.cfg = _loadConfig()
        self.hotkey = getattr(Keys, str(self.cfg['hotkey']), None)
        if self.hotkey is None:
            _log('unknown hotkey %r, using KEY_0' % self.cfg['hotkey'])
            self.cfg['hotkey'] = 'KEY_0'
            self.hotkey = Keys.KEY_0
        mods = self.cfg['hotkey_modifiers']
        if isinstance(mods, basestring):
            mods = [mods]
        self.hotkeyMods = set(str(m).strip().lower() for m in mods) & set(('ctrl', 'shift', 'alt'))
        self.link = CompanionLink(self.cfg['companion_host'], self.cfg['companion_port'])
        self.state = IDLE
        self.coverOn = False
        self.muted = []
        self.snapshot = None
        self.fireAt = 0.0
        self.enqueueRequestedAt = 0.0
        self.seenInQueue = False
        self.armingId = None
        self.armingDeadline = 0.0
        self.pollCbId = None
        self.heartbeatCbId = None

    # ---------- lifecycle ----------
    def start(self):
        _DEBUG[0] = bool(self.cfg.get('debug_log'))
        from gui import InputHandler
        InputHandler.g_instance.onKeyDown += self.onKeyEvent
        g_playerEvents.onArenaCreated += self.onArenaCreated
        g_playerEvents.onAvatarBecomePlayer += self.onAvatarBecomePlayer
        g_playerEvents.onEnqueued += self.onEnqueued
        g_playerEvents.onEnqueueFailure += self.onEnqueueFailure
        g_playerEvents.onKickedFromQueue += self.onKicked
        g_playerEvents.onKickedFromUnitsQueue += self.onKicked
        g_playerEvents.onArenaJoinFailure += self.onKicked
        g_playerEvents.onKickedFromArena += self.onKicked
        g_playerEvents.onAccountBecomeNonPlayer += self.onAccountGone
        self.schedulePoll()
        _log('loaded v%s, hotkey=%s, delay=%.0f-%.0fs' % (
            MOD_VERSION, self.hotkeyLabel(), self.cfg['delay_min_seconds'], self.cfg['delay_max_seconds']))

    def hotkeyLabel(self):
        return '+'.join([m.capitalize() for m in ('ctrl', 'shift', 'alt') if m in self.hotkeyMods] +
                        [str(self.cfg['hotkey'])])

    def stop(self):
        self.finish('mod unloaded', notify=False)
        try:
            from gui import InputHandler
            InputHandler.g_instance.onKeyDown -= self.onKeyEvent
        except Exception:
            pass
        for evName, handler in (('onArenaCreated', self.onArenaCreated),
                                ('onAvatarBecomePlayer', self.onAvatarBecomePlayer),
                                ('onEnqueued', self.onEnqueued),
                                ('onEnqueueFailure', self.onEnqueueFailure),
                                ('onKickedFromQueue', self.onKicked),
                                ('onKickedFromUnitsQueue', self.onKicked),
                                ('onArenaJoinFailure', self.onKicked),
                                ('onKickedFromArena', self.onKicked),
                                ('onAccountBecomeNonPlayer', self.onAccountGone)):
            try:
                ev = getattr(g_playerEvents, evName)
                ev -= handler
            except Exception:
                pass
        for cb in (self.pollCbId, self.heartbeatCbId):
            if cb is not None:
                try:
                    BigWorld.cancelCallback(cb)
                except Exception:
                    pass
        self.link.close()

    # ---------- helpers ----------
    @staticmethod
    def inLobby():
        player = BigWorld.player()
        return player is not None and hasattr(player, 'enqueueRandom')

    @staticmethod
    def prbDispatcher():
        from gui.prb_control.dispatcher import g_prbLoader
        return g_prbLoader.getDispatcher()

    def entity(self):
        d = self.prbDispatcher()
        return d.getEntity() if d is not None else None

    def checkSupported(self, entity):
        """Random Battles, solo or as platoon commander. Returns error text or None."""
        if entity is None:
            return 'prebattle system not ready'
        from gui.prb_control.entities.random.pre_queue.entity import RandomEntity
        from gui.prb_control.entities.random.squad.entity import RandomSquadEntity
        if entity.getQueueType() != QUEUE_TYPE.RANDOMS or not isinstance(entity, (RandomEntity, RandomSquadEntity)):
            return 'only Random Battles are supported'
        if isinstance(entity, RandomSquadEntity) and not entity.isCommander():
            return 'only the platoon commander can use the hotkey'
        if not g_currentVehicle.isPresent():
            return 'no vehicle selected'
        return None

    def takeSnapshot(self, entity):
        return (g_currentVehicle.invID, id(entity), type(entity).__name__)

    # ---------- input ----------
    @staticmethod
    def eventModifiers(event):
        mods = set()
        if event.isCtrlDown():
            mods.add('ctrl')
        if event.isShiftDown():
            mods.add('shift')
        if event.isAltDown():
            mods.add('alt')
        return mods

    def onKeyEvent(self, event):
        # Registered on gui.InputHandler.onKeyDown: called for every non-repeat key
        # press BEFORE the GUI. The 2.x lobby UI consumes all keys, so the later
        # gui.g_keyEventHandlers hook never sees them. Because this hook also sees
        # keys typed into chat/search fields, the hotkey needs a modifier (Ctrl+0
        # by default) that typing never produces.
        try:
            if not (self.cfg['enabled'] and event.key == self.hotkey):
                return
            mods = self.eventModifiers(event)
            _dbg('hotkey pressed, mods=%s inLobby=%s state=%s' % (sorted(mods), self.inLobby(), self.state))
            if mods != self.hotkeyMods or event.isRepeatedEvent() or not self.inLobby():
                return
            self.onHotkey()
        except Exception as e:
            _log('key handler error:', e)

    def onHotkey(self):
        _dbg('onHotkey, state=%s' % self.state)
        if self.state in (ARMING, WAITING):
            self.finish('cancelled')
            return
        if self.state == QUEUED:
            self.leaveQueue()
            return
        if self.state == LOADING:
            return
        entity = self.entity()
        if entity is not None and entity.isInQueue():
            # Queued manually without the mod: hotkey just leaves the queue.
            entity.exitFromQueue()
            return
        err = self.checkSupported(entity)
        if err:
            _notify(err, error=True)
            return
        self.snapshot = self.takeSnapshot(entity)
        if self.cfg['require_companion']:
            self.state = ARMING
            self.armingId = self.link.send('COVER_ON')
            self.armingDeadline = time.time() + float(self.cfg['companion_ack_timeout_seconds'])
        else:
            self.link.send('COVER_ON')
            self.beginWaiting(coverConfirmed=False)

    # ---------- state transitions ----------
    def beginWaiting(self, coverConfirmed):
        self.coverOn = True
        self.muteSounds()
        self.startHeartbeat()
        delay = random.uniform(self.cfg['delay_min_seconds'], self.cfg['delay_max_seconds'])
        self.fireAt = time.time() + delay
        self.state = WAITING
        if self.cfg['show_delay_in_notification']:
            _notify('stream frozen, joining queue in %d s (hotkey again to cancel)' % int(round(delay)))
        if not coverConfirmed:
            _notify('companion not confirmed - stream may NOT be frozen', error=True)
        _log('waiting %.1fs' % delay)

    def fireQueue(self):
        entity = self.entity()
        err = self.checkSupported(entity)
        if err:
            self.finish('could not join queue: %s' % err, error=True)
            return
        if self.takeSnapshot(entity) != self.snapshot:
            self.finish('cancelled: vehicle or mode changed', error=True)
            return
        self.state = QUEUED
        self.seenInQueue = False
        self.enqueueRequestedAt = time.time()
        self.requestBattle()

    @adisp_process
    def requestBattle(self):
        # Mirrors PrebattlePresenter.__startBattleHandler (the Battle button).
        navOk = yield self.lobbyContext.isHeaderNavigationPossible()
        fightOk = yield self.lobbyContext.isFightButtonPressPossible()
        if self.state != QUEUED:
            return
        if not (navOk and fightOk):
            self.finish('could not join queue: battle start blocked by the game', error=True)
            return
        from gui.prb_control.entities.base.ctx import PrbAction
        dispatcher = self.prbDispatcher()
        entity = self.entity()
        if dispatcher is None or entity is None or entity.isInQueue():
            # doAction toggles, so never call it while already queued.
            if entity is not None and entity.isInQueue():
                self.seenInQueue = True
                return
            self.finish('could not join queue', error=True)
            return
        # Unit entities may return None on success; only an explicit False is a refusal.
        if dispatcher.doAction(PrbAction('', 0)) is False:
            self.finish('could not join queue', error=True)
            return
        _notify('joining the queue')

    def leaveQueue(self):
        entity = self.entity()
        if entity is not None and entity.isInQueue():
            entity.exitFromQueue()   # dequeue is detected by polling -> finish()
        else:
            self.finish('cancelled')

    def finish(self, reason, error=False, notify=True):
        """Return to IDLE: unmute, unfreeze. Safe to call repeatedly."""
        wasActive = self.state != IDLE or self.coverOn
        if wasActive:
            _dbg('finish from state=%s: %s' % (self.state, reason))
        self.state = IDLE
        self.armingId = None
        self.unmuteSounds()
        if self.coverOn:
            self.link.send('COVER_OFF')
            self.coverOn = False
        if wasActive:
            _log('finish:', reason)
            if notify:
                _notify(reason, error=error)

    # ---------- polling ----------
    def schedulePoll(self):
        self.pollCbId = BigWorld.callback(POLL_INTERVAL, self.poll)

    def poll(self):
        self.pollCbId = None
        try:
            self.tick()
        except Exception as e:
            _log('poll error:', e)
        self.schedulePoll()

    def tick(self):
        acks = self.link.pollAcks()
        now = time.time()
        if self.state == ARMING:
            for ack in acks:
                if ack.get('id') == self.armingId:
                    if ack.get('ok'):
                        self.beginWaiting(coverConfirmed=True)
                    else:
                        self.state = IDLE
                        _notify('companion could not freeze the stream: %s' % ack.get('error', 'unknown'), error=True)
                    return
            if now > self.armingDeadline:
                self.state = IDLE
                self.link.send('COVER_OFF')
                _notify('OBS companion not responding - start it and OBS, then try again', error=True)
            return
        if self.state not in (WAITING, QUEUED):
            return
        if not self.inLobby():
            return
        entity = self.entity()
        if self.state == WAITING:
            if entity is not None and entity.isInQueue():
                # Battle pressed manually during the wait: keep the cover, track the queue.
                self.state = QUEUED
                self.seenInQueue = True
                return
            if entity is None or self.takeSnapshot(entity) != self.snapshot:
                self.finish('cancelled: vehicle or mode changed', error=True)
                return
            if now >= self.fireAt:
                self.fireQueue()
            return
        # QUEUED
        inQueue = entity is not None and entity.isInQueue()
        if inQueue:
            self.seenInQueue = True
        elif self.seenInQueue:
            self.finish('left the queue')
        elif now - self.enqueueRequestedAt > float(self.cfg['enqueue_confirm_timeout_seconds']):
            self.finish('queue was not confirmed by the server', error=True)

    # ---------- audio ----------
    def muteSounds(self):
        try:
            import SoundGroups
            for cat in self.cfg['mute_sound_categories']:
                # updatePrefs=False: nothing is saved, so a crash can't leave it muted.
                SoundGroups.g_instance.setVolume(cat, 0.0, updatePrefs=False)
                self.muted.append(cat)
        except Exception as e:
            _log('mute failed:', e)

    def unmuteSounds(self):
        if not self.muted:
            return
        try:
            import SoundGroups
            for cat in self.muted:
                SoundGroups.g_instance.setVolume(cat, SoundGroups.g_instance.getVolume(cat), updatePrefs=False)
        except Exception as e:
            _log('unmute failed:', e)
        self.muted = []

    # ---------- heartbeat ----------
    def startHeartbeat(self):
        if self.heartbeatCbId is None:
            self.heartbeatCbId = BigWorld.callback(float(self.cfg['heartbeat_interval_seconds']), self.heartbeat)

    def heartbeat(self):
        self.heartbeatCbId = None
        if self.coverOn:
            self.link.send('PING')
            self.startHeartbeat()

    # ---------- game events ----------
    def onEnqueued(self, *args, **kwargs):
        if self.state == QUEUED:
            self.seenInQueue = True

    def onEnqueueFailure(self, *args, **kwargs):
        if self.state == QUEUED:
            self.finish('the server rejected the queue request', error=True)

    def onKicked(self, *args, **kwargs):
        if self.state in (QUEUED, LOADING):
            self.finish('removed from the queue', error=True)

    def onArenaCreated(self, *args, **kwargs):
        if self.state in (WAITING, QUEUED):
            self.state = LOADING

    def onAvatarBecomePlayer(self, *args, **kwargs):
        # Battle loading screen: matchmaking is done, safe to show the stream again.
        if self.state != IDLE or self.coverOn:
            self.finish('battle found', notify=False)

    def onAccountGone(self, *args, **kwargs):
        if self.state in (ARMING, WAITING, QUEUED):
            self.finish('left the hangar', notify=False)


g_stealthStart = None


def init():
    global g_stealthStart
    try:
        g_stealthStart = StealthStart()
        g_stealthStart.start()
    except Exception as e:
        _log('init failed:', e)


def fini():
    global g_stealthStart
    if g_stealthStart is not None:
        g_stealthStart.stop()
        g_stealthStart = None
