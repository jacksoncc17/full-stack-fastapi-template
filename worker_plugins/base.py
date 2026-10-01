"""
The service plugin contract.

A service is the unit of work this system schedules. Adding one means writing a
class in `worker_plugins/` that implements `ServicePlugin` -- there is no registry to
edit, no broker change, and no migration. The loader finds it by importing the
package.

Why the split is where it is: the broker schedules jobs, the worker runs them. The
broker therefore only needs a service's *contract* (name, param schema, what kinds of
artifact it produces) and never its code. That is what makes a service addable
without touching the broker at all -- publishing the contract at worker startup is
the entire registration step.

Writing a plugin
----------------
Subclass `ServicePlugin`, set the four class attributes, implement `run`. Then:

    class MyService(ServicePlugin):
        name = "my_service"
        title = "My service"
        param_schema = {
            "type": "object",
            "properties": {"size": {"type": "integer", "minimum": 16,
                                    "default": 512}},
            "required": ["size"],
            "additionalProperties": False,
        }
        artifact_kinds = ("png",)

        def run(self, params, workdir, outdir, progress):
            progress(10, "starting")
            out = outdir / "result.png"
            ...
            return [{"kind": "png", "path": out}]

`param_schema` is validated by the broker on submission and rendered as a form by
the frontend, so it must only use keywords the broker's validator supports -- it
refuses a schema it cannot fully enforce. Keep to: type, properties, required,
additionalProperties, enum, minimum/maximum, minLength/maxLength, pattern, items,
default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, Protocol, runtime_checkable

# progress(percent, message) -- reported back to the broker and shown in the UI
ProgressFn = Callable[[int, str], None]


@runtime_checkable
class ServicePlugin(Protocol):
    """Structural contract. Subclassing is optional; matching the shape is enough."""

    name: str
    title: str
    param_schema: dict
    artifact_kinds: tuple[str, ...]

    def run(
        self,
        params: dict,
        workdir: Path,
        outdir: Path,
        progress: ProgressFn,
        inputs: dict[str, list[Path]] | None = None,
    ) -> Iterable[dict]:
        """Do the work for one job.

        `inputs` maps the service's file field name (from `param_schema`) to the
        staged files for it. Passed explicitly rather than left for the plugin to
        discover by scanning `workdir`: a service may declare several input fields,
        and guessing which file is which would be unreliable exactly when it matters.
        It stays optional so a plugin with no inputs ignores it.

        `workdir` is scratch space, safe to fill and expected to be deleted -- this
        is the right place for intermediate files, including large ones.

        `outdir` is where results go. Return one dict per artifact with `kind` and
        `path`; `kind` is free-form but should match `artifact_kinds` so the UI can
        describe results before they exist.

        Raise to fail the job: the exception message becomes the job's error, so it
        should say what went wrong in terms the submitter understands.
        """
        ...


class PluginError(RuntimeError):
    """A plugin is malformed or could not be loaded.

    Raised at startup rather than at claim time: a worker that loads a broken plugin
    and then fails every job it claims is worse than one that refuses to start.
    """


def validate_plugin(obj: object, origin: str) -> None:
    """Check a candidate against the contract, with messages that say what is wrong.

    Deliberately explicit rather than relying on `isinstance(..., ServicePlugin)`:
    a Protocol runtime check only verifies attribute presence, so a class with
    `name` misspelled as `Name` passes and then fails in a confusing place.
    """
    for attr in ("name", "title", "param_schema", "artifact_kinds"):
        if not hasattr(obj, attr):
            raise PluginError(f"{origin}: missing required attribute {attr!r}")
    plugin = obj
    if not isinstance(plugin.name, str) or not plugin.name.strip():
        raise PluginError(f"{origin}: 'name' must be a non-empty string")
    if not isinstance(plugin.title, str) or not plugin.title.strip():
        raise PluginError(f"{origin}: 'title' must be a non-empty string")
    if not isinstance(plugin.param_schema, dict):
        raise PluginError(f"{origin}: 'param_schema' must be a dict")
    if not isinstance(plugin.artifact_kinds, (tuple, list)):
        raise PluginError(f"{origin}: 'artifact_kinds' must be a tuple or list")
    if not artifact_kinds_valid(plugin.artifact_kinds):
        raise PluginError(f"{origin}: 'artifact_kinds' must all be strings")
    run = getattr(plugin, "run", None)
    if not callable(run):
        raise PluginError(f"{origin}: 'run' must be callable")

    # A plugin whose name does not match its filename is a real source of confusion
    # when reading a deployment, so it is rejected rather than warned about.
    expected = origin.rsplit(":", 1)[-1]
    if plugin.name != expected:
        raise PluginError(
            f"{origin}: plugin name {plugin.name!r} must match the module name "
            f"{expected!r} (the loader uses the filename as the service name)"
        )


def artifact_kinds_valid(kinds: Iterable) -> bool:
    return all(isinstance(k, str) and k for k in kinds)


# `check_schema_supported(schema)` used to live here. It imported the broker's
# `app.param_schema` -- a backend module that is NOT on the worker's path, since the
# worker only ever has `worker_plugins` and the plugin directories -- and it was never
# called by anything. Its docstring claimed the worker ran it at registration, which
# was wrong: registration is the broker's job. POST /worker/services validates the
# schema server-side (`param_schema.check_schema`) and answers 422, so a service with
# an unsupported schema is refused where the check can actually be enforced, and the
# worker needs no access to the backend package.
