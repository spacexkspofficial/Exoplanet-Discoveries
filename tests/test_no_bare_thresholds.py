"""HANDOFF 6.6's invariant, enforced for the new kernel modules.

The historical failure: `7.1` appeared at six call sites in cli.py, the
duration grid's rails produced 4,401 edge-pinned fits, and a 3-sigma
secondary gate was applied at whatever literal each site happened to carry.
Science thresholds now live in exohunt.config with named fields and stated
rationale; the modules that consume them must not re-introduce the literals.

Scope: the post-overhaul kernel modules. cli.py's historical literals are
migrated when its science paths are rewired onto this kernel (the P2 switch),
at which point cli.py joins this list.
"""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "exohunt"

# Modules that must source every science threshold from exohunt.config.
KERNEL_MODULES = (
    "detrend.py",
    "search.py",
    "vetoes.py",
    "population.py",
    "ledger.py",
    "importer.py",
    "checkpoints.py",
    "lease.py",
    "paths.py",
    # P4 vetting kernel. The pixel scale (21.0) is the literal these modules
    # most want to re-introduce -- a match radius "of one pixel" is the whole
    # point -- so they derive it from InstrumentConfig instead.
    "snapshots.py",
    "identity.py",
)

# The numeric values with a documented history of silent drift. Scanned as
# AST constants so prose in comments and docstrings does not trip the wire --
# the rule is about numbers in *code*.
FORBIDDEN_VALUES = {7.1, 13.7, 21.0, 0.15}


def test_kernel_modules_carry_no_bare_science_thresholds() -> None:
    import ast

    offenders: list[str] = []
    for name in KERNEL_MODULES:
        tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, (int, float))
                and float(node.value) in FORBIDDEN_VALUES
            ):
                offenders.append(f"{name}:{node.lineno}: {node.value!r}")
    assert not offenders, (
        "Science thresholds belong in exohunt.config with a named field and "
        "a rationale:\n" + "\n".join(offenders)
    )


def test_config_is_where_the_thresholds_actually_live() -> None:
    source = (SRC / "config.py").read_text(encoding="utf-8")
    for literal in ("7.1", "13.70", "21.0", "0.15"):
        assert literal in source, (
            f"{literal} should be defined (once, with rationale) in config.py"
        )
