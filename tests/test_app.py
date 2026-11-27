"""Tests del lanzador de la app de escritorio (fase 8b)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shutil  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from sanitizer.app import client_configs, new_config, project_root  # noqa: E402
from sanitizer.rules import load_config  # noqa: E402

CONFIGS = Path(__file__).parent.parent / "configs"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "configs").mkdir()
    for name in ("base.yaml", "example.yaml"):
        shutil.copyfile(CONFIGS / name, tmp_path / "configs" / name)
    return tmp_path


def test_new_config_inherits_and_is_listed(root):
    path = new_config(root, "Cliente Uno")
    assert path.name == "cliente-uno.yaml"
    cfg = load_config(path)
    assert cfg.name == "Cliente Uno" and cfg.substitutions
    assert client_configs(root) == [path]  # base y example no son editables
    with pytest.raises(ValueError):
        new_config(root, "cliente uno")
    with pytest.raises(ValueError):
        new_config(root, "Example")


def test_project_root_from_source():
    assert (project_root() / "configs" / "base.yaml").exists()
