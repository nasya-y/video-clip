# Third-party notices

Video Compilation Maker is released under the MIT License (see `LICENSE`).
The Windows package bundles the following third-party software. Each component keeps
its own licence; full licence texts are installed in `_internal\licenses` next to the
program (and are produced in `build/licenses` by `packaging/collect_licenses.py`).

| Component | Use in this app | Licence | Source code |
|---|---|---|---|
| FFmpeg (+ libx264 and the other libraries of the build) | decoding, encoding (H.264/AAC), filtering | GNU GPL v3 (this build includes GPL components such as x264) | https://github.com/FFmpeg/FFmpeg — build recipe and exact library versions: https://github.com/BtbN/FFmpeg-Builds (the bundled build is identified in `licenses/FFmpeg/BUILD_INFO.json`) |
| Qt 6 / Qt for Python (PySide6, Shiboken6) | graphical interface | GNU LGPL v3 | https://code.qt.io/cgit/pyside/pyside-setup.git/ , https://code.qt.io/cgit/qt/qtbase.git/ |
| OpenCV (opencv-python-headless) | frame analysis, optical flow, face detection | Apache License 2.0 (bundled third-party code listed in `LICENSE-3RD-PARTY.txt`) | https://github.com/opencv/opencv |
| NumPy | numerical processing | BSD 3-Clause | https://github.com/numpy/numpy |
| Pillow | drawing the watermark | MIT-CMU (HPND) | https://github.com/python-pillow/Pillow |
| Poppins Bold font | watermark lettering | SIL Open Font License 1.1 | https://github.com/itfoundry/Poppins |
| YuNet face detection model (`face_detection_yunet_2023mar.onnx`) | keeping the watermark away from faces | MIT | https://github.com/opencv/opencv_zoo |
| CPython runtime (bundled by PyInstaller) | runs the application | PSF License | https://github.com/python/cpython |

## Notes on the GPL / LGPL components

* FFmpeg is shipped as unmodified separate executables (`ffmpeg.exe`, `ffprobe.exe`) with
  their DLLs; the application starts them as external programs. The corresponding source
  code is available from the links above; the exact build used is recorded in
  `licenses/FFmpeg/BUILD_INFO.json`.
* The Qt / PySide6 libraries are shipped as separate, replaceable DLLs and Python modules,
  as required by the LGPL v3.
* No component has been modified.
