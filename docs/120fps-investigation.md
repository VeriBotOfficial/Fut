# Historical test.1/test.2 investigation

The floating JNI-based frame-rate interface described below was removed in
test.3. Current implementation adds 120 to the existing native settings menu;
see [native FPS menu implementation and validation](native-fps-menu.md).
Do not use the old overlay instructions as the current player entry point.

# 120 FPS experiment: evidence and boundary

The supplied `D:/ef/Unlock 120 FPS(1).zip` (1,131 bytes) has SHA-256
`4dc3c40b622490fb13348d3022807aa3296f0b059421cc2c2694f98e8f42c63e`.
REA's archive inspection is `ev_42f5b4455a058e008e060e89b8f072913a0da4d36b656e6028d21f66ce9f93fd`;
inventory is `ev_8259d8e02ba46a8ceec74f0410e52fb5ba452c06714b0e2edf4da4cfc28b09b8`.
There is exactly one regular member, 73-byte
`UE4Game/PesMobile/PesMobile/Saved/Config/Android/Engine.ini`, SHA-256
`fe236176260acf2d45f48257f2da8bd93299817f5a42bf2e59480b1441606716`:

```ini
[/Script/Engine.Engine]
bUseFixedFrameRate=True
FixedFrameRate=120.000000
```

No script or binary was executed. These are fixed-frame engine settings, not
proof of 120 presented frames per second. Epic describes manually setting
`UEngine::bUseFixedFrameRate` / `FixedFrameRate` as fixed tick mode in its
[Network Prediction API](https://dev.epicgames.com/documentation/en-us/unreal-engine/API/Plugins/NetworkPrediction/FNetworkPredictionSettings/bForceEngineFixT-?application_version=5.5).
The API citation establishes the upstream setting's nature, not this modified
game's runtime behavior. The experiment does **not** enable fixed timestep:
changing simulation timing would need a separate game-speed validation.

The published app declares UE 4.26.0. The existing GameActivity smali has
`public native nativeConsoleCommand(String)`. REA method inspection
`ev_d15f77e51c2c99c9d2f220f7b51df113501ed4ba1d26b5666d6c80f277d56309`
(`build/rea-native-console.json`) confirms the unique method declaration in
the code-inspection APK, SHA-256
`abbf810948c114eec96e701de389ca84d66afe4997b7f0c143f0e677559dd258`.
It reports `body_status=not_available` because this is a native method: it does
not establish command acceptance or the actual native implementation.
The full published APK's `libUE4.so` contains its
JNI symbol, `t.MaxFPS %u` at file offset 12,669,978, and UTF-16 `r.SetFramePace`
at 9,074,554. Epic's [UE 4.27 mobile frame pacing guide](https://dev.epicgames.com/documentation/en-us/unreal-engine/frame-pacing-for-mobile-devices?application_version=4.27)
documents `r.SetFramePace N` for UE 4.25 and later. This makes it a concrete
candidate engine request, not a guarantee that this shipping build accepts it
or keeps it after game scene changes.

`AndroidThunkJava_SetNativeDisplayRefreshRate` only chooses a same-resolution
Display.Mode; its Java runnable changes `preferredDisplayModeId`. It is a
display-Hz setting and cannot itself unlock engine FPS. The experiment sends
both `t.MaxFPS N` and `r.SetFramePace N` through the existing game JNI method,
then requests the matching display mode when available. The 120 option is
refused before sending commands if the current resolution has no 120 Hz mode.

No `Saved/Config/Android/Engine.ini` was found in either the supplied original
resource ZIP or the 1.1.1 APK. GameActivity has an observed UE4CommandLine reader,
but the exact Engine.ini consumption path has not been traced. Consequently
this implementation does not blindly write Engine.ini to one or both app data
directories. It also does not edit native code, game physics, or resource ZIPs.

Integration: call `HighFrameRate.show(activity)` from the game settings panel,
and `HighFrameRate.apply(activity)` when an Activity resumes. Only actual
`com.epicgames.ue4.GameActivity` instances may invoke native commands. Choices
are original (no commands), requested 60, and requested 120 (experimental).
The setting is app-private. Returning to original removes the request and
requires completely exiting/restarting the game, so native startup restores
its own defaults; `t.MaxFPS 0` is never misrepresented as a saved original value.
The code does not persist modified engine config files.

UI and logcat report request dispatch, never an achieved FPS. Acceptance must
be measured on a phone during real matches, including AI division and My
League, substitutions, half-time, scene changes and a restart. Compare actual
game-surface presentation frame times (about 8.33 ms for 120 FPS) with a 60 FPS
baseline and check elapsed match time/animation speed. Android display Hz,
Choreographer callbacks for the Java overlay, and a void JNI call returning
success are insufficient evidence. If native scenes reset the request, or the
device cannot render that quickly, report that limitation; do not add an
unbounded command loop or claim 120 FPS is verified.
