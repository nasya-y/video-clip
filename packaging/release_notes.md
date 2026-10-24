## Video Compilation Maker 1.0.4 for Windows (64-bit)

**Changes since 1.0.3**

* **The watermark is optional.** Leave the watermark text empty and the compilations are made without a watermark; the Start button no longer requires a text. Every other check (no cuts inside clips, exact timing, audio sync, no footage reuse) still runs.

Changes in 1.0.3: louder sections are preferred when choosing clips (loudness and movement weigh equally), and a Glass effect button adds a fluted-glass look over the picture.

Changes in 1.0.2: camera shake is no longer taken for scene cuts; every possible compilation is made (failed clips are replaced instead of stopping the video, camcorder MTS/M2TS/TS/MPG files are read through a seekable copy); the results show the most compilations a video can give.

Analysis results cached by earlier versions are recomputed automatically.

Turns your local videos into 30-second highlight compilations automatically — completely offline.

**Downloads**

* `VideoCompilationMaker-Setup-1.0.4.exe` — installer (recommended). Installs for the current user, no administrator rights needed, adds Start-menu and optional desktop shortcuts, includes an uninstaller.
* `VideoCompilationMaker-1.0.4-portable-win64.zip` — portable version: unzip anywhere and run `VideoCompilationMaker.exe`.

Everything needed (Python runtime, Qt, OpenCV, FFmpeg 9.0) is bundled. No internet connection, account or API key is required, and no video ever leaves your computer.

The program is not code-signed, so Windows SmartScreen may show "Windows protected your PC" the first time: click **More info → Run anyway**.

This build was produced and tested automatically on a Windows runner: the full test suite, a self-test of the packaged program and a silent install / run / uninstall of the installer all passed before publishing. See the README for usage, troubleshooting and build instructions.
