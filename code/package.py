"""Build code.zip for submission (AGENTS.md §6.5): runnable solution, prompts/config, README, evaluation/.

    python3 code/package.py            # -> ./code.zip

Excludes dataset/, secrets (.env), caches, virtualenvs, git metadata and log.txt.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCLUDE = ["code", "tests", "docs", "README.md", "requirements.txt", ".env.example", "problem_statement.md", "AGENTS.md", "CLAUDE.md", ".gitignore"]
EXCLUDE_PARTS = {"__pycache__", ".pytest_cache", ".cache", ".venv", "venv", "node_modules", ".git"}
EXCLUDE_NAMES = {".env", "log.txt", "code.zip", "packets.jsonl", "sample_packets.jsonl"}


def main() -> int:
    target = ROOT / "code.zip"
    n = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for item in INCLUDE:
            p = ROOT / item
            files = [p] if p.is_file() else sorted(q for q in p.rglob("*") if q.is_file())
            for f in files:
                rel = f.relative_to(ROOT)
                if set(rel.parts) & EXCLUDE_PARTS or f.name in EXCLUDE_NAMES or f.suffix == ".pyc":
                    continue
                z.write(f, str(rel))
                n += 1
    required = {"code/main.py", "code/evaluation/usage_report.md", "README.md"}
    with zipfile.ZipFile(target) as z:
        names = set(z.namelist())
    missing = required - names
    if missing:
        print(f"ERROR: code.zip is missing {sorted(missing)}", file=sys.stderr)
        return 1
    print(f"wrote {target} ({n} files, {target.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
