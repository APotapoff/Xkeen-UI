"""Tests for multi-proxy YAML-kind support in the Mihomo generator.

Driven by the Xray-JSON subscription import flow: a converted subscription is
fed in as a single ``kind: yaml`` proxy item, but the YAML payload contains N
proxies that must each be registered in proxy-groups.
"""

from __future__ import annotations

import yaml

from services.mihomo_generator_proxies import (
    _split_multi_proxy_yaml,
    insert_proxies_from_state,
)


def test_split_multi_proxy_yaml_handles_single_proxy():
    block = (
        "- name: A\n"
        "  type: vless\n"
        "  server: 1.1.1.1\n"
        "  port: 443\n"
    )
    parts = _split_multi_proxy_yaml(block)
    assert len(parts) == 1
    assert parts[0].lstrip().startswith("- name: A")


def test_split_multi_proxy_yaml_separates_top_level_dashes():
    block = (
        "- name: A\n"
        "  type: vless\n"
        "  alpn:\n"
        "    - h2\n"
        "    - http/1.1\n"
        "- name: B\n"
        "  type: vless\n"
    )
    parts = _split_multi_proxy_yaml(block)
    assert len(parts) == 2
    assert "- name: A" in parts[0] and "- name: B" not in parts[0]
    assert "- name: B" in parts[1]
    # Inner list dashes (alpn) must stay with the first block, not split it.
    assert "- h2" in parts[0]
    assert "- http/1.1" in parts[0]


def test_split_multi_proxy_yaml_returns_empty_on_blank_input():
    assert _split_multi_proxy_yaml("") == []
    assert _split_multi_proxy_yaml("   \n\n") == []


def test_yaml_kind_with_multi_proxy_registers_every_proxy_in_group():
    template = (
        "proxy-groups:\n"
        "  - name: Selector\n"
        "    type: select\n"
        "    include-all: true\n"
        "\n"
        "rule-providers:\n"
        "  some: ~\n"
    )
    multi = (
        "- name: A\n"
        "  type: vless\n"
        "  server: 1.1.1.1\n"
        "  port: 443\n"
        "  uuid: aaaa\n"
        "- name: B\n"
        "  type: vless\n"
        "  server: 2.2.2.2\n"
        "  port: 443\n"
        "  uuid: bbbb\n"
        "- name: C\n"
        "  type: vless\n"
        "  server: 3.3.3.3\n"
        "  port: 443\n"
        "  uuid: cccc\n"
    )
    state = {
        "proxies": [
            {"kind": "yaml", "yaml": multi, "groups": ["Selector"]},
        ]
    }
    result = insert_proxies_from_state(template, state)
    parsed = yaml.safe_load(result)

    assert {p["name"] for p in parsed["proxies"]} == {"A", "B", "C"}
    assert "uuid: aaaa\n\n  - name: B" in result
    assert "uuid: bbbb\n\n  - name: C" in result
    selector = next(g for g in parsed["proxy-groups"] if g["name"] == "Selector")
    assert set(selector.get("proxies") or []) == {"A", "B", "C"}


def test_yaml_kind_with_single_proxy_still_works_after_split_helper():
    """Regression: single-proxy yaml-kind should not change behaviour."""
    template = (
        "proxy-groups:\n"
        "  - name: G\n"
        "    type: select\n"
        "    include-all: true\n"
        "\n"
        "rule-providers:\n"
        "  some: ~\n"
    )
    single = (
        "- name: Solo\n"
        "  type: vless\n"
        "  server: 9.9.9.9\n"
        "  port: 443\n"
        "  uuid: dddd\n"
    )
    state = {"proxies": [{"kind": "yaml", "yaml": single, "groups": ["G"]}]}
    result = insert_proxies_from_state(template, state)
    parsed = yaml.safe_load(result)

    assert [p["name"] for p in parsed["proxies"]] == ["Solo"]
    g = next(g for g in parsed["proxy-groups"] if g["name"] == "G")
    assert g.get("proxies") == ["Solo"]


def test_yaml_kind_multi_proxy_raises_on_duplicate_names():
    template = (
        "proxy-groups:\n"
        "  - name: G\n"
        "    type: select\n"
        "    include-all: true\n"
        "\n"
        "rule-providers:\n"
        "  some: ~\n"
    )
    dup = (
        "- name: Dup\n"
        "  type: vless\n"
        "  server: 1.1.1.1\n"
        "  port: 443\n"
        "  uuid: aaaa\n"
        "- name: Dup\n"
        "  type: vless\n"
        "  server: 2.2.2.2\n"
        "  port: 443\n"
        "  uuid: bbbb\n"
    )
    state = {"proxies": [{"kind": "yaml", "yaml": dup, "groups": ["G"]}]}
    try:
        insert_proxies_from_state(template, state)
    except ValueError as exc:
        assert "Дублирующееся имя" in str(exc)
    else:
        raise AssertionError("expected ValueError for duplicate names")
