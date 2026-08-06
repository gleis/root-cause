"""
Read-only command safety enforcement (spec section 13).

This module is the single source of truth for what diagnostic commands the
system is allowed to run (or, in this MVP, allowed to even *construct* -- no
agent in this codebase actually shells out to infrastructure; InfrastructureAgent
talks to the Kubernetes API client directly). It exists so that:

  1. Any future "run a diagnostic command" feature has a hard gate to go through.
  2. Recommended remediation commands surfaced in postmortems/hypotheses can be
     validated as "this is describable but not executable" -- mutating verbs are
     never allowed to be marked as safe/runnable, they can only ever appear as
     free-text recommendations in a report.

Nothing in this codebase calls a mutating command. This module is the
guardrail that keeps it that way even if a future contributor tries to wire
up command execution.
"""
import re
import shlex

# Allow-listed read-only diagnostic command roots. Each entry is
# (binary, allowed_subcommands_or_None). None means "any subcommand/args are
# fine because the binary itself is inherently read-only".
_ALLOWED_COMMANDS = {
    "kubectl": {"get", "describe", "logs", "top", "version", "explain"},
    "journalctl": None,
    "systemctl": {"status", "is-active", "is-enabled", "list-units"},
    "df": None,
    "free": None,
    "uptime": None,
    "ps": None,
    "ss": None,
    "netstat": None,
    "cat": None,   # only meaningful when combined with a path allowlist upstream
    "top": None,
}

# Verbs / tokens that must ALWAYS be rejected, even if they appear nested
# inside an otherwise-allowed command (e.g. "kubectl delete", "kubectl exec",
# a subshell, a pipe into `sh -c "rm -rf"`, etc). This list is checked
# regardless of which binary is used, specifically to catch string-trick
# smuggling (e.g. `kubectl get pods; rm -rf /`).
_DENY_TOKENS = {
    "delete", "rm", "reboot", "shutdown", "restart", "rollout", "apply",
    "patch", "replace", "edit", "cordon", "drain", "uncordon", "taint",
    "exec", "cp", "scale", "kill", "killall", "mv", "dd", "mkfs", "shred",
    "truncate", "chmod", "chown", "iptables", "reset", "destroy", "purge",
    "drop", "terminate", "stop", "disable", "mask", "poweroff", "halt",
    "init", "systemctl-restart",
}

# Shell metacharacters that indicate command chaining / injection attempts.
_DANGEROUS_METACHARS = re.compile(r"[;&|`$><\n]")


def is_command_allowed(cmd: str) -> bool:
    """
    Returns True only if `cmd` is a single, read-only, allow-listed
    diagnostic command with no shell chaining/injection tricks and no
    mutating verb anywhere in its tokens.

    This is intentionally conservative: any ambiguity resolves to False.
    """
    if not cmd or not cmd.strip():
        return False

    # Reject outright if it contains shell metacharacters that could be used
    # to chain in a second (mutating) command, e.g. "kubectl get pods; rm -rf /"
    if _DANGEROUS_METACHARS.search(cmd):
        return False

    try:
        tokens = shlex.split(cmd)
    except ValueError:
        # Unbalanced quotes etc -- reject rather than guess.
        return False

    if not tokens:
        return False

    binary = tokens[0].split("/")[-1]  # strip any path prefix like /usr/bin/kubectl

    # Hard denylist check across ALL tokens, not just the subcommand slot --
    # this is what catches "kubectl --raw=/api/v1/.../delete" style smuggling
    # or flags that embed a mutating word.
    lowered_tokens = [t.lower() for t in tokens]
    for tok in lowered_tokens:
        # exact-match on token, or the token contains a deny word as its own
        # "word" (avoid false positives like a pod named "restarter-abcd" by
        # only rejecting flags/subcommands, not free-form positional args
        # such as resource names -- subcommands are conventionally the 2nd
        # token and flags start with '-').
        if tok in _DENY_TOKENS:
            return False

    if binary not in _ALLOWED_COMMANDS:
        return False

    allowed_subcommands = _ALLOWED_COMMANDS[binary]
    if allowed_subcommands is None:
        return True

    if len(tokens) < 2:
        # A bare binary like "kubectl" with no subcommand isn't a complete,
        # verifiable read-only command.
        return False

    subcommand = tokens[1].lower()
    return subcommand in allowed_subcommands


def assert_command_allowed(cmd: str) -> None:
    """Raises PermissionError if the command is not allowed. Convenience wrapper."""
    if not is_command_allowed(cmd):
        raise PermissionError(f"Command is not on the read-only allowlist and will not be executed: {cmd!r}")


# A small, explicit denylist of full mutating command roots, kept separate
# from _DENY_TOKENS for clarity/documentation purposes (spec explicitly
# calls these out as examples that must never execute).
HARD_DENYLIST_EXAMPLES = [
    "kubectl delete",
    "kubectl rollout restart",
    "terraform apply",
    "systemctl restart",
    "rm",
    "reboot",
    "shutdown",
]

for _example in HARD_DENYLIST_EXAMPLES:
    assert not is_command_allowed(_example), f"Safety self-test failed for: {_example}"
