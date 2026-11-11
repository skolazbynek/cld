"""Claude Code hook enforcing the anchor contract inside a container.

Run as ``python3 -P /opt/cld/cld/vcs/anchor_guard.py`` for ``PreToolUse`` (file tools
and Bash) and ``PostToolUse`` (Bash). See docs/design-anchor-guard.md.

Deny is JSON on stdout with exit 0. Any internal error propagates and exits 1,
which Claude Code treats as non-blocking (fail open): jj's own immutability
config is the floor.
"""

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass

DEFAULT_ANCHORS_FILE = "/tmp/cld-anchors.jsonl"
JJ_TIMEOUT = 10
WORKSPACE_ROOT = "/workspace"
FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

_PUNCTUATION = ";()<>|&\n"
_ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")
_SHELL_COMMAND_FLAG = re.compile(r"-[A-Za-z]*c")
_JJ_WORD = re.compile(r"\bjj\b")
_STRING_PATTERN_KINDS = {"exact", "glob", "substring", "regex"}

_GLOBAL_VALUE_FLAGS = {"-R", "--repository", "--config", "--config-file", "--at-op", "--at-operation", "--color"}
_VALUE_FLAGS = _GLOBAL_VALUE_FLAGS | {
    "-r", "--revision", "--revisions", "-o", "--onto", "-d", "--destination",
    "-A", "--insert-after", "--after", "-B", "--insert-before", "--before", "-b", "--branch", "-s", "--source",
    "-f", "--from", "-t", "--to", "--into", "-m", "--message", "--tool", "--file",
    "-T", "--template", "-n", "--limit",
}
_ONTO_FLAGS = ("-o", "--onto", "-d", "--destination")
_DESTINATION_FLAGS = (*_ONTO_FLAGS, "-A", "--insert-after", "--after")
_DESTINATION_COMMANDS = {"rebase", "duplicate", "revert", "split", "squash"}

_COMMAND_ALIASES = {"b": "bookmark", "operation": "op"}
_BOOKMARK_ALIASES = {
    "a": "advance", "c": "create", "d": "delete", "f": "forget", "m": "move", "r": "rename", "s": "set",
}
_READ_ONLY = {
    "log", "show", "diff", "status", "st", "evolog", "obslog", "interdiff", "help", "root", "version",
}
_READ_ONLY_NESTED = {
    "file": {"show", "list", "annotate", "search"},
    "op": {"log", "show", "diff"},
    "bookmark": {"list", "l"},
    "tag": {"list"},
    "config": {"get", "list", "path"},
    "workspace": {"list", "root"},
}


@dataclass(frozen=True)
class Anchor:
    repo: str
    workspace: str
    anchor: str
    mode: str
    backend: str


@dataclass
class JjCall:
    """Words are the subcommand path plus positionals; options keep their order."""

    words: list[str]
    options: list[tuple[str, str | None]]

    def has(self, *names: str) -> bool:
        return any(name in names for name, _ in self.options)

    def values(self, *names: str) -> list[str]:
        return [value for name, value in self.options if name in names and value is not None]


def load_anchors() -> list[Anchor]:
    path = os.environ.get("CLD_ANCHORS_FILE", DEFAULT_ANCHORS_FILE)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        records = [Anchor(**json.loads(line)) for line in f if line.strip()]
    return [Anchor(**{**vars(a), "workspace": os.path.realpath(a.workspace)}) for a in records]


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def find_workspace(path: str, anchors: list[Anchor]) -> Anchor | None:
    matches = [a for a in anchors if _inside(path, a.workspace)]
    return max(matches, key=lambda a: len(a.workspace), default=None)


def _jj(workspace: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["jj", "--ignore-working-copy", "-R", workspace, *args],
        capture_output=True, text=True, timeout=JJ_TIMEOUT,
    )


def _log(workspace: str, revset: str) -> subprocess.CompletedProcess[str]:
    return _jj(workspace, "log", "--no-graph", "--limit", "1", "-r", revset, "-T", "change_id.short()")


def _log_ids(workspace: str, revset: str) -> str:
    result = _log(workspace, revset)
    if result.returncode != 0:
        raise RuntimeError(f"jj log -r {revset!r} failed in {workspace}: {result.stderr.strip()}")
    return result.stdout.strip()


