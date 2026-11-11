from pathlib import Path

from ozon_forecast.api import app
from ozon_forecast.version import APP_VERSION, VERSION_CODE


def test_single_version_source():
    assert APP_VERSION == "0.7.8"
    assert VERSION_CODE == 34
    assert app.version == APP_VERSION
    gradle = Path("android/app/build.gradle.kts").read_text(encoding="utf-8")
    assert 'versionName = appVersion.getProperty("versionName")' in gradle
    assert 'versionCode = appVersion.getProperty("versionCode").toInt()' in gradle
