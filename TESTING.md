# Testing Stealth Start on your PC

Work through the stages in order. Each one isolates a single part, so if something
fails you know exactly where.

**Prerequisites:** Windows, World of Tanks (Wargaming), OBS 28+, Python 3.9+.

---

## Stage 1: OBS + companion (no game)

1. In OBS: **Tools → WebSocket Server Settings → Enable WebSocket server**. Note the port and password.
2. Optional but recommended: install Exeldro's **Freeze Filter** plugin, then restart OBS.
3. In a terminal:
   ```
   cd companion
   pip install -r requirements.txt
   python stealth_start_companion.py --selftest
   ```
   The first run creates `companion_config.json` and probably fails. Fill in `obs_password`,
   `game_capture_source` (your exact OBS source name) and `game_audio_source`, then run it again.
4. Start WoT (or any window your capture source shows) and run `--selftest` again.

**Expect:** the OBS preview freezes for 5 s, the game audio meter in OBS is muted, then both come back.
The log says `Freeze method: filter` (plugin found) or `screenshot` (fallback).

- [ ] Freeze works
- [ ] Audio source mutes and unmutes
- [ ] If the audio source was already muted before the test, it stays muted afterwards

## Stage 2: mod loads

1. Copy `mod/dist/stealthstart_0.1.0.wotmod` into `World_of_Tanks\mods\<game version>\` (Steam install: `C:\Program Files (x86)\Steam\steamapps\common\World of Tanks\eu\mods\2.4.1.1\`)
   (the folder named like `2.4.1.1`).
2. Start the game and reach the hangar. Close it again.
3. Open `World_of_Tanks\game.log` (newer clients log Python there, tagged `[SCRIPT]`) and search for `[Stealth Start]`.

**Expect:** `[Stealth Start] loaded v0.1.0, hotkey=Ctrl+KEY_0, delay=1-60s`, and the config file now
exists at `World_of_Tanks\mods\configs\stealthstart\config.json`.

- [ ] Loaded line present, no Python traceback near it

## Stage 3: fail closed (companion off)

Companion **not** running. In the hangar, Random Battles selected, click into the game window and press **Ctrl+0**.

**Expect:** within about 4 s, an error notification that the OBS companion isn't responding. No queue.

- [ ] Error shown, nothing queued
- [ ] Typing a plain `0` in the chat box does not trigger the mod

## Stage 4: freeze and cancel (no battle)

Start the companion normally: `python stealth_start_companion.py`. Press **Ctrl+0**, then press it
again within a few seconds.

**Expect:** OBS freezes at the first press, a notification shows the rolled delay, interface sounds
go quiet; the second press cancels, OBS unfreezes and the sound returns.

- [ ] Freeze on press, unfreeze on cancel
- [ ] Interface sounds muted while frozen (click a few hangar buttons)
- [ ] Switching tank during the wait cancels and unfreezes

## Stage 5: queue, then leave

For quick runs, set `"delay_min_seconds": 3` and `"delay_max_seconds": 6` in the mod config and restart
the game. Press **Ctrl+0** and wait.

**Expect:** after the delay the queue window appears in the game (OBS still frozen). Press Ctrl+0 again
(or Exit in the queue window): you leave the queue, OBS unfreezes.

- [ ] Queue starts after the delay
- [ ] Leaving the queue unfreezes
- [ ] **Listen to the stream output** (OBS monitor or a test recording): is any sound audible at the
      moment the queue starts? Note which one.

## Stage 6: full run

Press **Ctrl+0** and let it find a battle.

**Expect:** OBS stays frozen until the battle loading screen, then unfreezes and sound returns.

- [ ] Unfreezes at the loading screen, not earlier
- [ ] No sound on the stream when the battle is found
- [ ] After the battle, back in the hangar, everything is normal (sound volume, Battle button)

## Stage 7: platoon (optional)

As platoon commander with a ready platoon mate, repeat Stage 6. As a platoon member, the hotkey should
show "only the platoon commander can use the hotkey".

- [ ] Commander works
- [ ] Member is refused

---

## When something goes wrong

Send me:
- the companion's terminal output
- the `[Stealth Start]` lines and any traceback from `World_of_Tanks\game.log`
- which stage and step failed
