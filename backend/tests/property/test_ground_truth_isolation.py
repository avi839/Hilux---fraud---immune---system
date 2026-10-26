"""Property 70: ground-truth isolation (Req 20.12, 21.15, 24.11).

AST scan of every module under ``helix/``. Only ``helix.evaluation.*``,
``helix.immune.miss_detector`` and the store itself may import the ground-truth read side
(``helix.storage.ground_truth`` / ``helix.evaluation.ground_truth``) or name the
``ground_truth`` table in a string literal. Producers importing ``helix.sim.ground_truth``
(record + writer protocol only) are fine. Detection-path packages additionally may not
import ``helix.evaluation`` or the miss-detector at all, which closes the re-export route.
This is an architectural guard inside one process, not a security boundary.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

pytestmark = pytest.mark.property

ROOT = Path(__file__).resolve().parents[2] / "helix"
FORBIDDEN = ("helix.storage.ground_truth", "helix.evaluation.ground_truth")
DETECTION_PKGS = (
    "sensors", "fusion", "memory", "changepoint", "graph", "motifs", "risk", "cases", "explain", "learn", "attacker",
)
_SQL_IDENT = re.compile(r"(?<![\w.])ground_truth(?!\w)")


def allowed(mod: str) -> bool:
    return (
        mod == "helix.evaluation"
        or mod.startswith("helix.evaluation.")
        or mod in ("helix.immune.miss_detector", "helix.storage.ground_truth")
    )


def module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve(mod: str, is_pkg: bool, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    base = mod.split(".") if is_pkg else mod.split(".")[:-1]
    base = base[: len(base) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def imported(mod: str, is_pkg: bool, tree: ast.AST) -> set[str]:
    """Every module name an import statement could bind, including `from pkg import sub`."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve(mod, is_pkg, node)
            out.add(base)
            out.update(f"{base}.{a.name}" for a in node.names if a.name != "*")
    return out


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


def strings(tree: ast.AST) -> list[str]:
    skip = _docstring_ids(tree)
    return [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip
    ]


def violations(mod: str, is_pkg: bool, source: str) -> list[str]:
    tree = ast.parse(source)
    out: list[str] = []
    imps = imported(mod, is_pkg, tree)
    if not allowed(mod):
        out += [f"{mod} imports {i}" for i in sorted(imps) if any(i == f or i.startswith(f + ".") for f in FORBIDDEN)]
        for s in strings(tree):
            if _SQL_IDENT.search(s) or any(f in s for f in FORBIDDEN):
                out.append(f"{mod} names ground truth in string {s[:60]!r}")
    if any(mod == f"helix.{p}" or mod.startswith(f"helix.{p}.") for p in DETECTION_PKGS):
        bad = [
            i for i in sorted(imps)
            if i == "helix.evaluation" or i.startswith("helix.evaluation.") or i == "helix.immune.miss_detector"
        ]
        out += [f"detection module {mod} imports {i}" for i in bad]
    return out


def scan() -> list[str]:
    found: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        found += violations(module_name(path), path.name == "__init__.py", path.read_text(encoding="utf-8-sig"))
    return found


def test_no_module_outside_the_allowed_set_reads_ground_truth():
    """**Validates: Requirements 20.12, 21.15, 24.11**"""
    assert len(list(ROOT.rglob("*.py"))) > 20  # really scanning the package
    assert scan() == []


def test_allowed_readers_do_import_the_read_side():
    """Sanity: the scanner sees the real imports of the allowed readers."""
    src = (ROOT / "immune" / "miss_detector.py").read_text(encoding="utf-8-sig")
    assert "helix.evaluation.ground_truth" in imported("helix.immune.miss_detector", False, ast.parse(src))
    src = (ROOT / "evaluation" / "ground_truth.py").read_text(encoding="utf-8-sig")
    assert "helix.storage.ground_truth" in imported("helix.evaluation.ground_truth", False, ast.parse(src))


@pytest.mark.parametrize(
    "mod,is_pkg,src,bad",
    [
        ("helix.cases.manager", False, "from helix.storage.ground_truth import GroundTruthStore", True),
        ("helix.cases.manager", False, "from helix.storage import ground_truth", True),
        ("helix.storage.store", False, "from .ground_truth import GroundTruthStore", True),
        ("helix.storage", True, "from . import ground_truth", True),
        ("helix.sensors.agents", False, "import helix.evaluation.ground_truth as g", True),
        ("helix.sensors.agents", False, "from helix.evaluation import EvaluationMonitor", True),
        ("helix.fusion.core", False, "q = 'SELECT * FROM ground_truth'", True),
        ("helix.api.app", False, "import importlib; importlib.import_module('helix.storage.ground_truth')", True),
        ("helix.sim.generator", False, "from .ground_truth import GroundTruthRecord", False),
        ("helix.datasets.amlworld", False, "from helix.sim.ground_truth import GroundTruthWriter", False),
        ("helix.storage", True, '"""``helix.storage.ground_truth`` is not re-exported."""', False),
        ("helix.core.pipeline", False, "from helix.evaluation import ground_truth_channel", False),
        ("helix.evaluation.monitor", False, "from .ground_truth import GroundTruthReader", False),
        ("helix.immune.miss_detector", False, "from helix.evaluation.ground_truth import GroundTruthReader", False),
    ],
)
def test_scanner_examples(mod, is_pkg, src, bad):
    assert bool(violations(mod, is_pkg, src)) is bad


_PKGS = ["cases", "sensors", "sim", "datasets", "api", "core", "evaluation", "immune", "storage", "replay"]


@given(
    pkg=st.sampled_from(_PKGS),
    leaf=st.sampled_from(["mod", "miss_detector", "ground_truth", "pipeline"]),
    target=st.sampled_from(list(FORBIDDEN) + ["helix.sim.ground_truth", "helix.storage.store", "helix.codec"]),
    form=st.sampled_from(["import {t}", "from {t} import X", "from {p} import {l}", "import {t} as alias"]),
)
def test_scanner_flags_forbidden_import_iff_importer_not_allowed(pkg, leaf, target, form):
    """A forbidden module imported in any form is flagged exactly when the importer is
    outside the allowed set.

    **Validates: Requirements 20.12, 24.11**
    """
    mod = f"helix.{pkg}.{leaf}"
    parent, _, last = target.rpartition(".")
    src = form.format(t=target, p=parent, l=last)
    flagged = [v for v in violations(mod, False, src) if not v.startswith("detection module")]
    assert bool(flagged) is (target in FORBIDDEN and not allowed(mod))
