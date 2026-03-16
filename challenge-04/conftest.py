"""Pytest conftest for challenge-04.

Registers the ``challenge-04`` directory as a Python package under the
name ``challenge_04`` so that both the test file and the source modules
(which use relative imports like ``from .state_machine import ...``) work
correctly.
"""

import importlib
import importlib.util
import sys
from pathlib import Path

_pkg_dir = Path(__file__).resolve().parent

if "challenge_04" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "challenge_04",
        str(_pkg_dir / "__init__.py"),
        submodule_search_locations=[str(_pkg_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["challenge_04"] = mod
    spec.loader.exec_module(mod)
