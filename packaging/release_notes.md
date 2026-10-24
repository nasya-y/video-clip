## Video Compilation Maker 1.0.1 for Windows (64-bit)

Changes since 1.0.0: smaller download and installation (duplicate FFmpeg libraries and an unused Qt OpenGL library are no longer bundled). The program itself is unchanged.

Turns your local videos into 30-second highlight compilations automatically — completely offline.

**Downloads**

* `VideoCompilationMaker-Setup-1.0.1.exe` — installer (recommended). Installs for the current user, no administrator rights needed, adds Start-menu and optional desktop shortcuts, includes an uninstaller.
* `VideoCompilationMaker-1.0.1-portable-win64.zip` — portable version: unzip anywhere and run `VideoCompilationMaker.exe`.

Everything needed (Python runtime, Qt, OpenCV, FFmpeg 9.0) is bundled. No internet connection, account or API key is required, and no video ever leaves your computer.

The program is not code-signed, so Windows SmartScreen may show "Windows protected your PC" the first time: click **More info → Run anyway**.

This build was produced and tested automatically on a Windows runner: the full test suite, a self-test of the packaged program and a silent install / run / uninstall of the installer all passed before publishing. See the README for usage, troubleshooting and build instructions.
