# Releasing plateau-bridge

Publishing is automated: **push a `vX.Y.Z` tag and CI publishes to PyPI** via
GitHub's OIDC trusted publishing — no tokens or passwords involved.

## Cut a release

1. Bump the version in **both** files (kept in lock-step so the wheel and
   `plateau_bridge.__version__` agree):
   - `pyproject.toml` → `version = "X.Y.Z"`
   - `src/plateau_bridge/__init__.py` → `__version__ = "X.Y.Z"`
2. Commit to `main` and push.
3. Tag and push:
   ```bash
   git tag -a vX.Y.Z -m "plateau-bridge X.Y.Z"
   git push origin vX.Y.Z
   ```
4. Watch: `gh run watch <id>` (workflow: `release.yml`). Builds sdist + wheel,
   publishes to PyPI, attaches artifacts to a GitHub Release.
5. Verify: `curl -s https://pypi.org/pypi/plateau-bridge/json | python3 -c "import json,sys;print(json.load(sys.stdin)['info']['version'])"`
   (PyPI's JSON index can lag ~30–60 s.)

> Note: publishing a **data bundle** (the prebuilt city `.tar.zst` files served
> by `plateau pull` / `cache add`) is a separate, maintainer-only flow — see
> `plateau cache push` and `docs/DISTRIBUTION.md`. This file is about the
> Python package only.

## One-time setup (already done — recorded for recovery)

PyPI **Trusted Publisher** for the project `plateau-bridge`
(pypi.org → project → Manage → Publishing → Add → GitHub):

| Field | Value |
|---|---|
| Owner | `pixelx-jp` |
| Repository name | `plateau-bridge` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

No API tokens are stored; the job authenticates to PyPI over OIDC.
