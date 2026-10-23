"""GUI (offscreen Qt) and command-line entry points."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _wait(app, predicate, timeout=300.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from vcm.gui.app import make_app
    return make_app()


@pytest.mark.gui
def test_window_shows_required_defaults(qapp, tmp_path):
    from vcm.gui.main_window import MainWindow
    from vcm.settings import save_settings, Settings
    save_settings(Settings())
    w = MainWindow()
    assert w.sp_speed.value() == pytest.approx(1.3)
    assert w.sp_clip.value() == pytest.approx(3.0)
    assert w.sp_count.value() == 10
    assert w.ed_text.text() == "onlyfans.com/nyraishtara"
    assert w.cb_colour.currentData() == "auto"
    assert w.cb_quality.currentData() == "high"
    assert w.lbl_total.text().startswith("30.0 s")
    assert not w.btn_start.isEnabled()  # nothing selected yet
    assert not w.btn_cancel.isEnabled()
    w.close()


@pytest.mark.gui
def test_settings_and_folders_are_remembered(qapp, tmp_path, one_comp_video):
    from vcm.gui.main_window import MainWindow
    w = MainWindow()
    w.sp_speed.setValue(1.5)
    w.sp_count.setValue(8)
    w.out_edit.setText(str(tmp_path))
    w.add_paths([one_comp_video[0]])
    w.close()
    w2 = MainWindow()
    assert w2.sp_speed.value() == pytest.approx(1.5)
    assert w2.sp_count.value() == 8
    assert w2.out_edit.text() == str(tmp_path)
    assert w2.input_paths() == [one_comp_video[0]]
    w2.on_defaults()
    assert w2.sp_speed.value() == pytest.approx(1.3) and w2.sp_count.value() == 10
    assert w2.out_edit.text() == str(tmp_path), "restoring defaults keeps the folders"
    w2.close()


@pytest.mark.gui
def test_full_run_from_the_window(qapp, tmp_path, one_comp_video):
    from vcm.gui.main_window import MainWindow
    w = MainWindow()
    w.on_defaults()
    w.on_clear()
    w.add_paths([one_comp_video[0]])
    w.out_edit.setText(str(tmp_path / "gui out"))
    w.show()
    assert w.btn_start.isEnabled()
    w.on_start()
    assert w.btn_cancel.isEnabled() and not w.btn_start.isEnabled()
    assert _wait(qapp, lambda: w.thread is None, 600), "processing did not finish"
    assert w.results.count() == 1
    assert "Created 1 compilation" in w.lbl_results.text()
    assert w.progress.value() == 1000
    assert w.btn_preview.isEnabled() and w.btn_open_out.isEnabled()
    assert Path(w.results.item(0).data(256)).exists()  # Qt.UserRole == 256
    w.grab().save(str(tmp_path / "window_after_run.png"))
    w.close()


@pytest.mark.gui
def test_cancel_from_the_window(qapp, tmp_path, multi_comp_video):
    from vcm.gui.main_window import MainWindow
    w = MainWindow()
    w.on_clear()
    w.add_paths([multi_comp_video[0]])
    w.out_edit.setText(str(tmp_path))
    w.on_start()
    assert _wait(qapp, lambda: "rendering" in w.lbl_stage.text() or w.thread is None, 300)
    w.on_cancel()
    assert _wait(qapp, lambda: w.thread is None, 120), "cancel did not stop the worker"
    assert "Cancelled" in w.lbl_stage.text()
    assert w.btn_start.isEnabled()
    w.close()


def _run_cli(*args, timeout=900):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "vcm", *args], capture_output=True, text=True, timeout=timeout,
                          env=env, cwd=str(ROOT))


def test_cli_version():
    r = _run_cli("--version")
    assert r.returncode == 0 and "Video Compilation Maker" in r.stdout


def test_cli_batch_processing(one_comp_video, tmp_path):
    r = _run_cli("--cli", "--defaults", "-i", one_comp_video[0], "-o", str(tmp_path))
    assert r.returncode == 0, r.stderr[-2000:]
    assert (tmp_path / Path(one_comp_video[0]).stem / "Compilation 1.mp4").exists()


@pytest.mark.slow
def test_self_test_command(tmp_path):
    r = _run_cli("--self-test", str(tmp_path), timeout=1800)
    result = json.loads((tmp_path / "selftest_result.json").read_text(encoding="utf-8"))
    assert r.returncode == 0 and result["passed"], json.dumps(result, indent=1)[:3000]