def _outside_parent_set(anchor: Anchor, revset: str) -> bool:
    """True if ``revset`` has a commit outside ``change_id(A)::``. A revset jj rejects is allowed:
    the real command fails the same way."""
    result = _log(anchor.workspace, f"({revset}) ~ change_id({anchor.anchor})::")
    return result.returncode == 0 and bool(result.stdout.strip())


def _touches_tree(anchor: Anchor, revset: str) -> bool:
    return bool(_log(anchor.workspace, f"({revset}) & change_id({anchor.anchor})::").stdout.strip())


def _outside_tree_reason(anchor: Anchor) -> str:
    return (
        f"`@` in {anchor.workspace} is outside the anchor tree (change_id({anchor.anchor})+::). "
        f"Run `jj new 'change_id({anchor.anchor})'` (or any commit in the tree) first."
    )


def _git_head_vs_anchor(anchor: Anchor) -> tuple[bool, bool]:
    """``(HEAD is the anchor or descends from it, HEAD is the anchor itself)`` in the workspace.
    Raises CalledProcessError when HEAD or the anchor does not resolve."""
    head = subprocess.run(
        ["git", "-C", anchor.workspace, "rev-parse", "HEAD", anchor.anchor],
        capture_output=True, text=True, timeout=JJ_TIMEOUT, check=True,
    ).stdout.split()
    ancestor = subprocess.run(
        ["git", "-C", anchor.workspace, "merge-base", "--is-ancestor", anchor.anchor, "HEAD"],
        capture_output=True, text=True, timeout=JJ_TIMEOUT,
    )
    if ancestor.returncode not in (0, 1):
        raise RuntimeError(f"git merge-base failed in {anchor.workspace}: {ancestor.stderr.strip()}")
    return ancestor.returncode == 0, head[0] == head[1]


def check_file(path: str, anchors: list[Anchor]) -> str | None:
    config_dir = os.path.realpath(os.path.expanduser("~/.config/jj/workspaces"))
    if _inside(path, config_dir) or any(_inside(path, os.path.join(a.workspace, ".jj")) for a in anchors):
        return (
            f"Rule 'jj internals': {path} holds the anchor guard's jj configuration or the "
            "workspace's link to the store. Do not edit it; use jj commands on your own commits instead."
        )
    anchor = find_workspace(path, anchors)
    if anchor is None:
        return None
    if anchor.backend == "jj":
        if _log_ids(anchor.workspace, f"@ & change_id({anchor.anchor})+::"):
            return None
        return f"Rule 'file write outside anchor tree': {_outside_tree_reason(anchor)}"
    in_tree, at_anchor = _git_head_vs_anchor(anchor)
    if in_tree and not at_anchor:
        return None
    return (
        f"Rule 'file write outside anchor tree': HEAD in {anchor.workspace} is not a strict descendant "
        f"of the anchor {anchor.anchor}. Check out a commit descending from it first."
    )


