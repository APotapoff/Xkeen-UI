from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "xkeen-ui"

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


from core.mihomo_paths import init_mihomo_paths


def test_mihomo_templates_init_migrates_legacy_hwid_template_name(tmp_path, monkeypatch):
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    legacy = templates_dir / "hwid_subscription_template.yaml"
    legacy.write_text("proxy-groups: []\n", encoding="utf-8")

    monkeypatch.setenv("MIHOMO_TEMPLATES_DIR", str(templates_dir))

    init_mihomo_paths(str(tmp_path / "config.yaml"))

    assert (templates_dir / "template.yaml").is_file()
    assert not legacy.exists()
