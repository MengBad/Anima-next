from _deploy import _parse_systemd_identity, _render_post_deploy_smoke


def test_parse_systemd_identity():
    assert _parse_systemd_identity("1234\n567890\n") == (1234, 567890)
    assert _parse_systemd_identity("invalid") == (0, 0)


def test_post_deploy_smoke_is_read_only_and_checks_critical_links():
    script = _render_post_deploy_smoke("/opt/anima")

    assert "/api/health" in script
    assert "/api/modules/status" in script
    assert "/api/evaluation" in script
    assert "channel_connected" in script
    assert "heartbeat_running" in script
    assert "_tool_runtime_status" in script
    assert "Authorization" in script
    assert "POST" not in script
    assert "DELETE" not in script
