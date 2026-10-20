from __future__ import annotations

from pathlib import Path


def _load() -> tuple[str, int]:
    path = Path(__file__).resolve().parent.parent / "version.properties"
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, separator, value = line.partition("=")
            if not separator:
                raise RuntimeError("Некорректный version.properties")
            values[key.strip()] = value.strip()
    name = values.get("versionName", "")
    code = values.get("versionCode", "")
    if not name or not code.isdecimal() or int(code) < 1:
        raise RuntimeError("Некорректная версия приложения")
    return name, int(code)


APP_VERSION, VERSION_CODE = _load()
