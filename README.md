# Fut V1.2

> **Project ownership and upstream attribution**
>
> Fut is maintained by Doruk Çavdar. This project is a permitted modification of work by 73@**7othree**; subsequent upstream maintenance is attributed to maxlzyver.

[Fut source repository](https://github.com/VeriBotOfficial/Fut) · [Full release notes](release-notes.md)

After installing the APK, Fut automatically prepares the required game resources. You do not need to select, move, or extract resource packages manually. Once setup is complete, the game can be played offline. Supports ARM64 devices running Android 12 or later.

## Updating an existing stable installation

If you already have the stable version installed, download V1.2 and follow Android's prompts to install it over the existing app. **Do not uninstall the app or clear its data first.** This release retains the previous stable package name and signing identity, preserving existing saves. Fully installed resources for the same game version are verified and reused.

The test build is a separate app, and its saves are not automatically transferred to the stable release. An original APK signed with a different key cannot be used as an in-place update for this release.

## V1.2 highlights

- Preloads one copy of each of the 6,328 confirmed non-standard player-card variants (including BT and ST cards) into the Players list. Players are fully levelled and receive automatic progression; cards capped at level 1 retain their original cap.
- Adds selectable standard and ST skills through the game's existing Skill Training screen. Fixes the five-slot assignment issue and skills disappearing after restart.
- Fixes player playing styles, preferred foot, weak-foot settings, and the Through Passing effect. Corrects level and progression-point displays after upgrades.
- Fixes AI-division matches being incorrectly recorded as forfeits after normal completion. Tracks appearances, goals, and assists and preserves the statistics after restart.
- Fixes My League startup hangs and saves its squad, manager, and tactics. Adds goal and assist leaderboards containing both user and AI players across all 441 participating teams.
- Adds a 120 FPS option to the existing frame-rate settings while retaining the other options. Release announcements are available from the notification icon in the top-right corner.
- Automatically retries interrupted or timed-out downloads and expired temporary links. Supports resumable downloads and reuse of verified resources.

Some original FP card artwork from the Chinese client is not yet displayed, and Blitz Curler and Long Reach Tackle are not yet available to obtain. A small number of device-specific issues, including pack-opening crashes and a black screen on first online launch, are still being investigated. See the release notes for the full list.

## Downloads and support

Game resources are hosted on GitHub. If downloads are slow or fail, try a stable network or proxy. An internet connection is required for the initial resource setup; existing download progress is retained whenever possible.

For support, open [GitHub Issues](https://github.com/VeriBotOfficial/Fut/issues). Include your device model, Android version, game version, reproduction steps, and screenshots or a screen recording. If startup fails, include the complete error message. Keep the app and its save data installed while troubleshooting.

## Build and maintenance

The stable release is built with `tools/build_stable_apk.py`, using version name 1.2 and build revision 1206. It is built from the verified stable 1.1.1 APK, preserving the app identity and incorporating the features validated in test.6. Older build scripts and test.* documents are historical development records.

Native changes remain limited to the skill-selection candidates and frame-rate menu validated in test.6. Original game DEX files 1–6, other native libraries, and resource tables are preserved. Signing, alignment, file hashes, comparison with test.6, and save migration results are documented in [V1.2 verification](docs/stable-v1.2-verification.md) and the release assets.

APK update checks use an incrementing `buildRevision`; the game resource dependency `versionCode=311010101` remains unchanged. Android still handles the installation confirmation. See the [maintenance guide](docs/resource-updates.md) for the resource and APK publication sequence. The update channel is switched last; do not delete historical resource assets.

The original game APK, large game resources, signing keys, passwords, temporary saves, and build outputs are not stored in this source repository.
