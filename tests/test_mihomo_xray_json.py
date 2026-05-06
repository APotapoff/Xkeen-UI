"""Tests for the Xray-JSON → Mihomo proxy converter."""

from __future__ import annotations

import json

import pytest
import yaml

from services.mihomo_xray_json import (
    convert_outbound_to_mihomo,
    convert_subscription_text,
    format_proxies_section,
)


def _outbound_vless_xhttp_tls():
    return {
        "tag": "proxy",
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": "1.2.3.4",
                    "port": 443,
                    "users": [{"id": "11111111-1111-1111-1111-111111111111", "encryption": "none"}],
                }
            ]
        },
        "streamSettings": {
            "network": "xhttp",
            "security": "tls",
            "tlsSettings": {
                "serverName": "edge.example.com",
                "fingerprint": "random",
                "alpn": ["h2", "http/1.1"],
                "allowInsecure": False,
            },
            "xhttpSettings": {"path": "/api/v2/", "mode": "auto", "host": ""},
        },
    }


def _outbound_vless_tcp_reality_vision(short_id="ab12cd34"):
    return {
        "tag": "proxy",
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": "5.6.7.8",
                    "port": 443,
                    "users": [
                        {
                            "id": "22222222-2222-2222-2222-222222222222",
                            "encryption": "none",
                            "flow": "xtls-rprx-vision",
                        }
                    ],
                }
            ]
        },
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "serverName": "www.microsoft.com",
                "fingerprint": "chrome",
                "publicKey": "z7ObaBEwG9lXYX2JPQsFNWIXcH25ywpLIf4_g9LgSX4",
                "shortId": short_id,
            },
        },
    }


def _full_xray_config(remarks, outbound):
    """Wrap an outbound in a full Xray config (matching real subscription format)."""
    return {
        "remarks": remarks,
        "log": {"loglevel": "warning"},
        "dns": {"servers": ["8.8.8.8"]},
        "routing": {"domainStrategy": "AsIs", "rules": []},
        "inbounds": [],
        "outbounds": [
            outbound,
            {"protocol": "freedom", "tag": "direct"},
            {"protocol": "blackhole", "tag": "block"},
        ],
    }


def test_convert_vless_xhttp_tls_emits_expected_yaml():
    result = convert_outbound_to_mihomo(_outbound_vless_xhttp_tls(), "Germany")
    assert result is not None
    parsed = yaml.safe_load(result.yaml)
    assert isinstance(parsed, list) and len(parsed) == 1
    p = parsed[0]
    assert p["name"] == "Germany"
    assert p["type"] == "vless"
    assert p["server"] == "1.2.3.4"
    assert p["port"] == 443
    assert p["uuid"] == "11111111-1111-1111-1111-111111111111"
    assert p["network"] == "xhttp"
    assert p["tls"] is True
    assert p["servername"] == "edge.example.com"
    assert p["alpn"] == ["h2", "http/1.1"]
    assert p["xhttp-opts"]["path"] == "/api/v2/"
    assert p["xhttp-opts"]["mode"] == "auto"
    # encryption: "" must remain a string, not get parsed as null
    assert p["encryption"] == ""


def test_convert_vless_tcp_reality_vision_emits_expected_yaml():
    result = convert_outbound_to_mihomo(_outbound_vless_tcp_reality_vision(), "Reality-Node")
    assert result is not None
    parsed = yaml.safe_load(result.yaml)[0]
    assert parsed["name"] == "Reality-Node"
    assert parsed["network"] == "tcp"
    assert parsed["tls"] is True
    assert parsed["flow"] == "xtls-rprx-vision"
    assert parsed["servername"] == "www.microsoft.com"
    ro = parsed["reality-opts"]
    assert ro["public-key"] == "z7ObaBEwG9lXYX2JPQsFNWIXcH25ywpLIf4_g9LgSX4"
    # short-id roundtrip preserves string type even for hex-with-letters values
    assert ro["short-id"] == "ab12cd34"
    assert isinstance(ro["short-id"], str)


@pytest.mark.parametrize("short_id", ["28000000", "12345", "0"])
def test_numeric_looking_short_ids_stay_strings(short_id):
    """Reality short-id values that look numeric must round-trip as strings."""
    outbound = _outbound_vless_tcp_reality_vision(short_id=short_id)
    result = convert_outbound_to_mihomo(outbound, "n")
    assert result is not None
    parsed = yaml.safe_load(result.yaml)[0]
    sid = parsed["reality-opts"]["short-id"]
    assert isinstance(sid, str), f"expected str, got {type(sid).__name__}: {sid!r}"
    assert sid == short_id


def test_convert_outbound_returns_none_for_unsupported_protocol():
    outbound = {"protocol": "shadowsocks", "settings": {}, "streamSettings": {}}
    assert convert_outbound_to_mihomo(outbound, "x") is None


def test_convert_subscription_text_parses_real_world_array_shape():
    body = json.dumps(
        [
            _full_xray_config("Germany", _outbound_vless_xhttp_tls()),
            _full_xray_config("Reality-Node", _outbound_vless_tcp_reality_vision()),
        ]
    )
    proxies, skipped = convert_subscription_text(body)
    assert len(proxies) == 2
    assert skipped == []
    assert proxies[0].name == "Germany"
    assert proxies[1].name == "Reality-Node"


def test_convert_subscription_text_dedupes_against_existing_names():
    body = json.dumps(
        [
            _full_xray_config("Germany", _outbound_vless_xhttp_tls()),
            _full_xray_config("Germany", _outbound_vless_tcp_reality_vision()),
        ]
    )
    proxies, _ = convert_subscription_text(body, existing_names=["Germany"])
    # First duplicates against the existing name → "_2", second against the
    # newly-added "Germany_2" → "_3" (because "Germany" is already taken).
    assert proxies[0].name == "Germany_2"
    assert proxies[1].name == "Germany_3"


def test_convert_subscription_text_raises_for_non_json_body():
    with pytest.raises(ValueError, match="not_xray_json"):
        convert_subscription_text("<html>not json</html>")


def test_convert_subscription_text_raises_for_share_link_body():
    with pytest.raises(ValueError, match="not_xray_json"):
        convert_subscription_text("vless://abc@1.2.3.4:443\nvless://def@5.6.7.8:443\n")


def test_convert_subscription_text_skips_unsupported_outbounds():
    body = json.dumps(
        [
            _full_xray_config("ok", _outbound_vless_xhttp_tls()),
            _full_xray_config(
                "ss",
                {
                    "tag": "proxy",
                    "protocol": "shadowsocks",
                    "settings": {"servers": [{"address": "x", "port": 443}]},
                },
            ),
        ]
    )
    proxies, skipped = convert_subscription_text(body)
    assert len(proxies) == 1
    assert proxies[0].name == "ok"
    assert len(skipped) == 1
    assert "shadowsocks" in skipped[0]["reason"]


def test_format_proxies_section_indents_each_block():
    body = json.dumps(
        [
            _full_xray_config("A", _outbound_vless_xhttp_tls()),
            _full_xray_config("B", _outbound_vless_tcp_reality_vision()),
        ]
    )
    proxies, _ = convert_subscription_text(body)
    text = format_proxies_section(proxies)
    parsed = yaml.safe_load(text)
    assert "proxies" in parsed
    assert len(parsed["proxies"]) == 2
    assert {p["name"] for p in parsed["proxies"]} == {"A", "B"}
    assert "mode: auto\n\n  - name: B" in text
