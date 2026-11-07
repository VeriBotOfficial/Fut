# Fut V1.2 — Stable Release Notes

> **Project ownership and upstream attribution**
>
> Fut is maintained by Doruk Çavdar. This project is a permitted modification of work by 73@**7othree**; subsequent upstream maintenance is attributed to maxlzyver.

1. **Preloaded non-standard player cards (BT, ST, and others).** One copy of each of the 6,328 confirmed non-standard variants is preloaded into the Players list. Players are fully levelled, receive automatic progression, and can be viewed, trained, and selected for matches.

2. **Standard cards are not included in the preload.** The preload is limited to the confirmed card types.

3. **Training respects each card's level cap.** Cards originally capped at level 1 keep that cap. Other players are trained to their individual maximum levels and receive recommended progression allocations.

4. **Training plans remain editable.** You can adjust progression points in the game's existing player-training screen to suit your play style.

5. **Preloaded players are not granted more than once.** Restarting or updating the game does not duplicate the same player grants. Players released from the preloaded group are not automatically added again.

6. **Existing player data is preserved.** Saved training, additional skills, and personal statistics remain intact.

7. **Selectable standard and ST skills.** Open Player Training → Skill Training and use a skill-selection item to choose a skill.

8. **The selection list contains 63 approved skills.** It uses the game's existing categories, descriptions, and confirmation screens. Skills a player already has are not added again.

9. **Fixed additional-skill slot assignment.** Each of the five slots is saved independently, preventing consecutive training operations from overwriting another slot.

10. **Fixed skills disappearing after restart.** Successfully saved additional skills remain after exiting, force-closing, and reopening the game.

11. **Empty slots can be trained independently.** An empty earlier slot no longer blocks selection and saving in a later slot.

12. **Additional skills can be removed.** After removal, slots are rearranged and saved according to the game's original rules. Different copies of the same card can have different skill combinations.

13. **Fixed level and progression-point displays after upgrades.** Addresses incorrect displays such as “0/100” and “Progression points -2” that previously required a restart to correct.

14. **Fixed player playing styles in matches.** Addresses players incorrectly appearing with “No Style” after entering a match. Playing styles still only take effect in positions where they apply.

15. **Corrected preferred-foot mapping.** Fixes cases where left-footed players appeared differently in matches and player details.

16. **Corrected weak-foot accuracy and usage frequency.** Values are read from each individual card rather than being uniformly set to maximum.

17. **Fixed the Through Passing effect in matches.** Restores the effect that previously failed to activate during matches.

18. **Player training and skills carry into matches.** Match startup reads data from the selected player instance. Cards with the same name, including separate copies of the same card, retain their own data.

19. **Fixed normally completed matches being recorded as forfeits.** AI-division results are settled using the actual completion state and score. Matches genuinely abandoned midway are still treated as forfeits.

20. **Added personal match statistics.** Appearances, goals, and assists are recorded after a match is completed normally.

21. **Appearances reflect actual time on the pitch.** Starting players and substitutes who enter the match count as appearances. Unused substitutes do not.

22. **Personal statistics persist across restarts.** Submitting the same match result more than once does not duplicate totals, and updates do not deliberately clear saved records.

23. **Fixed My League startup hangs.** Starting a match consistently loads the squad, manager, and tactics saved for the current season.

24. **Saved a dedicated My League squad.** Players, managers, and tactics changed and saved in My League remain available when you return.

25. **Improved My League scores and standings.** Results are saved after completed matches. A placeholder 0–0 is not treated as a played match if the match was never entered.

26. **Added My League goal and assist leaderboards.** Both leaderboards include user-controlled and AI players.

27. **Real matches use actual match events.** Goals and assists for your players and the AI opponent in the same match are read from the match events.

28. **Other AI fixtures generate player statistics.** Simulated matches between other league teams distribute goals and assists based on the relevant squads and scores.

29. **Completed all participating team rosters.** All 441 teams in the current competition are covered, including Fluminense, Remo, and American League 2 teams.

30. **Fixed byes in leagues with an odd number of teams.** During a bye, the game prepares the next actual fixture without inventing a draw, appearance, points, or rewards for your team.

31. **Fixed released players reappearing.** Releasing a player affects only the selected player instance. Old grant records no longer add that player again after a restart.

32. **A new match can be prepared after leaving an old one.** An unfinished previous match no longer locks the next squad and opponent indefinitely. Saved historical statistics remain intact.

33. **Added a 120 FPS option.** The existing frame-rate settings retain their other options, so you can choose the setting that suits your device.

34. **The 120 FPS setting is saved.** Actual frame rate depends on device performance, display refresh rate, and system scheduling. Not every device can sustain 120 FPS.

35. **New features use the game's native interface.** Skill training, progression allocation, and frame-rate settings are accessed through the game's existing screens.

36. **Announcements are now opened manually.** Tap the notification icon in the top-right corner of the game to read updates. Announcements no longer repeatedly appear as automatic pop-ups.

37. **Unread announcements are marked.** Read status is saved, and the project ownership and upstream attribution notice stays pinned.

38. **Improved the mobile update screen.** Uses a blue-and-white palette, larger text, and a scrollable layout to show the project address, download information, and support details.

39. **Automatic retries for failed downloads.** Temporary network loss, timeouts, interrupted transfers, and expired GitHub temporary links trigger retries while preserving progress.

40. **Resumable downloads and resource reuse.** Complete resources from the same version are verified and reused. Only missing or incomplete files need to be downloaded again.

41. **Offline play after resource setup.** The first launch downloads and prepares the required resources automatically. You do not need to select, move, or extract resource packages manually.

42. **Improved handling of second-launch errors.** Addresses reported local startup conflicts while retaining save protection and detailed error information.

43. **Install over the previous stable release.** Follow Android's prompts to install V1.2 over the existing stable app. Existing stable data is preserved. **Do not uninstall the old app or clear its data first.**

44. **Supports ARM64 devices running Android 12 or later.** Actual performance varies by device.

45. **Some original FP card artwork from the Chinese client is not displayed yet.** This includes previously reported cards such as Martin Ødegaard and Radamel Falcao. The players themselves remain available; the corresponding display assets have not yet been added.

46. **Blitz Curler and Long Reach Tackle are not yet available to obtain.** Unverified placeholder skills are not exposed as completed skills. The Through Passing effect can be used in matches, but this does not mean it is available through additional skill training.

47. **A small number of device-specific issues are still being investigated.** These include crashes when opening certain packs, a black screen on the first online launch, immediate refreshes on combination-pack purchase screens, some team kits, and certain skill-trigger issues.

48. **Data that was never saved by an older version cannot be reconstructed.** Historical match goals and assists that were never recorded, and skills that were overwritten without a saved record, cannot be reliably restored.

49. **Download network information.** Game resources are hosted on GitHub. If downloads are slow or fail, try a stable network or proxy. Existing progress is retained whenever possible.

50. **Support:** [GitHub Issues](https://github.com/VeriBotOfficial/Fut/issues). Include your device model, Android version, game version, reproduction steps, and screenshots or a screen recording. If startup fails, provide the complete error text. Keep the app and save data installed.

51. **Official project:** [github.com/VeriBotOfficial/Fut](https://github.com/VeriBotOfficial/Fut).
