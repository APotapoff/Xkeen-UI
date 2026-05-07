"""Tests for managed Mihomo Xray-JSON subscription refreshes."""

from __future__ import annotations

import json

from mihomo_config_generator import build_full_config
from services import mihomo_subscriptions as svc


def _outbound(address: str, uuid: str):
    return {
        "tag": "proxy",
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": address,
                    "port": 443,
                    "users": [{"id": uuid, "encryption": "none"}],
                }
            ]
        },
        "streamSettings": {
            "network": "tcp",
            "security": "tls",
            "tlsSettings": {"serverName": "edge.example.com", "fingerprint": "chrome"},
        },
    }


def _subscription_body(remarks: str, address: str, uuid: str) -> str:
    return json.dumps(
        [
            {
                "remarks": remarks,
                "outbounds": [
                    _outbound(address, uuid),
                    {"protocol": "freedom", "tag": "direct"},
                ],
            }
        ]
    )


def _state_with_managed_yaml(yaml_text: str):
    return {
        "profile": "router_custom",
        "subscriptions": [],
        "defaultGroups": ["Заблок. сервисы"],
        "enabledRuleGroups": ["Blocked"],
        "proxies": [
            {
                "kind": "yaml",
                "yaml": yaml_text,
                "tags": ["xray-sub:example.test"],
                "xray_json_subscription": {
                    "id": "xray-example",
                    "url": "https://example.test/xray.json",
                    "tag": "xray-sub:example.test",
                    "enabled": True,
                    "interval_hours": 24,
                },
            }
        ],
    }


def test_sync_from_generator_state_stores_managed_subscription(tmp_path):
    state = _state_with_managed_yaml("- name: Old\n  type: vless\n")
    saved = svc.sync_from_generator_state(str(tmp_path), state, config_text="config")

    subs = saved["subscriptions"]
    assert len(subs) == 1
    assert subs[0]["id"] == "xray-example"
    assert subs[0]["url"] == "https://example.test/xray.json"
    assert subs[0]["next_update_ts"]
    proxy = saved["generator_state"]["proxies"][0]
    assert proxy["xray_json_subscription"]["id"] == "xray-example"
    assert "xray-sub:example.test" in proxy["tags"]


def test_refresh_subscription_replaces_managed_yaml_and_restarts(tmp_path, monkeypatch):
    old_body = _subscription_body("Old Node", "1.1.1.1", "11111111-1111-1111-1111-111111111111")
    old_proxies, _ = svc.convert_subscription_text(old_body)
    old_yaml = "\n\n".join(p.yaml.strip() for p in old_proxies)
    state = _state_with_managed_yaml(old_yaml)
    current_config = build_full_config(state)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(current_config, encoding="utf-8")
    svc.sync_from_generator_state(str(tmp_path), state, config_text=current_config)

    new_body = _subscription_body("New Node", "2.2.2.2", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setattr(svc, "fetch_subscription_body", lambda url: (new_body, {}))

    restarts: list[str] = []

    def _restart(*, source="api"):
        restarts.append(source)

    def _save(text: str):
        config_path.write_text(text, encoding="utf-8")

    result = svc.refresh_subscription(
        str(tmp_path),
        "xray-example",
        mihomo_config_file=str(config_path),
        restart_xkeen=_restart,
        save_callback=_save,
    )

    assert result["ok"] is True
    assert result["changed"] is True
    assert result["count"] == 1
    assert restarts == ["mihomo-subscription-refresh"]
    written = config_path.read_text(encoding="utf-8")
    assert "New Node" in written
    assert "2.2.2.2" in written
    saved = svc.load_subscription_state(str(tmp_path))
    assert saved["subscriptions"][0]["last_ok"] is True
    assert saved["subscriptions"][0]["last_count"] == 1
    assert "New Node" in saved["generator_state"]["proxies"][0]["yaml"]


def test_refresh_subscription_refuses_to_clobber_manual_active_config(tmp_path, monkeypatch):
    old_body = _subscription_body("Old Node", "1.1.1.1", "11111111-1111-1111-1111-111111111111")
    old_proxies, _ = svc.convert_subscription_text(old_body)
    old_yaml = "\n\n".join(p.yaml.strip() for p in old_proxies)
    state = _state_with_managed_yaml(old_yaml)
    current_config = build_full_config(state)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(current_config, encoding="utf-8")
    svc.sync_from_generator_state(str(tmp_path), state, config_text=current_config)
    config_path.write_text("manual edit\n", encoding="utf-8")

    def _fail_fetch(url):
        raise AssertionError("refresh should stop before fetching")

    monkeypatch.setattr(svc, "fetch_subscription_body", _fail_fetch)

    result = svc.refresh_subscription(
        str(tmp_path),
        "xray-example",
        mihomo_config_file=str(config_path),
    )

    assert result["ok"] is False
    assert result["error"] == "active_config_changed"
    assert config_path.read_text(encoding="utf-8") == "manual edit\n"


def test_update_subscription_settings_clamps_interval_and_updates_generator_meta(tmp_path):
    state = _state_with_managed_yaml("- name: Old\n  type: vless\n")
    saved = svc.sync_from_generator_state(str(tmp_path), state, config_text="config")
    old_next = saved["subscriptions"][0]["next_update_ts"]

    updated = svc.update_subscription_settings(
        str(tmp_path),
        "xray-example",
        {"interval_hours": 999},
    )

    assert updated["interval_hours"] == 168
    assert updated["next_update_ts"] != old_next

    saved = svc.load_subscription_state(str(tmp_path))
    assert saved["subscriptions"][0]["interval_hours"] == 168
    proxy_meta = saved["generator_state"]["proxies"][0]["xray_json_subscription"]
    assert proxy_meta["interval_hours"] == 168
