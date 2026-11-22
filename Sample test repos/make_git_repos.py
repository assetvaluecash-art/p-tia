"""Turn the sample repositories into git repositories, with the six changes of payments-api as branches.

    python make_git_repos.py [--out git-repos]

Needs only Python 3 and git. The web UI does not need this (upload the zips instead); it is for registering repositories by path or for
the command line, which compare git revisions:

    tia analyze --repo payments-api=git-repos/payments-api --repo statement-service=git-repos/statement-service \\
        --repo mobile-bff=git-repos/mobile-bff --repo web-portal=git-repos/web-portal --repo qa-testcases=git-repos/qa-testcases \\
        --target payments-api --base main --head feature/holds
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
IDENTITY = ["-c", "user.name=TIA sample", "-c", "user.email=sample@tia.invalid"]


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *IDENTITY, *args], cwd=repo, check=True, capture_output=True)


def clear(repo: Path) -> None:
    for p in repo.iterdir():
        if p.name != ".git":
            shutil.rmtree(p) if p.is_dir() else p.unlink()


def branch_of(folder: str) -> str:
    """01-feature-holds -> feature/holds, 06-docs-clarify -> docs/clarify"""
    kind, _, rest = folder.split("-", 1)[1].partition("-")
    return f"{kind}/{rest}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="git-repos", help="where to create the repositories (default: ./git-repos)")
    out = Path(ap.parse_args().out).resolve()
    if out.exists():
        raise SystemExit(f"{out} already exists: remove it or choose another --out")
    for src in sorted((HERE / "repos").iterdir()):
        repo = out / src.name
        shutil.copytree(src, repo)
        git(repo, "init", "-q", "-b", "main")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "main")
    pay = out / "payments-api"
    for scenario in sorted((HERE / "zips" / "scenarios").iterdir()):
        name = branch_of(scenario.name)
        git(pay, "checkout", "-q", "-b", name, "main")
        clear(pay)
        with zipfile.ZipFile(scenario / "payments-api.zip") as z:
            z.extractall(pay)
        git(pay, "add", "-A")
        git(pay, "commit", "-qm", name)
        git(pay, "checkout", "-q", "main")
    print(f"created {out}: five repositories; payments-api has main and the branches "
          + ", ".join(branch_of(s.name) for s in sorted((HERE / "zips" / "scenarios").iterdir())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
