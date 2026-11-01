"""Build stealthstart.wotmod. Must be run with Python 2.7 (the game's Python).

    python2.7 build.py            -> dist/<id>_<version>.wotmod
"""
from __future__ import print_function
import os
import py_compile
import re
import shutil
import sys
import tempfile
import zipfile

if sys.version_info[:2] != (2, 7):
    sys.exit('Run this with Python 2.7: the game only loads 2.7 bytecode.')

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, 'res', 'scripts', 'client', 'gui', 'mods', 'mod_stealthstart.py')
MOD_ID = 'stealthstart'

with open(SRC) as f:
    VERSION = re.search(r"MOD_VERSION = '([^']+)'", f.read()).group(1)

META = """<root>
  <id>{id}</id>
  <version>{version}</version>
  <name>Stealth Start</name>
  <description>Anti stream-sniping: hotkey joins Random Battles after a hidden random delay while the OBS companion freezes the stream.</description>
</root>
""".format(id=MOD_ID, version=VERSION)


def main():
    tmp = tempfile.mkdtemp()
    try:
        pyc = os.path.join(tmp, 'mod_stealthstart.pyc')
        py_compile.compile(SRC, cfile=pyc, dfile='mod_stealthstart.py', doraise=True)
        dist = os.path.join(HERE, 'dist')
        if not os.path.isdir(dist):
            os.makedirs(dist)
        out = os.path.join(dist, '%s_%s.wotmod' % (MOD_ID, VERSION))
        # .wotmod = uncompressed zip; directory entries are expected by the loader.
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_STORED) as z:
            z.writestr('meta.xml', META)
            for d in ('res/', 'res/scripts/', 'res/scripts/client/', 'res/scripts/client/gui/',
                      'res/scripts/client/gui/mods/'):
                z.writestr(zipfile.ZipInfo(d), '')
            z.write(pyc, 'res/scripts/client/gui/mods/mod_stealthstart.pyc')
        print('built', out)
    finally:
        shutil.rmtree(tmp)


if __name__ == '__main__':
    main()
