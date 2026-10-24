## Video Compilation Maker 1.0.2 for Windows (64-bit)

**Changes since 1.0.2**

* **Camera shake is no longer mistaken for a scene cut.** Handheld wobble, walking and running footage, bumps and motion blur used to be detected as cuts, which chopped up the usable footage. The program now measures how the whole picture moved (shift, rotation, zoom) and treats a change as shake when undoing that movement gives the same picture and the movement looks physical (blur, spread over several frames, or the camera swinging back). Real jump cuts — time skips, reframes and zoom punch-ins, also inside shaky footage — are still found.
* **More compilations per video.** Fewer false cuts means more usable footage. In addition, a clip that fails verification no longer counts against a fixed limit of six re-plans per video (after which processing of the video stopped early); the program now keeps replacing failed clips until the footage is used up, and a check that fails for most clips at once no longer throws that footage away. Verification's independent cut re-check now looks at the footage at the same resolution and with the same lead-in as the main analysis, so the two agree.
* **Camcorder and other index-less files fixed.** In MPEG transport streams (camcorder `.MTS`/`.M2TS`, `.TS`) and program streams (`.MPG`/`.VOB`) seeking could land more than a second away from the planned clip. Verification caught this, but then set the "faulty" footage aside, so such files gave far fewer compilations than possible (often none). These files are now read through a temporary copy with a seek index (streams copied, nothing re-encoded), and every other file gets a quick seek check that triggers the same copy if needed.
* **"At most N possible".** The results screen and the report (`capacity`) now say how many compilations the whole video can give and why: usable footage after cuts and transitions, how many 3.9 s clips it holds, and any footage set aside by verification. The report also lists the camera shakes that were not counted as cuts.

Measured on synthetic test videos with known ground truth: a 2-minute walking/running video with room for exactly 3 compilations gave 2 with 1.0.1 (camera shake was taken for 30+ cuts) and gives 3 with 1.0.2; on 16 shaky test videos (24–60 fps) 1.0.2 finds every real cut and no false ones. The full test suite (146 tests) passes on Windows and Linux.

Analysis results cached by earlier versions are recomputed automatically.

Turns your local videos into 30-second highlight compilations automatically — completely offline.

**Downloads**

* `VideoCompilationMaker-Setup-1.0.2.exe` — installer (recommended). Installs for the current user, no administrator rights needed, adds Start-menu and optional desktop shortcuts, includes an uninstaller.
* `VideoCompilationMaker-1.0.2-portable-win64.zip` — portable version: unzip anywhere and run `VideoCompilationMaker.exe`.

Everything needed (Python runtime, Qt, OpenCV, FFmpeg 9.0) is bundled. No internet connection, account or API key is required, and no video ever leaves your computer.

The program is not code-signed, so Windows SmartScreen may show "Windows protected your PC" the first time: click **More info → Run anyway**.

This build was produced and tested automatically on a Windows runner: the full test suite, a self-test of the packaged program and a silent install / run / uninstall of the installer all passed before publishing. See the README for usage, troubleshooting and build instructions.
