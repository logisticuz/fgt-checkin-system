"""Verify every original main.py function remains source-identical after moving."""

import argparse
import ast
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_REF = "a8c74f7"


def functions(source):
    lines = source.splitlines()
    extracted = {}

    def visit(node, parents=()):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = ".".join((*parents, child.name))
                # lineno starts at def, so route decorators are deliberately excluded.
                extracted[key] = textwrap.dedent(
                    "\n".join(lines[child.lineno - 1 : child.end_lineno])
                )
                visit(child, (*parents, child.name))
            elif isinstance(child, ast.ClassDef):
                visit(child, (*parents, child.name))
            else:
                visit(child, parents)

    visit(ast.parse(source))
    return extracted


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=BASE_REF)
    options = parser.parse_args(argv)
    original = subprocess.check_output(
        ["git", "show", f"{options.base}:backend/main.py"], cwd=ROOT, text=True, encoding="utf-8"
    )
    baseline = functions(original)
    current = {}
    duplicates = set()
    paths = [ROOT / "backend" / "main.py", ROOT / "backend" / "app_state.py"]
    paths.extend(sorted((ROOT / "backend" / "routers").glob("*.py")))
    for path in paths:
        if not path.exists():
            continue
        for name, source in functions(path.read_text(encoding="utf-8")).items():
            if name in current:
                duplicates.add(name)
            current[name] = source
    identical = 0
    mismatches = []
    for name, source in baseline.items():
        if name not in current:
            mismatches.append(f"MISSING {name}")
        elif name in duplicates:
            mismatches.append(f"DUPLICATE {name}")
        elif current[name] != source:
            mismatches.append(f"CHANGED {name}")
        else:
            identical += 1
    for mismatch in mismatches:
        print(mismatch)
    print(f"{identical} functions identical, {len(mismatches)} mismatches")
    return bool(mismatches)


if __name__ == "__main__":
    raise SystemExit(main())
