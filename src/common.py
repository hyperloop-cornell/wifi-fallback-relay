from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from dotenv import load_dotenv


@dataclass(frozen=True)
class RetryPolicy:
    base_seconds: float = 1.0
    max_seconds: float = 60.0
    jitter_ratio: float = 0.30

    def delay_for(self, attempt: int) -> float:
        safe_attempt = max(1, attempt)
        base_delay = min(self.base_seconds * (2 ** (safe_attempt - 1)), self.max_seconds)
        jitter = random.uniform(0.0, base_delay * self.jitter_ratio)
        return min(base_delay + jitter, self.max_seconds)


def load_env(env_file: Optional[str] = None) -> None:
    if env_file:
        load_dotenv(env_file, override=False)
    else:
        load_dotenv(override=False)


def setup_logging(level: str) -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def get_env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()


def get_env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def get_env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def get_env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def read_env_value(env_file: Path, key: str) -> Optional[str]:
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(f"{key}="):
            return stripped.split("=", 1)[1].strip()
    return None


def upsert_env_value(env_file: Path, key: str, value: str) -> bool:
    env_file.parent.mkdir(parents=True, exist_ok=True)

    if env_file.exists():
        lines = env_file.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    target = f"{key}={value}"
    changed = False
    replaced = False

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            replaced = True
            if line != target:
                lines[idx] = target
                changed = True
            break

    if not replaced:
        lines.append(target)
        changed = True

    if changed:
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return changed


def dedupe_ordered(items: Iterable[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
