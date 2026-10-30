"""PostToolUse hook: ruff-format the Python file Claude just wrote or edited.

Silent on non-Python files, on files outside this project and on any failure.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> None:
    """Format the edited file with the project's pinned ruff, if it is Python in this repo."""
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return
    path = str(payload.get("tool_input", {}).get("file_path", ""))
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
    uv = shutil.which("uv")
    if not path.endswith(".py") or not uv or not Path(path).resolve().is_relative_to(project):
        return
    subprocess.run(  # noqa: S603 — fixed argv, only the edited path varies
        [uv, "run", "--frozen", "--quiet", "--project", str(project), "ruff", "format", path],
        check=False,
        capture_output=True,
        timeout=20,
    )


if __name__ == "__main__":
    main()
