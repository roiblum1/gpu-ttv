"""PreToolUse hook: refuse Bash commands that would change cluster state.

AGENT.md §0.4 keeps this project read-only toward clusters; installs are the user's to run with `!`.
Exception: lab clusters the user approved, listed in the gitignored `.claude/lab-clusters.local`.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path, PurePosixPath

LAB_ALLOWLIST = ".claude/lab-clusters.local"
_PATH_VALUE = r"(\"[^\"]*\"|'[^']*'|[^\s;&|)]+)"
KUBECONFIG_ASSIGN = re.compile(r"(?:^|[\s;&|(])(?:export\s+)?KUBECONFIG=" + _PATH_VALUE)
KUBECONFIG_FLAG = re.compile(r"--kubeconfig(?:=|\s+)" + _PATH_VALUE)
SERVER_LINE = re.compile(r"^\s*server:\s*[\"']?([^\s\"']+)", re.MULTILINE)

CLIS = frozenset({"oc", "kubectl", "helm"})
SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
REMOTE = frozenset({"ssh", "watch"})
KEYWORDS = frozenset({"if", "then", "elif", "else", "do", "while", "until", "{", "}", "!", "time"})
WRAPPERS = frozenset(
    {"sudo", "doas", "env", "rtk", "timeout", "gtimeout", "xargs", "nice", "ionice", "stdbuf"}
    | {"command", "exec", "caffeinate", "nohup", "find", "parallel", "flock"}
)
VALUE_FLAGS = frozenset(
    {"-n", "--namespace", "--context", "--kube-context", "--kubeconfig", "--cluster", "--server"}
    | {"-s", "--token", "--as", "--as-group", "--as-uid", "--user", "--request-timeout", "-v"}
    | {"--v", "--loglevel", "--certificate-authority", "--client-certificate", "--client-key"}
    | {"--tls-server-name", "--cache-dir", "--kube-apiserver", "--kube-token", "--kube-as-user"}
    | {"--kube-as-group", "--kube-ca-file", "--kube-tls-server-name", "--registry-config"}
    | {"--repository-config", "--repository-cache", "--burst-limit", "--qps", "--profile"}
    | {"--profile-output", "--username", "--password"}
)
KUBE_WRITE = frozenset(
    {"annotate", "apply", "attach", "autoscale", "cancel-build", "certificate", "cordon", "cp"}
    | {"create", "debug", "delete", "drain", "edit", "exec", "expose", "idle", "import-image"}
    | {"label", "new-app", "new-build", "new-project", "patch", "replace", "rollback", "rsh"}
    | {"rsync", "run", "scale", "secrets", "set", "start-build", "tag", "taint", "uncordon"}
)
KUBE_READ = frozenset(
    {"get", "describe", "logs", "explain", "api-resources", "api-versions", "whoami", "version"}
    | {"top", "events", "status", "wait", "diff", "port-forward", "proxy", "config", "login"}
    | {"logout", "project", "projects", "completion", "plugin", "kustomize", "observe"}
    | {"extract", "process", "options", "help", "cluster-info", "convert", "registry"}
)
POLICY_READ = frozenset({"who-can", "scc-review", "scc-subject-review"})
KUBE_READ_TREE: dict[str, object] = {
    "adm": {
        "top": None,
        "inspect": None,
        "node-logs": None,
        "wait-for-stable-cluster": None,
        "policy": POLICY_READ,
        "release": frozenset({"info", "extract"}),
    },
    "auth": frozenset({"can-i", "whoami"}),
    "image": frozenset({"info", "extract"}),
    "policy": POLICY_READ,
    "rollout": frozenset({"status", "history"}),
}
HELM_WRITE = frozenset({"install", "upgrade", "uninstall", "delete", "del", "un", "rollback"})
HELM_WRITE_NO_DRY_RUN = frozenset({"test", "push"})
HELM_READ = frozenset(
    {"template", "lint", "list", "ls", "status", "get", "show", "history", "search", "repo"}
    | {"pull", "fetch", "package", "dependency", "dep", "plugin", "env", "version", "verify"}
    | {"registry", "completion", "create", "help", "diff", "inspect"}
)
NO_DRY_RUN = frozenset({"attach", "cp", "debug", "exec", "rsh", "rsync"})
DRY_RUN_VALUES = frozenset({"client", "server", "true", "t", "1"})
OPERATOR_CHARS = frozenset("();|&<>")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
SHELL_WORD = re.compile(r"(?:^|[\s;&|(/])(?:bash|sh|zsh|dash|ksh|ssh)(?=[\s;&|)]|$)")


def _positional(args: list[str]) -> list[str]:
    """
    Return the positional words of a CLI call, skipping flags and global flag values.

    Args:
        args (list[str]): Tokens after the CLI name, up to any `--`.

    Returns:
        list[str]: The verb, subcommands and other positional words in order.
    """
    words: list[str] = []
    skip = False
    for tok in args:
        if skip:
            skip = False
        elif tok.startswith("-"):
            skip = "=" not in tok and tok in VALUE_FLAGS
        else:
            words.append(tok)
    return words


def _is_dry_run(args: list[str]) -> bool:
    """
    Return whether the flags make this a simulated change.

    Args:
        args (list[str]): Tokens after the CLI name.

    Returns:
        bool: True for `--dry-run` or `--dry-run=client|server|true`.
    """
    for arg in args:
        if arg == "--dry-run":
            return True
        if arg.startswith("--dry-run="):
            return arg.split("=", 1)[1].lower() in DRY_RUN_VALUES
    return False


def _tree_violation(node: object, rest: list[str]) -> str | None:
    """
    Walk a read-only subcommand tree and return the first subcommand outside it.

    Args:
        node (object): None (anything allowed), a frozenset of allowed leaves, or a dict.
        rest (list[str]): Remaining positional words.

    Returns:
        str | None: The offending subcommand path, or None if read-only or bare help.
    """
    if node is None or not rest:
        return None
    word = rest[0]
    if isinstance(node, frozenset):
        return None if word in node else word
    if not isinstance(node, dict) or word not in node:
        return word
    deeper = _tree_violation(node[word], rest[1:])
    return None if deeper is None else f"{word} {deeper}"


def _verb_violation(cli: str, words: list[str], dry_run: bool, shifted: bool = False) -> str | None:
    """
    Classify a CLI verb as a cluster write; an unknown first word is retried once on the next.

    Args:
        cli (str): `oc`, `kubectl` or `helm`.
        words (list[str]): Positional words after the CLI name.
        dry_run (bool): Whether the call is a dry run.
        shifted (bool): Whether an unknown leading word was already skipped.

    Returns:
        str | None: The offending command, or None.
    """
    if not words:
        return None
    verb = words[0]
    if cli == "helm":
        if verb in HELM_WRITE_NO_DRY_RUN or (verb in HELM_WRITE and not dry_run):
            return f"helm {verb}"
        known = verb in HELM_WRITE or verb in HELM_READ
    else:
        if verb in KUBE_WRITE:
            return None if dry_run and verb not in NO_DRY_RUN else f"{cli} {verb}"
        if verb in KUBE_READ_TREE:
            sub = _tree_violation(KUBE_READ_TREE[verb], words[1:])
            return None if sub is None or dry_run else f"{cli} {verb} {sub}"
        known = verb in KUBE_READ
    if known or shifted:
        return None
    return _verb_violation(cli, words[1:], dry_run, shifted=True)


def _cli_violation(cli: str, args: list[str]) -> str | None:
    """
    Describe the cluster write an oc/kubectl/helm call would make.

    Args:
        cli (str): `oc`, `kubectl` or `helm`.
        args (list[str]): Tokens after the CLI name.

    Returns:
        str | None: The offending command, or None if it is read-only or a dry run.
    """
    if "--" in args:
        args = args[: args.index("--")]
    return _verb_violation(cli, _positional(args), _is_dry_run(args))


def _segment_violation(tokens: list[str], anywhere: bool = False) -> str | None:
    """
    Check one simple command (no operators) for a cluster write.

    Args:
        tokens (list[str]): The command's words.
        anywhere (bool): Look for a CLI at any position, not only after wrappers.

    Returns:
        str | None: The offending command, or None.
    """
    i = 0
    while i < len(tokens) and (ASSIGNMENT.match(tokens[i]) or tokens[i] in KEYWORDS):
        i += 1
    if i >= len(tokens):
        return None
    head, rest = PurePosixPath(tokens[i]).name, tokens[i + 1 :]
    if head in CLIS:
        return _cli_violation(head, rest)
    if head in SHELLS:
        for j, tok in enumerate(rest[:-1]):
            if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", tok):
                return command_violation(rest[j + 1])
        return None
    if head in REMOTE:
        return command_violation(" ".join(rest), anywhere=True)
    if head in WRAPPERS or anywhere:
        for j, tok in enumerate(rest):
            name = PurePosixPath(tok).name
            if name in CLIS or name in SHELLS or name in REMOTE:
                return _segment_violation(rest[j:])
    return None


def _drop_heredoc_bodies(command: str) -> str:
    """
    Remove heredoc bodies, keeping those fed to a shell or ssh so they are scanned too.

    Args:
        command (str): The full Bash command.

    Returns:
        str: The command without inert heredoc text.
    """
    out: list[str] = []
    end_marker: str | None = None
    keep = False
    for line in command.split("\n"):
        if end_marker is not None:
            if line.strip() == end_marker:
                end_marker = None
            elif keep:
                out.append(line)
            continue
        out.append(line)
        match = HEREDOC.search(line)
        if match:
            end_marker = match.group(2)
            keep = bool(SHELL_WORD.search(line[: match.start()] + " " + line[match.end() :]))
    return "\n".join(out)


def _scan_quotes(text: str) -> tuple[str, list[str]]:
    """
    Strip shell comments and collect command substitutions, honouring quotes.

    Args:
        text (str): Shell text without heredoc bodies.

    Returns:
        tuple[str, list[str]]: The text without comments, and each `$(...)`/backtick body
        that the shell would execute (none from inside single quotes).
    """
    out: list[str] = []
    subs: list[str] = []
    quote: str | None = None
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote != "'" and ch == "\\" and i + 1 < n:
            out.append(text[i : i + 2])
            i += 2
            continue
        if quote is None and ch == "#" and (i == 0 or text[i - 1] in " \t\n;&|("):
            end = text.find("\n", i)
            i = n if end == -1 else end
            continue
        if quote != "'" and ch == "`":
            end = text.find("`", i + 1)
            end = n if end == -1 else end
            subs.append(text[i + 1 : end])
            out.append(text[i : end + 1])
            i = end + 1
            continue
        if quote != "'" and text.startswith("$(", i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            subs.append(text[i + 2 : j - 1])
        elif ch in "'\"" and quote in (None, ch):
            quote = None if quote else ch
        out.append(ch)
        i += 1
    return "".join(out), subs


def command_violation(command: str, anywhere: bool = False) -> str | None:
    """
    Return the first cluster write in a shell command line.

    Args:
        command (str): The Bash command.
        anywhere (bool): Look for a CLI at any position in each simple command.

    Returns:
        str | None: The offending command, or None if nothing writes to a cluster.
    """
    text, subs = _scan_quotes(_drop_heredoc_bodies(command.replace("\\\n", " ")))
    for sub in subs:
        hit = command_violation(sub)
        if hit:
            return hit
    text = text.replace("\n", " ; ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        tokens = text.split()
    segment: list[str] = []
    for tok in [*tokens, ";"]:
        if tok and set(tok) <= OPERATOR_CHARS:
            hit = _segment_violation(segment, anywhere)
            if hit:
                return hit
            segment = []
        else:
            segment.append(tok)
    return None


def _lab_servers(project_dir: Path) -> frozenset[str]:
    """
    API servers the user approved for lab writes, one URL per line in LAB_ALLOWLIST.

    Args:
        project_dir (Path): Project root.

    Returns:
        frozenset[str]: Approved server URLs (empty when the file is absent).
    """
    try:
        lines = (project_dir / LAB_ALLOWLIST).read_text().splitlines()
    except OSError:
        return frozenset()
    return frozenset(x.strip().rstrip("/") for x in lines if x.strip() and not x.startswith("#"))


def lab_write_allowed(command: str, project_dir: Path) -> bool:
    """
    Whether every cluster this command can reach is an approved lab cluster.

    True only if the command names its kubeconfig by literal absolute path (KUBECONFIG= or
    --kubeconfig), every server in each such file is approved, and nothing redirects elsewhere.

    Args:
        command (str): The Bash command.
        project_dir (Path): Project root (holds the allow-list).

    Returns:
        bool: True when the write may go ahead.
    """
    allowed = _lab_servers(project_dir)
    if not allowed or "--server" in command or "--context" in command:
        return False
    if re.search(r"(?:^|[\s;&|(])(?:ssh|sudo|su)\s", command):
        return False
    values = KUBECONFIG_ASSIGN.findall(command) + KUBECONFIG_FLAG.findall(command)
    if not values:
        return False
    for value in values:
        for path in value.strip("\"'").split(":"):
            if not path.startswith("/") or "$" in path or "`" in path:
                return False
            try:
                servers = {m.rstrip("/") for m in SERVER_LINE.findall(Path(path).read_text())}
            except OSError:
                return False
            if not servers or not servers <= allowed:
                return False
    return True


def main() -> None:
    """Deny the Bash call if it would write to a cluster; allow everything else."""
    try:
        payload = json.load(sys.stdin)
        command = str(payload.get("tool_input", {}).get("command", ""))
        hit = command_violation(command)
        project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
        if hit and lab_write_allowed(command, project):
            note = f"`{hit}` allowed: every server in its kubeconfig is in {LAB_ALLOWLIST}."
            print(json.dumps({"systemMessage": note}))
            return
    except Exception as exc:
        print(json.dumps({"systemMessage": f"block_cluster_writes.py failed open: {exc}"}))
        return
    if hit:
        reason = (
            f"`{hit}` changes cluster state, and this project is read-only toward clusters "
            "(AGENT.md §0.4, .claude/hooks/block_cluster_writes.py). Use a read-only command or "
            "--dry-run, or ask the user to run it themselves with `!`."
        )
        decision = {"hookEventName": "PreToolUse", "permissionDecision": "deny"}
        decision["permissionDecisionReason"] = reason
        print(json.dumps({"hookSpecificOutput": decision}))


if __name__ == "__main__":
    main()
