# Stealth Start

Anti stream-sniping for World of Tanks (Wargaming client) streamers.

Press a hotkey instead of **Battle**. The stream freezes on the hangar, the game's
interface sounds go quiet, and the mod joins the Random Battle queue after a
hidden random delay (1–60 s). When the battle starts loading, the stream comes
back. Viewers only ever see when the hotkey was pressed, never when the queue
actually started.

```
hotkey ─► companion freezes OBS + mutes game audio ─► ACK ─► mute in-game GUI sounds
       ─► random 1–60 s ─► join queue (normal queue UI, invisible on stream)
       ─► match found ─► battle loading ─► unmute + unfreeze
```

Two parts:

| Part | Folder | Runs in |
|---|---|---|
| WoT mod (`stealthstart_0.1.0.wotmod`) | `mod/` | the game (Python 2.7) |
| OBS companion | `companion/` | next to OBS (Python 3.9+) |

They talk over localhost UDP (port 47811).

## Install

### 1. OBS
- OBS 28 or newer. Enable **Tools → WebSocket Server Settings → Enable WebSocket server**, note the port (4455) and password.
- Recommended: install Exeldro's **Freeze Filter** plugin. The companion adds a filter called `Stealth Start Freeze` to your game capture source by itself.
- Without the plugin, the companion falls back to a screenshot overlay: it saves a PNG of the game capture and shows it in an image source (`Stealth Start Freeze Image`) placed exactly over the game, creating that source if needed.
- In the game capture source, turn off **Capture Cursor** so the frozen frame has no stuck cursor.

### 2. Companion
```
cd companion
pip install -r requirements.txt
python stealth_start_companion.py
```
First run writes `companion_config.json`. Set at least:
- `obs_password`
- `game_capture_source` – the exact name of your WoT capture source in OBS
- `game_audio_source` – the OBS source carrying game audio (often the same source); `""` disables OBS-side muting

Check the OBS side on its own with `python stealth_start_companion.py --selftest` (freezes for 5 s, then restores).

Single .exe for streamers (build on Windows): `pip install pyinstaller && pyinstaller --onefile stealth_start_companion.py`

### 3. Mod
Copy `stealthstart_0.1.0.wotmod` into `World_of_Tanks/mods/<game version>/`.
First launch writes `World_of_Tanks/mods/configs/stealthstart/config.json`.

## Use
- **Hotkey (Ctrl+0):** start. Press again to cancel the wait, or to leave the queue.
- Changing tank, mode or platoon during the wait cancels it.
- The normal Battle button is unchanged. If you press it during the wait, the mod keeps the stream frozen and tracks that queue.
- Random Battles only, solo or as platoon commander.
- While frozen, notifications in the hangar show the rolled delay; viewers can't see them.

If OBS or the companion isn't running, the hotkey **does nothing except show an error** (fail closed), so you never queue thinking you're covered when you aren't.

## Safety nets
- In-game volume is changed with `updatePrefs=False`: nothing is saved, so a crash can't leave the game muted.
- The companion restores the audio source's previous mute state, not just "unmuted".
- The companion unfreezes on its own if the game stops sending heartbeats for 15 s (crash, disconnect) or after 10 minutes.

## Mod config (`mods/configs/stealthstart/config.json`)
| Key | Default | Notes |
|---|---|---|
| `hotkey` | `KEY_0` | any `Keys.KEY_*` name |
| `hotkey_modifiers` | `["ctrl"]` | any of `ctrl`, `shift`, `alt`; exact match |
| `debug_log` | `false` | extra `DEBUG` lines in `game.log` |
| `delay_min_seconds` / `delay_max_seconds` | 1 / 60 | re-rolled every press |
| `require_companion` | `true` | fail closed |
| `companion_port` | 47811 | must match the companion |
| `mute_sound_categories` | `["gui"]` | game sound categories: `gui`, `music`, `ambient`, `effects`, `vehicles` |
| `show_delay_in_notification` | `true` | |

## Status: what's verified and what needs a real client
Tested here: the mod's state machine under Python 2.7 with stubbed game modules (29 checks), the companion against a fake OBS (12 tests), and both together over UDP.

Hooks were taken from the decompiled client scripts for **v2.4.1.1** (IzeBerg/wot-src): the Battle button path (`PrebattlePresenter.__startBattleHandler` → `prbDispatcher.doAction`), `gui.g_keyEventHandlers`, `g_playerEvents`, `SoundGroups.setVolume`.

Needs checking in the real game:
1. **Hotkey (verified on 2.4.1.1).** The 2.x lobby UI consumes every key, so the mod listens on `gui.InputHandler.onKeyDown`, which runs before the GUI. That hook also sees keys typed into chat, so the hotkey needs a modifier (Ctrl+0 by default). The game window must have keyboard focus.
2. **Which sounds leak.** Listen for any sound on queue join, "battle found" and the countdown with only `gui` muted; add categories if needed. The OBS-side mute covers it either way.
3. **Platoon commander queue** (members must be ready; if they aren't, the mod unfreezes after 10 s with "queue was not confirmed").
4. **Unfreeze timing** at `onAvatarBecomePlayer` (battle loading).
5. Test in a training room first, then real Random Battles.

See [TESTING.md](TESTING.md) for the step-by-step test plan.

## Fair Play
The mod works only in the hangar and gives no information or advantage in battle. Because it presses Battle automatically, ask Wargaming support or submit to the official Mod Hub before publishing.

## Building the mod
```
python2.7 mod/build.py          # -> mod/dist/stealthstart_<version>.wotmod
python2.7 mod/tests/test_mod.py
cd companion && python -m pytest test_companion.py
```
