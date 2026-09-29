"""
A second service, to prove the plugin point is real.

It exists to answer "can I add a service without touching the broker or the worker?"
-- and the answer has to be yes or the abstraction is decorative. Nothing outside this
file was changed to add it: the worker discovered it by importing the package, and
the broker learned its parameters from the schema below at registration.

It is also deliberately self-contained. Adding a service has no minimum footprint: no
external tool, no dataset, no network. A service that needs a 250 MB model is equally
fine -- that is a deployment concern, not a framework one.

What it does: draws n cards from a deck and writes a reading as a text file. Trivial
on purpose -- the interesting part is the plumbing, not the fortune telling.
"""

from __future__ import annotations

import random
from pathlib import Path

from worker_plugins.base import ProgressFn

MAJOR_ARCANA = [
    "The Fool", "The Magician", "The High Priestess", "The Empress",
    "The Emperor", "The Hierophant", "The Lovers", "The Chariot", "Strength",
    "The Hermit", "Wheel of Fortune", "Justice", "The Hanged Man", "Death",
    "Temperance", "The Devil", "The Tower", "The Star", "The Moon", "The Sun",
    "Judgement", "The World",
]


class TarotDraw:
    name = "tarot_draw"
    title = "Tarot draw"

    # The broker validates submissions against this and the frontend renders a form
    # from it, so this dict is the ONLY place this service's parameters are declared.
    param_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string", "minLength": 1, "maxLength": 500,
                "title": "Question",
                "description": "What the reading is about.",
            },
            "cards": {
                "type": "integer", "minimum": 1, "maximum": 10, "default": 3,
                "title": "Cards to draw",
            },
            "reversed": {
                "type": "boolean", "default": True,
                "title": "Allow reversed cards",
            },
            "seed": {
                "type": ["integer", "null"], "default": None,
                "title": "Seed",
                "description": "Set for a reproducible reading.",
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    }

    artifact_kinds = ("reading_txt",)

    def run(self, params: dict, workdir: Path, outdir: Path,
            progress: ProgressFn) -> list[dict]:
        question = params["question"]
        n = int(params.get("cards", 3))
        allow_reversed = bool(params.get("reversed", True))
        seed = params.get("seed")

        progress(30, f"shuffling for {n} card(s)")
        rng = random.Random(seed)
        deck = list(MAJOR_ARCANA)
        rng.shuffle(deck)
        drawn = deck[:n]

        progress(70, "writing the reading")
        lines = [f"Question: {question}", ""]
        for i, card in enumerate(drawn, 1):
            if allow_reversed and rng.random() < 0.5:
                lines.append(f"{i}. {card} (reversed)")
            else:
                lines.append(f"{i}. {card}")
        if seed is not None:
            lines += ["", f"(seed {seed})"]

        out = outdir / "reading.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")

        progress(95, "done")
        return [{"kind": "reading_txt", "path": out}]

    def fake_run(self, params: dict, workdir: Path, outdir: Path,
                 progress: ProgressFn) -> list[dict]:
        progress(50, "fake tarot draw")
        out = outdir / "reading.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("fake reading\n", encoding="utf-8")
        return [{"kind": "reading_txt", "path": out}]


def plugin(**kwargs) -> TarotDraw:
    # accepts and ignores the loader's kwargs (toolkit, extra_args) -- a service with
    # no external dependencies has nothing to configure
    return TarotDraw()
