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

**A service is either a module or a package.** In the bundled `worker_plugins/` it must be
one file per service — the loader scans that package for modules only — so `tarot_draw` is
the shape a bundled service takes. A service that is a directory has to come from a
`--plugin-dir`:

```
services/
  sgf_video/                <- package: the SGF render service (moved out of here)
    __init__.py             <- re-exports `plugin`
    service.py              <- the contract: name, param_schema, artifact_kinds, run
    go_music_video.py       <- ...and the pipeline only this service runs
  intonationCheck/          <- package: one service, several modules
    __init__.py             <- re-exports `plugin`
    service.py              <- the contract: name, param_schema, artifact_kinds, run
    score.py  pitch.py  report.py
    tests/
```

**The implementation must not be a file called `plugin.py`.** A submodule is bound onto its
parent package under its own name, so `plugin.py` would set the package's `plugin` attribute
to the module — shadowing the factory the loader reads and turning a valid service into
`no plugin found`. Name it anything else (`service.py` by convention here).

`sgf_video` lives there rather than here because the code a service runs belongs next to
the service, not in the worker's own package — and because this package ships with the
broker, while a real pipeline depends on a toolkit the broker repo does not have. A worker
for it therefore needs `--plugin-dir /path/to/services`, or it will not find the service at
all.

Use a package when the service genuinely needs more than one file — a parser, a
measurement, a renderer — and wants them kept together with their own tests. Two rules
apply to both shapes:

* the directory (or file) name, the import name and the plugin's own `name` must all be
  identical — the loader rejects a mismatch, so a mistake is a startup error rather
  than a service that silently never loads;
* only the package itself is imported, never modules inside it, so submodules must
  reach each other with relative imports (`from .score import ...`). `--plugin-dir`
  puts the *parent* directory on `sys.path`, so the package is imported by its bare
  name and any absolute self-import would fail.

## What you do not have to touch

* the broker — it learns the schema from the worker at registration
* `worker.py` — it stages input, dispatches by service name, uploads what came back
* the frontend — the submission form is built from `param_schema`

If adding a service requires editing any of those, the abstraction has failed.

## The submission form is generated from `param_schema`

Nothing renders a per-service form. The frontend fetches `/jobs/services`, reads the
selected service's `param_schema`, and maps each property to a control:

| schema | control |
|---|---|
| `type: string` | text input |
| `type: string` with `maxLength > 200` | textarea |
| `type: string` + `format: textarea` | textarea |
| `type: number` / `integer` | number input |
| `type: number` + `format: slider` | range input with the value shown |
| `type: boolean` | checkbox |
| `enum: [...]` | select |
| `format: files` | file picker (multiple) |

An unrecognised `format` falls back to a plain control, so an older frontend works
against a newer service.

### Declaring a file input

```python
"sources": {
    "type": "array",
    "format": "files",
    "title": "Game record (.sgf)",
}
```

That is the whole declaration. The frontend finds the field by looking for
`format: "files"` rather than assuming a name, and the values travel as multipart
parts instead of inside the params JSON. The broker injects the uploaded filenames
into `params` **before** validating, so a file field can legitimately be `required` --
without that, validation would always fail with "missing required property" since the
frontend strips file fields out of the JSON.

### Where a service's parameters are declared

Once, in the plugin, as `param_schema`. It is then used for three things:

1. the worker publishes it to the broker at registration
2. the broker validates every submission against it
3. the frontend builds the form from it

So a new parameter needs one edit, in one file, with no broker or frontend change.

## Launching a worker

A worker needs a key from the broker, and that is the only setup:

```bash
cd full-stack-fastapi-template

# 1. register it (admin token from an email/password login, or the Workers page)
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/login/access-token \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'username=admin@example.com&password=changethis' \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['access_token'])")

curl -s -X POST http://localhost:8000/api/v1/admin/workers/ \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"sgf-worker-1"}'
# => {"api_key": "...", ...}   the key is shown ONCE

# 2. see what this checkout can run (--plugin-dir is what finds services/)
python3 worker.py --list-services --plugin-dir /path/to/selfCommentary/services

# 3. run the SGF service for real
python3 worker.py --broker http://localhost:8000/api/v1 \
  --key <api_key> --name sgf-worker-1 \
  --service sgf_video \
  --plugin-dir /path/to/selfCommentary/services \
  --toolkit /path/to/selfCommentary/sgf_tools
```

On startup it **publishes its contract** (so the broker learns the parameter schema and
binds this worker to `sgf_video`) and then polls for jobs of that service only.

### Flags worth knowing

| flag | why |
|---|---|
| `--service sgf_video` | pick one plugin explicitly. Without it every loaded plugin is published, and **the broker binds a worker to one service — the last published wins** |
| `--toolkit /path` | the checkout's `sgf_tools/` directory, where the steps the SGF pipeline shares with non-service tools live (`remote_katago.py`, `sgf_animate.py`). Passed to the plugin as `--shared-dir`. Only the SGF service needs it |
| `--fake-pipeline` | stub artifacts, no render. Exercises the queue in seconds |
| `--plugin-dir /path` | load plugins from outside this package |
| `--lease-seconds 3600` | must exceed the longest render; the lease auto-renews every ~5 min anyway |
| `--list-services` | show what would load, then exit |
| `--once` | process one job and exit, for testing |

### The toolkit requirement

Only `sgf_video` needs `--toolkit`, and it is not what finds `go_music_video.py` — that
ships inside `services/sgf_video/`. It names the checkout's `sgf_tools/`, where the two
steps the pipeline shares with tools that are not services (`remote_katago.py`,
`sgf_animate.py`) live; the plugin forwards it as `--shared-dir`. The service also needs more than code: the
pipeline reaches **KataGo over SSH** and **ACE-Step over HTTP**. A worker without those
will claim a job and fail it with a clear error. Use `--fake-pipeline` to test the
plumbing anywhere.

For a long-running worker, see `deploy/worker-intonation.service` and
`deploy/worker-sgf.service` -- systemd units, so they survive reboots and network
changes. There is one per service because each needs a different interpreter (the
intonation worker's dependencies are not in the toolkit venv), and both already pass
`--service`, which is required once more than one plugin is loadable.
