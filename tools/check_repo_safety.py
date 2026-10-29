"""Repository safety check: forbidden files and secret-shaped strings.

Scans tracked files at HEAD and, with --history, every line ever added in git
history. Prints file/commit and line, never the matched value. Exit code 1 if
anything is found. Standard library only; run from anywhere inside the repo:

    python tools/check_repo_safety.py --history
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

FORBIDDEN_FILES = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "*.sqlite3",
    "*.sqlite",
    "*.db",
    "settings.local.json",
)
ALLOWED_FILES = (".env.example",)

SECRET_PATTERNS = {
    "openai_or_anthropic_key": re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    "github_token": re.compile(
        r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}"
    ),
    "aws_access_key_id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "slack_token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "assigned_api_key": re.compile(r"(?i)\b(?:OPENAI|ANTHROPIC)_API_KEY\s*=\s*['\"]?[^\s'\"#]{8,}"),
}

# Obvious test fixtures used to exercise redaction; never real credentials.
DUMMY_MARKERS = ("abcdefghijklmnop", "0000000000", "sk-nope")


def git(*args: str) -> str:
    out = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return out.stdout


def forbidden(path: str) -> bool:
    name = PurePosixPath(path).name
    if name in ALLOWED_FILES:
        return False
    return any(fnmatch.fnmatch(name, pat) for pat in FORBIDDEN_FILES)


def scan_line(line: str) -> list[str]:
    hits = []
    for label, pattern in SECRET_PATTERNS.items():
        for m in pattern.finditer(line):
            if not any(d in m.group(0) for d in DUMMY_MARKERS):
                hits.append(label)
    return hits


def scan_head() -> list[str]:
    findings = []
    for path in git("ls-files", "-z").split("\0"):
        if not path:
            continue
        if forbidden(path):
            findings.append(f"forbidden file tracked: {path}")
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for label in scan_line(line):
                findings.append(f"{path}:{n}: possible {label}")
    return findings


def scan_history() -> list[str]:
    findings = []
    commit = path = ""
    # -m: also show merge commits' changes against each parent.
    log = git("log", "--all", "-p", "-m", "--no-color", "--unified=0", "--format=commit %H")
    for raw in log.splitlines():
        if raw.startswith("commit "):
            commit = raw.split()[1][:12]
        elif raw.startswith("+++ b/"):
            path = raw[6:]
            if forbidden(path):
                findings.append(f"{commit}: forbidden file added in history: {path}")
        elif raw.startswith("+") and not raw.startswith("+++"):
            for label in scan_line(raw[1:]):
                findings.append(f"{commit}:{path}: possible {label} (history)")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--history", action="store_true", help="also scan all git history")
    args = parser.parse_args()
    os.chdir(git("rev-parse", "--show-toplevel").strip())
    findings = scan_head()
    if args.history:
        findings += scan_history()
    for f in findings:
        print(f)
    files = len([p for p in git("ls-files").splitlines() if p])
    scope = "HEAD + history" if args.history else "HEAD"
    print(f"checked {files} tracked files ({scope}); findings: {len(findings)}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
