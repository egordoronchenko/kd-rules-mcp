"""Базовый compose разбирается без Docker и поддерживает оба пути запуска."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_image_and_build() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["kd-rules-mcp"]
    assert service["image"] == "ghcr.io/egordoronchenko/kd-rules-mcp:${KD_IMAGE_TAG:-latest}"
    assert service["build"] == "."
    assert compose["name"] == "${COMPOSE_PROJECT_NAME:-kd-rules-mcp}"
