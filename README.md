# breadcrumbs-common

The core of breadcrumbs: a content-addressed store of memory, six read-only verbs (find, open, zoom, trail, stores, health), three servers (MCP, socket, A2A), the command line, and the registry of ports.

- Needs: Python 3.11 or newer. Optional: google-crc32c (the CRC32C as a CPU instruction).
- Run: `python -m breadcrumbs --help`. With only this package, the viewers and the harness commands are absent.
- Other layers add themselves: each package `breadcrumbs_<layer>` that is on the path gives an `adapter.py`, and `breadcrumbs.ports.load()` finds it by that name.
- Test: `PYTHONPATH=. python -m unittest discover -s tests`.

MIT licence. Part of breadcrumbs-agency.