def _simple_commands(command: str, cwd: str) -> list[tuple[list[str], str]]:
    """Split a shell line into (words, directory) simple commands, tracking ``cd`` and
    subshell scope, and recursing into ``bash -c`` / ``sh -c`` strings."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    commands: list[tuple[list[str], str]] = []
    words: list[str] = []
    scopes: list[str] = []
    skip_word = False

    def flush() -> None:
        nonlocal words, cwd
        while words and _ASSIGNMENT.match(words[0]):
            words.pop(0)
        if words:
            program = os.path.basename(words[0])
            if program == "cd":
                target = os.path.expanduser(words[1]) if len(words) > 1 else "~"
                if target != "-":
                    cwd = os.path.realpath(os.path.join(cwd, os.path.expanduser(target)))
            elif program in ("bash", "sh"):
                for i, word in enumerate(words[1:-1], 1):
                    if _SHELL_COMMAND_FLAG.fullmatch(word):
                        commands.extend(_simple_commands(words[i + 1], cwd))
                        break
            commands.append((words, cwd))
        words = []

    for token in lexer:
        if not set(token) <= set(_PUNCTUATION):
            if skip_word:
                skip_word = False
            else:
                words.append(token)
            continue
        if set(token) & set("<>"):
            if words and words[-1].isdigit():
                words.pop()
            skip_word = True
            continue
        for char in token:
            flush()
            if char == "(":
                scopes.append(cwd)
            elif char == ")" and scopes:
                cwd = scopes.pop()
    flush()
    return commands


def _normalize(name: str) -> str:
    return _COMMAND_ALIASES.get(name, name)


def parse_jj(args: list[str]) -> JjCall:
    words: list[str] = []
    options: list[tuple[str, str | None]] = []
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        value_flags = _VALUE_FLAGS - {"-B"} if words and _normalize(words[0]) == "bookmark" else _VALUE_FLAGS
        if arg == "--":
            words += args[i:]
            break
        if arg.startswith("--"):
            name, equals, value = arg.partition("=")
            if equals:
                options.append((name, value))
            elif name in value_flags and i < len(args):
                options.append((name, args[i]))
                i += 1
            else:
                options.append((name, None))
        elif arg.startswith("-") and len(arg) > 1:
            if arg[:2] not in value_flags:
                options.append((arg, None))
            elif len(arg) > 2:
                options.append((arg[:2], arg[2:].removeprefix("=")))
            elif i < len(args):
                options.append((arg, args[i]))
                i += 1
            else:
                options.append((arg, None))
        else:
            words.append(arg)
    return JjCall(words, options)


def _is_read_only(call: JjCall) -> bool:
    if call.has("--help", "-h", "--version", "-V") or not call.words:
        return True
    command = _normalize(call.words[0])
    if command in _READ_ONLY:
        return True
    return len(call.words) > 1 and call.words[1] in _READ_ONLY_NESTED.get(command, set())


def _config_file_mentions_aliases(path: str, directory: str) -> bool:
    try:
        with open(os.path.join(directory, os.path.expanduser(path))) as f:
            return "revset-aliases" in f.read()
    except OSError:
        return False


def _bookmark_pattern(name: str) -> str:
    kind, colon, rest = name.partition(":")
    if colon and kind in _STRING_PATTERN_KINDS:
        return f"{kind}:{json.dumps(rest)}"
    return f"exact:{json.dumps(name)}"


def _destinations(call: JjCall, command: str) -> list[str]:
    if command == "new":
        return call.words[1:] + call.values(*_DESTINATION_FLAGS)
    if command in _DESTINATION_COMMANDS:
        return call.values(*_DESTINATION_FLAGS)
    return []


def check_jj(call: JjCall, directory: str, anchors: list[Anchor]) -> str | None:
    if call.has("--ignore-immutable"):
        return "Rule 'bypass flag': `--ignore-immutable` is not allowed. Do not rewrite commits outside your anchor tree."
    if any("revset-aliases" in v for v in call.values("--config")) or any(
        _config_file_mentions_aliases(v, directory) for v in call.values("--config-file")
    ):
        return "Rule 'bypass flag': `--config`/`--config-file` must not touch `revset-aliases`. Drop the option."

    command = _normalize(call.words[0]) if call.words else ""
    sub = call.words[1] if len(call.words) > 1 else ""
    if command == "config" and sub in ("set", "unset", "edit") and (
        call.has("--workspace") or any("revset-aliases" in w for w in call.words[2:])
    ):
        return "Rule 'config tamper': workspace config and `revset-aliases` are managed by the container. Do not change them."
    if command == "workspace" and sub in ("add", "forget", "rename"):
        return f"Rule 'workspace ops': `jj workspace {sub}` is not allowed. Work in the existing workspace."

    repository = call.values("-R", "--repository")
    if repository:
        directory = os.path.realpath(os.path.join(directory, os.path.expanduser(repository[-1])))
    anchor = find_workspace(directory, anchors)
    if anchor is None:
        # Outside /workspace the directory may not be a jj repo at all, where jj just fails.
        if _inside(directory, WORKSPACE_ROOT) and not _is_read_only(call):
            return (
                f"Rule 'foreign workspace': {directory} is not a registered workspace of this container, "
                "so the anchor immutability config does not apply there. Run only read-only jj commands "
                "there, or work in your own workspace."
            )
        return None
    if anchor.backend != "jj":
        return None

    outside = (
        f"outside the anchor tree: it must be `change_id({anchor.anchor})` or a descendant. "
        f"Use a commit in that tree, e.g. `change_id({anchor.anchor})` or `@`."
    )
    # A rebased commit gets every `--onto` revision as a parent, so one in the tree keeps it
    # in the tree: `jj rebase -b 'A::' -o 'A | trunk()'` (the rebase skill) must pass.
    mixed_ok = set(call.values(*_ONTO_FLAGS)) if command == "rebase" else set()
    for revset in _destinations(call, command):
        if _outside_parent_set(anchor, revset) and not (revset in mixed_ok and _touches_tree(anchor, revset)):
            return f"Rule 'destination': `{revset}` is {outside}"

    if command == "bookmark":
        action = _BOOKMARK_ALIASES.get(sub, sub)
        if action in ("create", "set", "move", "advance"):
            for revset in call.values("-r", "--revision", "-t", "--to", "-f", "--from"):
                if _outside_parent_set(anchor, revset):
                    return f"Rule 'bookmark target': `{revset}` is {outside}"
        if action in ("delete", "forget", "rename", "move", "set", "advance"):
            names = call.words[2:3] if action == "rename" else call.words[2:]
            for name in names:
                if _outside_parent_set(anchor, f"bookmarks({_bookmark_pattern(name)})"):
                    return (
                        f"Rule 'bookmark ownership': bookmark `{name}` points outside the anchor tree, "
                        "so it is not this container's ref. Leave it alone."
                    )
    return None


def check_bash(command: str, cwd: str, anchors: list[Anchor]) -> str | None:
    try:
        commands = _simple_commands(command, cwd)
    except ValueError:
        # shlex rejects lines bash accepts (an apostrophe in a comment or heredoc body, a trailing
        # backslash). Without jj in the line there is nothing to check; with it, fail closed.
        if not _JJ_WORD.search(command):
            return None
        return (
            "Rule 'unparsable command': the anchor guard could not split this line into words, so it cannot "
            "check its jj commands. Run the jj command on its own, without heredocs or comments containing "
            "unbalanced quotes."
        )
    for words, directory in commands:
        if os.path.basename(words[0]) != "jj":
            continue
        if reason := check_jj(parse_jj(words[1:]), directory, anchors):
            return reason
    return None


def _git_drift(anchor: Anchor) -> list[str]:
    """Unlike ``check_file``, HEAD at the anchor is no drift: a git workspace starts there
    (no scratch commit), and only HEAD off the anchor's descendants is outside the tree."""
    try:
        in_tree, _ = _git_head_vs_anchor(anchor)
    except (subprocess.CalledProcessError, RuntimeError):
        return [
            f"{anchor.repo}: HEAD or the anchor {anchor.anchor} no longer resolves. "
            "Stop and report this; do not write files."
        ]
    if in_tree:
        return []
    return [
        f"{anchor.repo}: HEAD in {anchor.workspace} is outside the anchor tree: the anchor "
        f"{anchor.anchor} is not an ancestor of it. Check out a commit descending from it first."
    ]


