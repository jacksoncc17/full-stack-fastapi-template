# Adding a service

A service is one file in this directory. There is no registry to edit, no broker
change, and no migration — the loader imports every module here, and each service
publishes its own parameter schema to the broker when a worker starts.

`tarot_draw.py` is a complete working example; `sgf_video.py` is the real pipeline.

## The minimum

```python
# worker_plugins/my_service.py
from pathlib import Path
from worker_plugins.base import ProgressFn


class MyService:
    name = "my_service"          # MUST equal the filename, or the loader refuses it
    title = "My service"
    artifact_kinds = ("png",)

    param_schema = {
        "type": "object",
        "properties": {
            "size": {"type": "integer", "minimum": 16, "maximum": 2048,
                     "default": 512, "title": "Size"},
        },
        "required": ["size"],
        "additionalProperties": False,
    }

    def run(self, params: dict, workdir: Path, outdir: Path,
            progress: ProgressFn) -> list[dict]:
        progress(50, "working")
        out = outdir / "result.png"
        out.write_bytes(b"...")
        return [{"kind": "png", "path": out}]

    def fake_run(self, params, workdir, outdir, progress):
        """Optional: enables `worker.py --fake-pipeline` for this service."""
        return self.run(params, workdir, outdir, progress)
```

That is the whole thing. Then:

```bash
python3 worker.py --list-services                       # it is listed
python3 worker.py --broker ... --key ... --service my_service
```

## Which choices are enforced, and why

**`name` must equal the filename.** Not cosmetic: a mismatch otherwise shows up at
runtime as a service silently missing, which is far harder to diagnose than a
startup error.

**`param_schema` must use only supported keywords.** The broker validates submissions
against it and the frontend builds the form from it, so it is the single description
of this service's parameters. Supported: `type`, `properties`, `required`,
`additionalProperties`, `enum`, `const`, `minimum`/`maximum`, `minLength`/`maxLength`,
`pattern`, `items`, `minItems`/`maxItems`, `anyOf`/`oneOf`/`allOf`, `default`,
`title`, `description`.

A schema using `$ref`, `if/then/else` or `definitions` is **rejected at registration**
rather than partly enforced — accepting it would leave submitters believing
validation had happened.

**A worker is bound to ONE service.** The broker routes a job only to a worker
serving that service. Loading several plugins is allowed (useful for testing), but a
worker should run `--service X` in production; without it, the last published wins and
the log warns.

**`workdir` is scratch, `outdir` is results.** The worker deletes `workdir` after the
job, so anything the submitter should keep must be written to `outdir` and returned.

**`run` raising fails the job**, and the message becomes the job's error, so it should
read as something the *submitter* can act on rather than a stack trace.

**`--plugin-dir`** loads plugins from outside this package, so a deployment can ship
service code separately from the worker.

## What you do not have to touch

* the broker — it learns the schema from the worker at registration
* `worker.py` — it stages input, dispatches by service name, uploads what came back
* the frontend — the submission form is built from `param_schema`

If adding a service requires editing any of those, the abstraction has failed.
