# Python 2.7 harness: stubs the game's modules and drives the mod's state machine.
# Run: python2.7 tests/test_mod.py [companion_port]   (no port -> built-in fake companion)
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
MOD_DIR = os.path.join(HERE, '..', 'res', 'scripts', 'client', 'gui', 'mods')


def module(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


class Event(object):
    def __init__(self):
        self.h = []

    def __iadd__(self, f):
        self.h.append(f)
        return self

    def __isub__(self, f):
        self.h.remove(f)
        return self

    def __call__(self, *a):
        for f in list(self.h):
            f(*a)


# ---- fake game world ----
class World(object):
    pass


W = World()
W.callbacks = []        # (fire_time, id, fn)
W.now = 1000.0
W.cbid = 0
W.player = None
W.messages = []
W.volumes = {'gui': 0.8, 'music': 0.5}
W.applied = dict(W.volumes)
W.enqueue_calls = 0
W.nav_ok = True


def callback(delay, fn):
    W.cbid += 1
    W.callbacks.append((W.now + delay, W.cbid, fn))
    return W.cbid


def cancel(cid):
    W.callbacks = [c for c in W.callbacks if c[1] != cid]


def advance(seconds, step=0.25):
    end = W.now + seconds
    while W.now < end:
        W.now += step
        due = [c for c in W.callbacks if c[0] <= W.now]
        W.callbacks = [c for c in W.callbacks if c[0] > W.now]
        for c in sorted(due):
            c[2]()
        time.sleep(0.005)   # let the UDP responder answer


import time as _time
_real_time = _time.time
_time.time = lambda: W.now

module('BigWorld', callback=callback, cancelCallback=cancel, player=lambda: W.player)
module('Keys', KEY_0=11, KEY_NUMPAD0=82, KEY_F12=88)
module('constants', QUEUE_TYPE=types.SimpleNamespace(RANDOMS=1, UNKNOWN=0) if hasattr(types, 'SimpleNamespace')
       else type('Q', (), {'RANDOMS': 1, 'UNKNOWN': 0}))


def adisp_process(fn):
    def run(*a, **k):
        g = fn(*a, **k)
        try:
            v = g.next()
            while True:
                v = g.send(W.nav_ok)
        except StopIteration:
            pass
    return run


module('adisp', adisp_process=adisp_process)


class CurVeh(object):
    invID = 11

    def isPresent(self):
        return True


module('CurrentVehicle', g_currentVehicle=CurVeh())


class LobbyCtx(object):
    def isHeaderNavigationPossible(self):
        return True

    def isFightButtonPressPossible(self):
        return True


class Descriptor(object):
    def __get__(self, obj, cls):
        return LobbyCtx()


module('helpers')
module('helpers.dependency', descriptor=lambda c: Descriptor())
sys.modules['helpers'].dependency = sys.modules['helpers.dependency']
module('skeletons'); module('skeletons.gui'); module('skeletons.gui.lobby_context', ILobbyContext=object)

PE = type('PE', (), {})()
for n in ('onArenaCreated', 'onAvatarBecomePlayer', 'onEnqueued', 'onEnqueueFailure', 'onKickedFromQueue',
          'onKickedFromUnitsQueue', 'onArenaJoinFailure', 'onKickedFromArena', 'onAccountBecomeNonPlayer'):
    setattr(PE, n, Event())
module('PlayerEvents', g_playerEvents=PE)


class SG(object):
    def setVolume(self, cat, v, updatePrefs=True):
        assert updatePrefs is False
        W.applied[cat] = v

    def getVolume(self, cat):
        return W.volumes[cat]


module('SoundGroups', g_instance=SG())

sm = module('gui.SystemMessages', SM_TYPE=type('T', (), {'Error': 'E', 'Information': 'I'}),
            pushMessage=lambda text, type=None: W.messages.append((type, text)))
INPUT = module('gui.InputHandler', g_instance=type('IH', (), {})())
INPUT.g_instance.onKeyDown = Event()
module('gui', g_keyEventHandlers=set(), SystemMessages=sm, InputHandler=INPUT)


# prb entities
class RandomEntity(object):
    commander = True

    def __init__(self):
        self.queued = False

    def getQueueType(self):
        return 1

    def isInQueue(self):
        return self.queued

    def isCommander(self):
        return self.commander

    def exitFromQueue(self):
        self.queued = False


class RandomSquadEntity(RandomEntity):
    pass


class OtherEntity(RandomEntity):
    def getQueueType(self):
        return 5


class Dispatcher(object):
    entity = RandomEntity()

    def getEntity(self):
        return self.entity

    def doAction(self, action):
        W.enqueue_calls += 1
        self.entity.queued = True   # server confirms immediately in this fake
        return True


DISP = Dispatcher()
for n in ('gui.prb_control', 'gui.prb_control.entities', 'gui.prb_control.entities.base',
          'gui.prb_control.entities.random', 'gui.prb_control.entities.random.pre_queue',
          'gui.prb_control.entities.random.squad'):
    module(n)
module('gui.prb_control.dispatcher', g_prbLoader=type('L', (), {'getDispatcher': staticmethod(lambda: DISP)}))
module('gui.prb_control.entities.base.ctx', PrbAction=lambda *a: a)
module('gui.prb_control.entities.random.pre_queue.entity', RandomEntity=RandomEntity)
module('gui.prb_control.entities.random.squad.entity', RandomSquadEntity=RandomSquadEntity)


class Account(object):
    def enqueueRandom(self):
        pass


class KeyEv(object):
    def __init__(self, key=11, down=True, rep=False, ctrl=True, shift=False, alt=False):
        self.key, self.d, self.r = key, down, rep
        self.c, self.s, self.a = ctrl, shift, alt

    def isCtrlDown(self):
        return self.c

    def isShiftDown(self):
        return self.s

    def isAltDown(self):
        return self.a

    def isKeyDown(self):
        return self.d

    def isRepeatedEvent(self):
        return self.r


# ---- fake companion (unless a real one is given) ----
class FakeCompanion(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind(('127.0.0.1', 0))
        self.port = self.s.getsockname()[1]
        self.log = []
        self.ok = True

    def run(self):
        while True:
            d, a = self.s.recvfrom(4096)
            m = json.loads(d)
            self.log.append(m['type'])
            self.s.sendto(json.dumps({'type': 'ACK', 'id': m['id'], 'ok': self.ok, 'error': 'boom'}), a)


# ---- load the mod with a temp config ----
REAL_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else None
comp = None
if REAL_PORT is None:
    comp = FakeCompanion()
    comp.start()
port = REAL_PORT or comp.port

workdir = tempfile.mkdtemp()
os.chdir(workdir)
os.makedirs(os.path.join('mods', 'configs', 'stealthstart'))
with open(os.path.join('mods', 'configs', 'stealthstart', 'config.json'), 'w') as f:
    json.dump({'companion_port': port, 'delay_min_seconds': 5, 'delay_max_seconds': 10}, f)

sys.path.insert(0, MOD_DIR)
import mod_stealthstart as M

M.init()
qc = M.g_stealthStart
W.player = Account()
handler = INPUT.g_instance.onKeyDown   # fires like the game's InputHandler

passed = []


def check(name, cond):
    if not cond:
        print 'FAIL', name, '| state', qc.state, '| msgs', W.messages[-3:], '| comp', comp and comp.log[-5:]
        sys.exit(1)
    passed.append(name)


def reset():
    qc.finish('reset', notify=False)
    DISP.entity = RandomEntity()
    W.messages[:] = []
    W.enqueue_calls = 0
    if comp:
        comp.ok = True
        del comp.log[:]
    advance(0.5)


def wait_ack():
    for _ in range(40):
        advance(0.05, step=0.05)
        time.sleep(0.01)
        if qc.state != M.ARMING:
            return


# 1. happy path
handler(KeyEv())
check('arming', qc.state == M.ARMING)
wait_ack()
check('waiting after ack', qc.state == M.WAITING)
check('gui muted', W.applied['gui'] == 0.0)
check('music untouched', W.applied['music'] == 0.5)
check('no enqueue yet', W.enqueue_calls == 0)
advance(11)
check('enqueued after delay', W.enqueue_calls == 1 and qc.state == M.QUEUED)
check('still muted in queue', W.applied['gui'] == 0.0)
PE.onArenaCreated()
check('loading', qc.state == M.LOADING)
PE.onAvatarBecomePlayer()
check('idle after battle load', qc.state == M.IDLE)
check('gui restored', W.applied['gui'] == 0.8)
advance(0.5)
if comp:
    check('cover on/off sent', 'COVER_ON' in comp.log and comp.log[-1] == 'COVER_OFF')
    check('heartbeats sent', comp.log.count('PING') >= 3)

# 2. cancel during wait
reset()
handler(KeyEv()); wait_ack()
handler(KeyEv())
check('cancel -> idle', qc.state == M.IDLE and W.applied['gui'] == 0.8)
advance(12)
check('no enqueue after cancel', W.enqueue_calls == 0)

# 3. vehicle change during wait
reset()
handler(KeyEv()); wait_ack()
sys.modules['CurrentVehicle'].g_currentVehicle.invID = 12
advance(1)
check('vehicle change cancels', qc.state == M.IDLE)
sys.modules['CurrentVehicle'].g_currentVehicle.invID = 11

# 4. leave queue with hotkey
reset()
handler(KeyEv()); wait_ack(); advance(11)
check('queued', qc.state == M.QUEUED)
handler(KeyEv())
advance(1)
check('dequeue -> idle', qc.state == M.IDLE and not DISP.entity.queued)

# 5. fail closed: companion refuses
if comp:
    reset()
    comp.ok = False
    handler(KeyEv()); wait_ack()
    check('nack -> idle, no mute', qc.state == M.IDLE and W.applied['gui'] == 0.8)
    check('nack message', any('could not freeze' in t for _, t in W.messages))

# 6. fail closed: companion silent
reset()
old = qc.link._addr
qc.link._addr = ('127.0.0.1', 9)  # discard port, nobody answers
handler(KeyEv()); advance(5)
check('timeout -> idle', qc.state == M.IDLE and W.enqueue_calls == 0)
check('timeout message', any('not responding' in t for _, t in W.messages))
qc.link._addr = old

# 7. unsupported mode / platoon member
reset()
DISP.entity = OtherEntity()
handler(KeyEv())
check('other mode refused', qc.state == M.IDLE and any('only Random' in t for _, t in W.messages))
reset()
DISP.entity = RandomSquadEntity(); DISP.entity.commander = False
handler(KeyEv())
check('platoon member refused', qc.state == M.IDLE and any('commander' in t for _, t in W.messages))
reset()
DISP.entity = RandomSquadEntity()
handler(KeyEv()); wait_ack()
check('platoon commander ok', qc.state == M.WAITING)

# 8. manual Battle press during wait keeps cover
reset()
handler(KeyEv()); wait_ack()
DISP.entity.queued = True
advance(1)
check('manual queue tracked', qc.state == M.QUEUED and qc.coverOn and W.enqueue_calls == 0)

# 9. other keys / repeats / in battle ignored
reset()
handler(KeyEv(key=88)); check('other key ignored', qc.state == M.IDLE)
handler(KeyEv(ctrl=False)); check('plain 0 (typing) ignored', qc.state == M.IDLE)
handler(KeyEv(shift=True)); check('ctrl+shift+0 ignored', qc.state == M.IDLE)
handler(KeyEv(rep=True)); check('repeat ignored', qc.state == M.IDLE)
W.player = object()
handler(KeyEv()); check('not in lobby ignored', qc.state == M.IDLE)
W.player = Account()

M.fini()
os.chdir(HERE)   # Windows can't delete the current directory
shutil.rmtree(workdir, ignore_errors=True)
print 'OK - %d checks passed' % len(passed)
