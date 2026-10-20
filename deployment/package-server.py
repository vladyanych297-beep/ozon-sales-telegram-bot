"""Build an allowlisted server transfer package without credentials or live data."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parent.parent
version = dict(
    line.split("=", 1)
    for line in (root / "version.properties").read_text(encoding="utf-8").splitlines()
    if line and not line.startswith("#")
)["versionName"]
output = root / f"outputs/android/ozon-server-{version}.zip"
apk = root / f"outputs/android/ozon-plan-{version}.apk"
if not apk.is_file():
    raise SystemExit(f"Signed APK missing: {apk}")
files = [
    root / "version.properties",
    root / "UPDATE-SERVER.cmd",
    root / "requirements-server.txt",
    root / "requirements.txt",
    root / ".env.example",
    root / "deployment/Start-ForecastServer.ps1",
    root / "deployment/Install-ForecastAutostart.ps1",
    root / "deployment/Apply-ServerUpdate.ps1",
    root / "docs/ANDROID.md",
    root / "docs/ANDROID-VERIFICATION.md",
    *sorted((root / "ozon_sales_bot").glob("*.py")),
    *sorted((root / "ozon_forecast").glob("*.py")),
]
output.parent.mkdir(parents=True, exist_ok=True)
with ZipFile(output, "w", ZIP_DEFLATED) as archive:
    for file in files:
        archive.write(file, file.relative_to(root).as_posix())
    archive.write(apk, "updates/ozon-plan.apk")
print(output)
