"""General text recognizer: PP-OCRv4 mobile (Apache-2.0) via onnxruntime, recognition stage only.

The scoreboard layout already says where text is, so the text-detection model is not needed.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata
from functools import cached_property
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

MODEL = Path(__file__).parent / "assets" / "ch_PP-OCRv4_rec_infer.onnx"
MODEL_SHA256 = "48fc40f24f6d2a207a2b1091d3437eb3cc3eb6b676dc3ef9c37384005483683b"
REC_HEIGHT = 48
MAX_WIDTH = 960

HEROES = [
    "Infernus",
    "Seven",
    "Vindicta",
    "Lady Geist",
    "Abrams",
    "Wraith",
    "McGinnis",
    "Paradox",
    "Dynamo",
    "Kelvin",
    "Haze",
    "Holliday",
    "Bebop",
    "Calico",
    "Grey Talon",
    "Mo & Krill",
    "Shiv",
    "Ivy",
    "Warden",
    "Yamato",
    "Lash",
    "Viscous",
    "Pocket",
    "Mirage",
    "Vyper",
    "Sinclair",
    "Mina",
    "Drifter",
    "Venator",
    "Victor",
    "Paige",
    "The Doorman",
    "Billy",
    "Graves",
    "Apollo",
    "Deadman Danny",
    "Rem",
    "Silver",
    "Celeste",
    "Rat King",
    "Solomon",
    "Violet",
    "Nurse Harrow",
    "Baba",
]
_HERO_KEYS = {re.sub(r"[^a-z&]", "", h.lower()): h for h in HEROES}


def snap_hero(text: str) -> tuple[str | None, float]:
    """Closest roster hero and its similarity (0..1). None when nothing is close."""
    key = re.sub(r"[^a-z&]", "", text.lower())
    match = difflib.get_close_matches(key, _HERO_KEYS, n=1, cutoff=0.6)
    if not match:
        return None, 0.0
    return _HERO_KEYS[match[0]], difflib.SequenceMatcher(None, key, match[0]).ratio()


class Recognizer:
    def __init__(self, threads: int = 1):
        model = MODEL.read_bytes()
        if hashlib.sha256(model).hexdigest() != MODEL_SHA256:
            raise RuntimeError(f"{MODEL} does not match its pinned SHA-256")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3
        self.session = ort.InferenceSession(
            model, sess_options=opts, providers=["CPUExecutionProvider"]
        )

    @cached_property
    def charset(self) -> list[str]:
        meta = self.session.get_modelmeta().custom_metadata_map
        # Output index 0 is the CTC blank and the last index is a space.
        return ["", *meta["character"].splitlines(), " "]

    def read(self, rgb: np.ndarray) -> tuple[str, float]:
        """Read one text line from an RGB crop. Returns (text, mean character confidence)."""
        h, w = rgb.shape[:2]
        if h == 0 or w == 0:
            return "", 0.0
        nw = min(MAX_WIDTH, max(REC_HEIGHT, round(w * REC_HEIGHT / h)))
        img = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8)).resize(
            (nw, REC_HEIGHT), Image.LANCZOS
        )
        # The model was trained on BGR input scaled to -1..1.
        x = (np.asarray(img, dtype=np.float32)[:, :, ::-1] / 127.5 - 1.0).transpose(2, 0, 1)[None]
        name = self.session.get_inputs()[0].name
        probs = self.session.run(None, {name: x})[0][0]
        ids = probs.argmax(axis=1)
        scores = probs.max(axis=1)
        keep = np.flatnonzero((ids != 0) & np.concatenate(([True], ids[1:] != ids[:-1])))
        if keep.size == 0:
            return "", 0.0
        text = "".join(self.charset[i] for i in ids[keep])
        return unicodedata.normalize("NFKC", text).strip(), float(scores[keep].mean())
