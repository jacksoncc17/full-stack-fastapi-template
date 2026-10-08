"""
Discover and load service plugins.

Two sources, deliberately:

1. the `worker_plugins` package this file lives in -- where `tarot_draw` lives, and what
   makes a small bundled service simply "add a file here". Only single MODULES are
   picked up from here, never packages
2. any extra directories given with `--plugin-dir`, so a deployment can ship service
   code separately from the worker without editing the repo. This is where the real
   services live (`services/sgf_video/`, `services/intonationCheck/`), and it is the
   only way to load one written as a package

A plugin is a module whose name matches the service it implements, exposing either a
plain class (instantiated with no arguments) or a `plugin(**kwargs)` factory (for one
that needs configuration such as a toolkit path). That convention is enforced --
`validate_plugin` rejects a name/module mismatch -- because a mismatch otherwise shows
up as a service silently missing at runtime rather than as an error.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path
from types import ModuleType

from worker_plugins.base import PluginError, ServicePlugin, validate_plugin


def _is_module_plugin(name: str) -> bool:
    return not name.startswith("_") and name not in ("base", "loader")


def _load_module(name: str, module: ModuleType, factory_kwargs: dict,
                 out: dict[str, ServicePlugin]) -> None:
    obj = getattr(module, "plugin", None)
    if callable(obj) and not isinstance(obj, type):
        instance = obj(**factory_kwargs)          # factory form
    else:
        # class form: find a class whose lowercased name matches the module name
        cls = None
        for attr in vars(module).values():
            if (isinstance(attr, type) and not attr.__module__.startswith("worker_plugins.base")
                    and getattr(attr, "name", None) == name):
                cls = attr
                break
        if cls is None:
            raise PluginError(
                f"{name}: no plugin found -- expose a class with name={name!r} "
                f"or a plugin(**kwargs) factory"
            )
        instance = cls(**factory_kwargs)

    validate_plugin(instance, f"{name}:{name}")
    if instance.name in out:
        raise PluginError(f"duplicate service name {instance.name!r}")
    out[instance.name] = instance


def load_plugins(plugin_dirs: list[Path] | None = None,
                 **factory_kwargs) -> dict[str, ServicePlugin]:
    """Return {service_name: plugin_instance}.

    Raises PluginError for anything malformed. A worker that starts with a broken
    plugin and then fails every job it claims is worse than one that refuses to
    start, so this is fatal by design.
    """
    plugins: dict[str, ServicePlugin] = {}

    import worker_plugins as pkg

    for info in pkgutil.iter_modules(pkg.__path__):
        if not _is_module_plugin(info.name) or info.ispkg:
            continue
        module = importlib.import_module(f"worker_plugins.{info.name}")
        _load_module(info.name, module, factory_kwargs, plugins)

    for directory in (plugin_dirs or []):
        directory = Path(directory).expanduser().resolve()
        if not directory.is_dir():
            raise PluginError(f"--plugin-dir is not a directory: {directory}")
        sys.path.insert(0, str(directory))
        for name, path in _candidates(directory):
            try:
                module = importlib.import_module(name)
            except Exception as e:                # noqa: BLE001
                raise PluginError(f"{path}: could not import: {e}") from e
            _load_module(name, module, factory_kwargs, plugins)

    if not plugins:
        raise PluginError(
            "no service plugins were found; a worker with nothing to run would "
            "silently claim nothing"
        )
    return plugins


def _candidates(directory: Path) -> list[tuple[str, Path]]:
    """Service-plugins available in `directory`, as (module name, path).

    Both shapes are supported, because both are reasonable ways to write a service:

    * `name.py` -- a single-module service, the simplest thing that works
    * `name/__init__.py` -- a package, for a service that needs more than one file
      (a parser, a measurement, a renderer) and wants them together

    Only the package itself is imported, never the modules inside it: a package is
    imported by its own name, so its submodules reach each other with relative
    imports. Importing `name/service.py` directly would put it at the top level and
    break those imports.

    A package's implementation must NOT be a submodule called `plugin.py`: importing it
    binds it to the package's own `plugin` attribute, which is the name the loader reads,
    so the factory gets shadowed by the module. See services/sgf_video/__init__.py.
    """
    found: list[tuple[str, Path]] = []
    for path in sorted(directory.iterdir()):
        if path.is_dir():
            if (path / "__init__.py").is_file() and _is_module_plugin(path.name):
                found.append((path.name, path))
        elif path.suffix == ".py" and _is_module_plugin(path.stem):
            found.append((path.stem, path))
    return found


def describe(plugins: dict[str, ServicePlugin]) -> str:
    lines = []
    for name, p in sorted(plugins.items()):
        kinds = ", ".join(p.artifact_kinds)
        lines.append(f"  {name:<18} {p.title:<28} artifacts: {kinds}")
    return "\n".join(lines)
