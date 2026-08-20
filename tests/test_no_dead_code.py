"""Nothing in the package may import a name it does not use, or define one twice.

Both of these are cheap to check and expensive to find by reading. The dead
imports that prompted this were not harmful in themselves — `Optional` costs
nothing — but one of them was actively misleading: `secure()` opened with

    from .sources.browser import BASKET_MARKERS, SEARCH_BUTTONS, _is_listing_row

and never used the third name, so the buying path read as though it validated
listing rows the same way the watcher does. It does not. An import is a claim
about what the code below it does, and a false claim in the one function that
touches David's account is worth a test.

The duplicate-definition check earns its place from history: commit ae1c0be
("Keep one secure_in_thread, not the two the merge left behind") fixed exactly
that, and nothing would have caught it. Python takes the second definition
silently, so the version you are reading may not be the version that runs.

Deliberately dependency-free, in the style of everything else in tests/ — this
project has two runtime dependencies and no dev ones, and a linter in CI that
nobody can run locally is a linter that gets disabled.

Run with:  .venv/bin/python tests/test_no_dead_code.py
"""

import ast
import collections
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def imported_names(tree):
    """{bound_name: lineno} for every import anywhere in the module."""
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # `import a.b.c` binds `a`; `import a.b as x` binds `x`.
                names[alias.asname or alias.name.split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    names[alias.asname or alias.name] = node.lineno
    return names


def used_names(tree):
    """Every bare name the module mentions, including attribute roots.

    `config.EVENTS` has to count as a use of `config`, or every module here
    would look as though it imported config for nothing.
    """
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            root = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name):
                used.add(root.id)
    return used


def duplicate_definitions(body):
    """Names defined more than once in one scope — the second silently wins."""
    seen = collections.Counter(
        node.name for node in body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )
    return sorted(name for name, count in seen.items() if count > 1)


sources = sorted(REPO.joinpath("ep_watcher").rglob("*.py"))
print(f"\nScanning {len(sources)} module(s) under ep_watcher/")

dead = []
duplicated = []

for path in sources:
    text = path.read_text()
    tree = ast.parse(text, str(path))
    rel = path.relative_to(REPO)

    used = used_names(tree)
    for name, line in imported_names(tree).items():
        # A name mentioned nowhere but its own import line. The string count
        # is a second opinion, so a name reached in a way the AST walk does
        # not model — a docstring reference, a __all__ entry, a getattr — is
        # left alone rather than reported.
        if name not in used and text.count(name) <= 1:
            dead.append(f"{rel}:{line} {name}")

    for name in duplicate_definitions(tree.body):
        duplicated.append(f"{rel}: {name}")
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for name in duplicate_definitions(node.body):
                duplicated.append(f"{rel}: {node.name}.{name}")

print("\nEvery import is used")
if dead:
    for item in dead:
        print(f"        unused: {item}")
check("no unused imports", dead, [])

print("\nNothing is defined twice in the same scope")
if duplicated:
    for item in duplicated:
        print(f"        defined twice: {item}")
check("no duplicate definitions", duplicated, [])

print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
