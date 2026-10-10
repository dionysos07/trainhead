"""Turn the end of a job log into a GitHub error annotation.

Annotations are readable through the GitHub API, which the Claude runs can
reach even where the raw log storage is blocked.
Usage: python scripts/annotate_failure.py <log file> "<title>"
"""
import pathlib
import sys

path, title = sys.argv[1], sys.argv[2]
try:
    text = pathlib.Path(path).read_text(errors="replace")
except FileNotFoundError:
    text = "no log was written"
text = text[-6000:]
encoded = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
print(f"::error title={title}::{encoded}")
