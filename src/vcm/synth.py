"""Synthetic test footage with known ground truth (cuts, jump cuts, dissolves, fades,
frozen and black sections, frame rates, resolutions, audio with sync beeps).

Used by the automated tests and by the packaged application's ``--self-test``.
"""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
import wave
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional, Sequence

import cv2
import numpy as np

from .ffmpeg_tools import ffmpeg, popen


@dataclass
class Shot:
    duration: float
    scene: int = 0
    camera: str = "pan"            # static | pan | fast_pan | zoom | shake | handheld
    sprites: int = 3
    transition: str = "cut"        # how this shot begins: cut | jump | dissolve | fade
    transition_duration: float = 0.0
    frozen: bool = False           # still picture (identical frames)
    black: bool = False            # black frames
    jump_offset: float = 0.10      # for jump cuts: camera offset as fraction of width
    jump_zoom: float = 1.0         # for jump cuts: punch-in zoom factor
    time_skip: float = 0.0         # for jump cuts: time skipped (subjects jump position)
    shake: str = ""                # realistic camera shake: "" | handheld | walking | running
    motion_blur: bool = False      # simulate a 180-degree shutter (blur of moving camera/subjects)


@dataclass
class GroundTruth:
    duration: float
    fps: float
    width: int
    height: int
    has_audio: bool
    cuts: list[float] = field(default_factory=list)            # instantaneous cuts (incl. jump cuts)
    transitions: list[tuple[float, float]] = field(default_factory=list)
    black: list[tuple[float, float]] = field(default_factory=list)
    frozen: list[tuple[float, float]] = field(default_factory=list)
    beeps: list[float] = field(default_factory=list)
    shot_starts: list[float] = field(default_factory=list)

    def forbidden(self) -> list[tuple[float, float]]:
        """Intervals (source seconds) that no clip may contain or straddle."""
        out = [(c, c) for c in self.cuts]
        out += list(self.transitions) + list(self.black) + list(self.frozen)
        return out


