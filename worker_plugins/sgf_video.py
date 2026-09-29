"""
The SGF video render service -- the pipeline this system was built for, expressed as
a plugin.

It is the reference implementation of the contract: everything the old worker.py
hard-coded (which script to run, which flags to pass, which file extensions are
which artifact kind) now lives here instead, so the worker itself is service-agnostic.

The `param_schema` below is the single description of this service's parameters. The
broker validates submissions against it and the frontend builds the form from it, so
adding a field means editing this dict and nothing else.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from worker_plugins.base import ProgressFn

# Default toolkit location; overridable per worker with --toolkit.
DEFAULT_TOOLKIT = "/home/wei/playground/goGames/selfCommentary"

PARAM_SCHEMA = {
    "type": "object",
    "properties": {
        # Consumes an uploaded file. `format: "files"` is a RENDERING HINT -- the
        # frontend draws a file picker for it, and this is the one field whose value
        # travels as a multipart part instead of inside the params JSON. Declaring it
        # here means the form knows where files go without the frontend knowing
        # anything about this service.
        "sources": {
            "type": "array",
            "format": "files",
            "title": "Game record (.sgf)",
            "description": "The SGF file to render.",
        },
        "duration_s": {
            "type": "number", "minimum": 10, "maximum": 480, "default": 180,
            "title": "Duration (seconds)",
            "description": "Length of the generated soundtrack, and therefore of "
                           "the video. 480s is the music model's limit.",
        },
        "visits": {
            "type": "integer", "minimum": 50, "maximum": 100000, "default": 300,
            "title": "KataGo visits per position",
            "description": "Higher is slower and more accurate.",
        },
        "bpm": {
            "type": "number", "minimum": 20, "maximum": 400, "default": 69,
            "title": "Tempo",
        },
        "musical_key": {
            "type": "string", "maxLength": 32, "default": "D minor",
            "title": "Key",
        },
        "time_sig": {
            "type": "string", "enum": ["2", "3", "4", "6"], "default": "3",
            "title": "Time signature",
        },
        "seed": {
            "type": ["string", "null"], "maxLength": 32, "default": None,
            "title": "Seed",
            "description": "Blank for a random seed.",
        },
        "formats": {
            "type": "string", "maxLength": 64, "default": "mp4,webp",
            "title": "Output formats",
        },
    },
    "required": ["duration_s", "sources"],
    "additionalProperties": False,
}

# which output file becomes which artifact kind. Explicit rather than derived from
# the extension so a service controls its own vocabulary.
SUFFIX_KINDS = {
    ".mp4": "mp4",
    ".webp": "webp",
    ".gif": "gif",
    ".mp3": "mp3",
    ".html": "programme_html",
    ".svg": "phases_svg",
}


class SgfVideo:
    name = "sgf_video"
    title = "SGF video render"
    param_schema = PARAM_SCHEMA
    artifact_kinds = ("mp4", "webp", "mp3", "programme_html", "phases_svg")

    def __init__(self, toolkit: str | Path | None = None, extra_args=None):
        self.toolkit = Path(toolkit or DEFAULT_TOOLKIT)
        self.extra_args = list(extra_args or [])

    def fake_run(self, params: dict, workdir: Path, outdir: Path,
                 progress: ProgressFn, inputs=None) -> list[dict]:
        """Stub outputs for the worker's --fake-pipeline mode.

        Part of the contract so each service owns its own stub: the worker must not
        know what any service's outputs look like, or adding a service would mean
        editing the worker.
        """
        progress(20, "fake pipeline")
        outdir.mkdir(parents=True, exist_ok=True)
        made = []
        for name, kind in (("fake.mp4", "mp4"), ("fake.webp", "webp"),
                           ("fake.mp3", "mp3")):
            p = outdir / name
            p.write_bytes(b"\x00" * 1024)
            made.append({"kind": kind, "path": p})
        html = outdir / "programme.html"
        html.write_text("<html>fake</html>")
        made.append({"kind": "programme_html", "path": html})
        return made

    def run(self, params: dict, workdir: Path, outdir: Path,
            progress: ProgressFn, inputs=None) -> list[dict]:
        script = self.toolkit / "go_music_video.py"
        if not script.is_file():
            raise RuntimeError(
                f"toolkit not found: {script} (pass --toolkit to the worker)"
            )

        # The staged SGF is handed over by name. The pipeline derives its output
        # names from the input stem, so the worker preserved the submitted filename.
        staged = []
        if inputs:
            for path_list in inputs.values():
                staged.extend(path_list)
        sgf = staged[0] if staged else None
        if sgf is None:
            raise RuntimeError(
                "no SGF was staged for this job: declare a file field with "
                "format: 'files' in param_schema"
            )

        progress(10, "starting the render pipeline")
        cmd = [
            sys.executable, str(script), str(sgf),
            "--outdir", str(outdir),
            "--duration", str(params.get("duration_s", 180)),
            "--visits", str(params.get("visits", 300)),
            "--formats", params.get("formats") or "mp4,webp",
        ]
        if params.get("bpm") is not None:
            cmd += ["--bpm", str(params["bpm"])]
        for key, flag in (("musical_key", "--key"), ("time_sig", "--time-sig"),
                          ("seed", "--seed")):
            if params.get(key):
                cmd += [flag, str(params[key])]
        cmd += self.extra_args

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if proc.returncode != 0:
            tail = "\n".join((proc.stderr or proc.stdout or "").splitlines()[-15:])
            raise RuntimeError(f"pipeline exited {proc.returncode}:\n{tail}")

        progress(85, "render finished, collecting artifacts")
        artifacts = []
        for path in sorted(outdir.rglob("*")):
            if not path.is_file() or path.name == "pipeline.log":
                continue
            kind = SUFFIX_KINDS.get(path.suffix.lower())
            if kind:
                artifacts.append({"kind": kind, "path": path})
        if not artifacts:
            raise RuntimeError(f"pipeline produced no artifacts in {outdir}")
        return artifacts


# the loader instantiates this
def plugin(**kwargs) -> SgfVideo:
    return SgfVideo(**kwargs)
