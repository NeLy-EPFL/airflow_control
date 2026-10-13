"""Optional add-ons to the GUI, such as closed-loop ball delivery.

The core app (MFC control) knows nothing specific about a plugin. A plugin
brings:

- a panel: ``panel.html`` (an HTML fragment) and ``panel.js`` in its
  ``static_dir``, served under ``/plugins/<name>/``. The page inserts the
  fragment above the MFC panels and loads the script, which registers itself
  with ``window.airflowPlugin(name, {render(status), redraw()})``; ``render``
  gets every status update, the plugin's own part in ``status.plugins[name]``;
- API routes under ``/api/plugins/<name>/`` (:meth:`Plugin.add_routes`);
- hooks: started and closed with the server, told about manual actions on an
  MFC (which take the MFC back from any automation), its part of the status.

Plugins are enabled per machine in the config, as ``[plugins.<name>]``
tables, or from ``gui`` options.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter


class Plugin:
    name: str = ""  # URL segment and config table name
    static_dir: Path | None = None  # holds panel.html and panel.js

    def add_routes(self, router: APIRouter, run: Callable[[Callable[[], object]], dict]) -> None:
        """Add API routes to *router* (prefix ``/api/plugins/<name>``). ``run(fn)``
        calls ``fn`` mapping its errors to HTTP ones (ValueError: 400, KeyError:
        404, else 503) and returns the full status, as the core routes do."""

    def start(self) -> None:
        """The server is up."""

    def close(self) -> None:
        """The server is going down; the MFCs are set to 0 right after."""

    def status(self) -> dict:
        return {}

    def on_manual(self, channel: str | None) -> None:
        """An operator acted on MFC *channel* (setpoint, puff, stop), or on all
        of them (``None``: STOP ALL, shutdown). Automation must let go of it."""