def check_drift(anchors: list[Anchor]) -> str | None:
    problems = []
    for anchor in anchors:
        if anchor.backend != "jj":
            problems.extend(_git_drift(anchor))
            continue
        if not _log_ids(anchor.workspace, f"change_id({anchor.anchor})"):
            problems.append(
                f"{anchor.repo}: the anchor change {anchor.anchor} no longer resolves. "
                "Stop and report this; do not write files."
            )
        elif not _log_ids(anchor.workspace, f"@ & change_id({anchor.anchor})+::"):
            problems.append(f"{anchor.repo}: {_outside_tree_reason(anchor)}")
    return "\n".join(problems) or None


def main() -> None:
    anchors = load_anchors()
    if not anchors:
        return
    data = json.load(sys.stdin)
    event, tool = data.get("hook_event_name"), data.get("tool_name")
    cwd = os.path.realpath(data.get("cwd") or os.getcwd())
    tool_input = data.get("tool_input") or {}

    if event == "PreToolUse":
        if tool in FILE_TOOLS:
            file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
            reason = check_file(os.path.realpath(os.path.join(cwd, file_path)), anchors) if file_path else None
        elif tool == "Bash":
            reason = check_bash(tool_input.get("command", ""), cwd, anchors)
        else:
            reason = None
        if reason:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }}))
    elif event == "PostToolUse" and tool == "Bash":
        if reason := check_drift(anchors):
            print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    main()
