"""Персистентное состояние сервиса — /data/state.json.

/data — стандартная персистентная директория, которую HA Supervisor выделяет
каждому add-on по умолчанию (без явного `map:` в config.yaml); переживает
рестарт и попадает в бэкапы HA. Для локальной разработки see docker-compose.yml
(volume ./data:/data).
"""
import json
import os
from pathlib import Path

STATE_PATH = Path(os.environ.get("IS74_STATE_PATH", "/data/state.json"))


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    with STATE_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def clear_state() -> None:
    if STATE_PATH.exists():
        STATE_PATH.unlink()
