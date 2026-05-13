"""Guard tests: v0.37.2 retired symbols must NOT come back accidentally.

If a future refactor restores any of these without a deliberate design
change, this file fails — forcing a conscious decision."""

from __future__ import annotations

import pytest


class TestRetiredSymbols:
    def test_handle_permission_request_gone(self):
        from channels import casa_engagement_channel as ch
        assert not hasattr(ch, "handle_permission_request")

    def test_drain_permission_verdicts_gone(self):
        from channels import casa_engagement_channel as ch
        assert not hasattr(ch, "_drain_permission_verdicts")

    def test_emit_permission_notification_gone(self):
        from channels import casa_engagement_channel as ch
        assert not hasattr(ch, "_emit_permission_notification")

    def test_permission_request_notification_class_gone(self):
        from channels import casa_engagement_channel as ch
        assert not hasattr(ch, "PermissionRequestNotification")

    def test_permission_verdict_notification_class_gone(self):
        from channels import casa_engagement_channel as ch
        assert not hasattr(ch, "PermissionVerdictNotification")

    def test_widen_session_gone(self):
        from channels import casa_engagement_channel as ch
        assert not hasattr(ch, "_widen_session_notification_type")

    def test_declared_capabilities_no_permission(self):
        from channels.casa_engagement_channel import declared_capabilities
        assert "claude/channel/permission" not in declared_capabilities()
