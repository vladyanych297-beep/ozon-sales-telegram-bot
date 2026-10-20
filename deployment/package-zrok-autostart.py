"""Package only the tunnel launcher and installer; never include credentials."""
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parent.parent
names = ("ozon_forecast/windows_start.py", "deployment/Install-ZrokAutostart.ps1")
output = root / ".local/zrok-autostart-transfer/ozon-zrok-autostart-0.3.1.zip"
output.parent.mkdir(parents=True, exist_ok=True)
with ZipFile(output, "w", ZIP_DEFLATED) as archive:
    for name in names:
        data = (root / name).read_bytes()
        data.decode("utf-8")
        if data.startswith(b"\xef\xbb\xbf"):
            raise ValueError(f"Unexpected UTF-8 BOM: {name}")
        archive.writestr(name, data)
with ZipFile(output) as archive:
    assert archive.namelist() == list(names)
    assert archive.testzip() is None
    for name in names:
        assert archive.read(name) == (root / name).read_bytes()
print(output)
print(f"bytes={output.stat().st_size}")
print(f"sha256={sha256(output.read_bytes()).hexdigest()}")