class _Scene:
    def __init__(self, seed: int, width: int, height: int):
        rng = np.random.default_rng(seed * 7919 + 11)
        self.W, self.H = int(width * 2.2), int(height * 2.2)
        base = np.zeros((self.H, self.W, 3), np.float32)
        hue = rng.uniform(0, 180)
        for octave, amp in ((4, 90.0), (12, 45.0), (40, 22.0)):
            small = rng.uniform(-1, 1, (octave, max(2, octave * self.W // self.H), 3)).astype(np.float32)
            base += amp * cv2.resize(small, (self.W, self.H), interpolation=cv2.INTER_CUBIC)
        tint = np.array(cv2.cvtColor(np.uint8([[[hue, 160, 200]]]), cv2.COLOR_HSV2BGR)[0, 0], np.float32)
        img = np.clip(base + tint * 0.75, 0, 255).astype(np.uint8)
        for _ in range(60):
            color = tuple(int(c) for c in rng.integers(0, 256, 3))
            kind = rng.integers(0, 3)
            x, y = int(rng.integers(0, self.W)), int(rng.integers(0, self.H))
            s = int(rng.integers(8, max(9, self.H // 6)))
            if kind == 0:
                cv2.circle(img, (x, y), s, color, -1, cv2.LINE_AA)
            elif kind == 1:
                cv2.rectangle(img, (x, y), (x + s, y + s // 2), color, -1, cv2.LINE_AA)
            else:
                cv2.line(img, (x, y), (x + s * 2, y + s), color, max(2, s // 8), cv2.LINE_AA)
        self.img = img
        self.rng = rng
        self.sprites = []
        for _ in range(8):
            self.sprites.append({
                "p": rng.uniform(0.15, 0.85, 2),
                "v": rng.uniform(-0.18, 0.18, 2),
                "r": rng.uniform(0.04, 0.09),
                "c": tuple(int(c) for c in rng.integers(40, 256, 3)),
            })
        self.cam = {"x": rng.uniform(0.1, 0.5), "y": rng.uniform(0.1, 0.5),
                    "vx": rng.choice([-1, 1]) * rng.uniform(0.02, 0.05),
                    "vy": rng.choice([-1, 1]) * rng.uniform(0.01, 0.03)}


def _bounce(v: float, lo: float = 0.0, hi: float = 0.5) -> float:
    """Fold v into [lo, hi] like a ball bouncing between walls (continuous)."""
    span = hi - lo
    v = (v - lo) % (2 * span)
    return lo + (v if v <= span else 2 * span - v)


class SyntheticVideo:
    def __init__(self, shots: Sequence[Shot], width: int = 480, height: int = 270,
                 fps: float | Fraction = 30, seed: int = 1):
        self.shots = list(shots)
        self.width, self.height = width, height
        self.fps = Fraction(fps).limit_denominator(1001)
        self.seed = seed
        self._scenes: dict[int, _Scene] = {}
        self.starts = []
        t = 0.0
        for s in self.shots:
            self.starts.append(t)
            t += s.duration
        self.duration = t
        noise_rng = np.random.default_rng(seed + 99)
        self._noise = [noise_rng.normal(0, 1.6, (height, width, 3)).astype(np.float32) for _ in range(7)]
        self._frozen_cache: dict[int, np.ndarray] = {}
        self._shake_cache: dict[int, dict] = {}

    def scene(self, sid: int) -> _Scene:
        if sid not in self._scenes:
            self._scenes[sid] = _Scene(self.seed * 1000 + sid, self.width, self.height)
        return self._scenes[sid]

    def _camera(self, shot_idx: int, local_t: float) -> tuple[float, float, float]:
        shot = self.shots[shot_idx]
        sc = self.scene(shot.scene)
        # jump cuts continue the camera of the same scene but displaced
        x, y = sc.cam["x"], sc.cam["y"]
        zoom = 1.0
        for j in range(shot_idx + 1):
            s = self.shots[j]
            if s.scene == shot.scene and s.transition == "jump" and j <= shot_idx:
                x += s.jump_offset
                zoom *= s.jump_zoom
        abs_t = self.starts[shot_idx] + local_t
        cam = shot.camera
        if cam == "static":
            dx = dy = 0.0
        elif cam == "pan":
            dx, dy = sc.cam["vx"] * abs_t, sc.cam["vy"] * abs_t * 0.5
        elif cam == "fast_pan":
            dx, dy = 3.0 * sc.cam["vx"] * abs_t, 0.5 * sc.cam["vy"] * abs_t
        elif cam == "zoom":
            dx, dy = 0.0, 0.0
            zoom *= 1.0 + 0.08 * local_t
        elif cam == "shake":
            # energetic handheld shake: several fast sinusoids (6-9 Hz)
            dx = 0.006 * math.sin(abs_t * 41.0) + 0.004 * math.sin(abs_t * 53.0 + 0.7)
            dy = 0.005 * math.sin(abs_t * 37.0 + 1.3) + 0.003 * math.sin(abs_t * 59.0)
        else:  # handheld: smooth wobble
            dx = 0.02 * math.sin(abs_t * 1.7) + 0.01 * math.sin(abs_t * 4.3)
            dy = 0.015 * math.sin(abs_t * 2.1 + 1)
        return _bounce(x + dx), _bounce(y + dy), zoom

    def _shake_params(self, idx: int) -> dict:
        """Per-shot random parameters of a realistic shake: wander, jitter and sudden jolts."""
        if idx in self._shake_cache:
            return self._shake_cache[idx]
        shot = self.shots[idx]
        rng = np.random.default_rng(self.seed * 7919 + idx * 31 + 5)
        kind = shot.shake
        if kind == "walking":
            p = {"bob": (0.004, 0.018), "bob_hz": rng.uniform(1.6, 2.0), "sway": 0.010, "sway_hz": rng.uniform(0.7, 1.0),
                 "jit": 0.0015, "rot": 0.8, "step_amp": (0.008, 0.022), "jolt_rate": 0.0, "perm": 0.0}
        elif kind == "running":
            p = {"bob": (0.008, 0.035), "bob_hz": rng.uniform(2.5, 3.0), "sway": 0.02, "sway_hz": rng.uniform(1.2, 1.5),
                 "jit": 0.004, "rot": 2.5, "step_amp": (0.02, 0.05), "jolt_rate": 1.0, "perm": 0.25}
        else:  # handheld
            p = {"bob": (0.008, 0.008), "bob_hz": rng.uniform(0.6, 0.9), "sway": 0.012, "sway_hz": rng.uniform(1.6, 2.2),
                 "jit": 0.0025, "rot": 0.6, "step_amp": (0.0, 0.0), "jolt_rate": 0.8, "perm": 0.3}
        p["phases"] = rng.uniform(0, 2 * math.pi, 8)
        jolts = []
        t = float(rng.uniform(0.2, 1.0))
        while p["jolt_rate"] > 0 and t < shot.duration:
            amp = rng.uniform(0.015, 0.06) * (1.6 if kind == "running" else 1.0)
            ang = rng.uniform(0, 2 * math.pi)
            jolts.append({"t0": self.starts[idx] + t, "ax": amp * math.cos(ang), "ay": amp * math.sin(ang),
                          "ar": rng.uniform(-2.5, 2.5), "rise": rng.uniform(0.012, 0.05),
                          "decay": rng.uniform(0.12, 0.6), "perm": rng.random() < p["perm"]})
            t += rng.exponential(1.0 / p["jolt_rate"]) + 0.25
        if p["step_amp"][1] > 0:  # footsteps: a jolt at every step
            period = 1.0 / p["bob_hz"]
            t = float(rng.uniform(0, period))
            while t < shot.duration:
                amp = rng.uniform(*p["step_amp"])
                jolts.append({"t0": self.starts[idx] + t, "ax": rng.normal(0, 0.2) * amp, "ay": amp,
                              "ar": rng.normal(0, p["rot"] / 2), "rise": rng.uniform(0.015, 0.04),
                              "decay": rng.uniform(0.08, 0.2), "perm": False})
                t += period * rng.uniform(0.9, 1.1)
        p["jolts"] = jolts
        self._shake_cache[idx] = p
        return p

    def _shake(self, idx: int, abs_t: float) -> tuple[float, float, float]:
        """(dx, dy) as fractions of the frame width and rotation in degrees."""
        p = self._shake_params(idx)
        ph = p["phases"]
        tau = 2 * math.pi
        dx = p["sway"] * math.sin(tau * p["sway_hz"] * abs_t + ph[0])
        dy = p["bob"][1] * math.sin(tau * p["bob_hz"] * abs_t + ph[1]) + p["bob"][0] * math.sin(tau * 0.37 * abs_t + ph[2])
        dx += p["jit"] * (math.sin(tau * 9.3 * abs_t + ph[3]) + 0.7 * math.sin(tau * 12.9 * abs_t + ph[4]))
        dy += p["jit"] * (math.sin(tau * 10.7 * abs_t + ph[5]) + 0.6 * math.sin(tau * 14.1 * abs_t + ph[6]))
        rot = p["rot"] * math.sin(tau * 0.8 * abs_t + ph[7])
        for j in p["jolts"]:
            u = abs_t - j["t0"]
            if u <= 0:
                continue
            rise = 1.0 - math.exp(-u / j["rise"])
            f = rise if j["perm"] else rise * math.exp(-u / j["decay"])
            dx += j["ax"] * f
            dy += j["ay"] * f
            rot += j["ar"] * f
        return dx, dy, rot

    def _render_shot(self, idx: int, local_t: float, frame_no: int) -> np.ndarray:
        shot = self.shots[idx]
        W, H = self.width, self.height
        if shot.black:
            return np.zeros((H, W, 3), np.uint8)
        if shot.frozen:
            if idx not in self._frozen_cache:
                self._frozen_cache[idx] = self._render_live(idx, 0.0)
            return self._frozen_cache[idx]
        if shot.motion_blur:
            # 180-degree shutter: average sub-frames over half a frame interval
            span = 0.5 / float(self.fps)
            subs = [self._render_live(idx, local_t + span * (k / 5.0 - 0.5)).astype(np.float32) for k in range(6)]
            img = np.mean(subs, axis=0)
        else:
            img = self._render_live(idx, local_t).astype(np.float32)
        img += self._noise[frame_no % len(self._noise)]
        return np.clip(img, 0, 255).astype(np.uint8)

    def _render_live(self, idx: int, local_t: float) -> np.ndarray:
        shot = self.shots[idx]
        sc = self.scene(shot.scene)
        W, H = self.width, self.height
        x, y, zoom = self._camera(idx, local_t)
        # crop window in the big scene image
        cw, ch = sc.W / 2.2 / zoom, sc.H / 2.2 / zoom
        ox, oy = x * (sc.W - cw), y * (sc.H - ch)
        M = np.array([[W / cw, 0, -ox * W / cw], [0, H / ch, -oy * H / ch]], np.float32)
        abs_t = self.starts[idx] + local_t
        frame = cv2.warpAffine(sc.img, M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        abs_t += sum(s.time_skip for s in self.shots[:idx + 1] if s.scene == shot.scene and s.transition == "jump")
        for k, sp in enumerate(sc.sprites[:shot.sprites]):
            p = sp["p"] + sp["v"] * abs_t
            p = np.abs(((p - 0.1) % 1.6) - 0.8) + 0.1  # bounce in [0.1, 0.9]
            cx, cy = int(p[0] * W), int(p[1] * H)
            rad = max(4, int(sp["r"] * min(W, H) * 1.6))
            cv2.circle(frame, (cx, cy), rad, sp["c"], -1, cv2.LINE_AA)
            cv2.circle(frame, (cx, cy), max(2, rad // 2), (255 - sp["c"][0], 255 - sp["c"][1], 255 - sp["c"][2]), -1, cv2.LINE_AA)
            cv2.line(frame, (cx - rad, cy), (cx + rad, cy), (20, 20, 20), max(1, rad // 6), cv2.LINE_AA)
        if shot.shake:
            # the shaking camera moves everything it films, background and subjects alike
            dx, dy, rot = self._shake(idx, self.starts[idx] + local_t)
            R = cv2.getRotationMatrix2D((W / 2, H / 2), rot, 1.0)
            R[0, 2] += dx * W
            R[1, 2] += dy * W
            frame = cv2.warpAffine(frame, R, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        return frame

    def shot_at(self, t: float) -> int:
        for i in range(len(self.shots) - 1, -1, -1):
            if t >= self.starts[i] - 1e-9:
                return i
        return 0

    def frame(self, t: float, frame_no: int) -> np.ndarray:
        i = self.shot_at(t)
        shot = self.shots[i]
        local = t - self.starts[i]
        cur = self._render_shot(i, local, frame_no)
        if i > 0 and shot.transition in ("dissolve", "fade") and local < shot.transition_duration:
            a = local / shot.transition_duration
            if shot.transition == "dissolve":
                prev = self._render_shot(i - 1, t - self.starts[i - 1], frame_no)
            else:
                prev = np.zeros_like(cur)
            return cv2.addWeighted(cur, a, prev, 1.0 - a, 0.0)
        return cur

    def ground_truth(self, has_audio: bool, beeps: list[float]) -> GroundTruth:
        gt = GroundTruth(duration=self.duration, fps=float(self.fps), width=self.width,
                         height=self.height, has_audio=has_audio, beeps=beeps)
        for i, s in enumerate(self.shots):
            st = self.starts[i]
            gt.shot_starts.append(st)
            if i > 0:
                if s.transition in ("cut", "jump"):
                    gt.cuts.append(st)
                else:
                    gt.transitions.append((st, st + s.transition_duration))
            if s.black:
                gt.black.append((st, st + s.duration))
            if s.frozen:
                gt.frozen.append((st, st + s.duration))
        return gt


def _write_wav(path: str, samples: np.ndarray, sr: int) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.repeat(pcm[:, None], 2, axis=1).tobytes())


def make_video(path: str, shots: Sequence[Shot], width: int = 480, height: int = 270,
               fps: float | Fraction = 30, audio: bool = True, vfr: bool = False,
               seed: int = 1, crf: int = 18, beep_interval: float = 0.71,
               container_args: Sequence[str] = (), sar: Optional[str] = None,
               rotation: int = 0) -> GroundTruth:
    """Render a synthetic video file and return its ground truth.

    ``sar`` (e.g. "4/3") marks the pixels as non-square, ``rotation`` adds a display
    rotation (like phone videos) without changing the stored pixels.
    """
    vid = SyntheticVideo(shots, width, height, fps, seed)
    fps_f = Fraction(fps).limit_denominator(1001)
    gen_fps = fps_f * 2 if vfr else fps_f
    n_frames = int(math.floor(vid.duration * float(gen_fps) + 1e-6))
    duration = n_frames / float(gen_fps)
    beeps: list[float] = []
    tmpdir = tempfile.mkdtemp(prefix="vcm-synth-")
    wav = os.path.join(tmpdir, "a.wav")
    sr = 48000
    if audio:
        t = np.arange(int(duration * sr)) / sr
        sig = np.zeros_like(t)
        for i, s in enumerate(vid.shots):
            st = vid.starts[i]
            m = (t >= st) & (t < st + s.duration)
            sig[m] = 0.05 * np.sin(2 * np.pi * (180 + 40 * (s.scene % 7)) * t[m])
        # irregular beep pattern so cross-correlation has a unique answer
        rng = np.random.default_rng(seed + 5)
        bt = 0.3
        while bt < duration - 0.1:
            beeps.append(round(bt, 4))
            m = (t >= bt) & (t < bt + 0.04)
            sig[m] += 0.6 * np.sin(2 * np.pi * 1000 * t[m])
            bt += beep_interval * rng.uniform(0.6, 1.4)
        _write_wav(wav, sig, sr)
    cmd = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}",
           "-framerate", f"{gen_fps.numerator}/{gen_fps.denominator}", "-i", "pipe:0"]
    if audio:
        cmd += ["-i", wav]
    vf = []
    if vfr:
        # drop an irregular subset of frames -> genuinely variable frame durations
        vf.append("select='not(eq(mod(n\\,7)\\,3))*not(eq(mod(n\\,5)\\,1))*not(eq(mod(n\\,3)\\,0))+lt(n\\,1)'")
    if sar:
        vf.append(f"setsar={sar}")
    if vf:
        cmd += ["-vf", ",".join(vf)]
    if vfr:
        cmd += ["-fps_mode", "vfr"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-g", str(int(round(float(fps_f) * 2)))]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-shortest"]
    encode_path = path + ".norot.mp4" if rotation else path
    cmd += list(container_args) + [encode_path]
    proc = popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for i in range(n_frames):
            tt = i / float(gen_fps)
            proc.stdin.write(vid.frame(tt, i).tobytes())
        proc.stdin.close()
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        rc = proc.wait()
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass
    if rc != 0:
        raise RuntimeError(f"synthetic video encode failed: {err}")
    if rotation:
        r = subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-display_rotation", str(rotation),
                            "-i", encode_path, "-c", "copy", path], capture_output=True)
        os.remove(encode_path)
        if r.returncode != 0:
            raise RuntimeError(f"rotation remux failed: {r.stderr.decode('utf-8', 'replace')}")
    return vid.ground_truth(audio, beeps)


def standard_shots(total: float, seed: int = 3, avg_shot: float = 7.0,
                   kinds: Sequence[str] = ("cut", "jump", "cut", "dissolve", "cut", "jump", "fade")) -> list[Shot]:
    """A realistic edited sequence: varied shots, hard cuts, jump cuts and dissolves."""
    rng = np.random.default_rng(seed)
    shots: list[Shot] = []
    t = 0.0
    scene = 0
    cams = ["pan", "handheld", "static", "zoom", "fast_pan", "shake"]
    k = 0
    while t < total - 1.0:
        dur = float(min(total - t, max(1.2, rng.normal(avg_shot, avg_shot * 0.45))))
        if shots:
            kind = kinds[k % len(kinds)]
            k += 1
        else:
            kind = "cut"
        if kind == "jump":
            shot = Shot(dur, scene=scene, camera="static" if rng.random() < 0.5 else "handheld",
                        transition="jump", jump_offset=float(rng.uniform(0.06, 0.12)),
                        jump_zoom=float(rng.choice([1.0, 1.12])), sprites=int(rng.integers(1, 4)),
                        time_skip=float(rng.choice([0.0, 1.2, 2.5])))
        else:
            scene += 1
            td = 0.6 if kind in ("dissolve", "fade") else 0.0
            shot = Shot(dur, scene=scene, camera=cams[scene % len(cams)], transition=kind,
                        transition_duration=td, sprites=int(rng.integers(1, 5)))
        shots.append(shot)
        t += dur
    return shots
