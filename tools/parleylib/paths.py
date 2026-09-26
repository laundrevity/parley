"""Where the spec lives. The tools need SCHEMA files, PROTOCOL.md (for the agent rules) and
the template registry; all three sit in the parley repository, not in the package. They are
found by walking up from this file (a checkout or an editable install) or via $PARLEY_HOME."""
from __future__ import annotations

import os


def _find_root() -> str:
    env = os.environ.get("PARLEY_HOME")
    if env and os.path.isfile(os.path.join(env, "PROTOCOL.md")):
        return os.path.abspath(env)
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isfile(os.path.join(d, "PROTOCOL.md")) and os.path.isdir(os.path.join(d, "schema")):
            return d
        d = os.path.dirname(d)
    raise RuntimeError(
        "cannot find the parley repository (PROTOCOL.md and schema/). Install it editable "
        "(`uv tool install --editable ~/git/parley`) or set PARLEY_HOME to the checkout.")


SPEC_ROOT = _find_root()
TOOLS_DIR = os.path.join(SPEC_ROOT, "tools")
SCHEMA_DIR = os.path.join(SPEC_ROOT, "schema")
PROTOCOL_MD = os.path.join(SPEC_ROOT, "PROTOCOL.md")
TEMPLATE_REGISTRY = os.path.join(SPEC_ROOT, ".parley", "participants.json")
