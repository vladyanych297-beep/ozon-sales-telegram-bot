"""Sign a release APK locally; never include signing material in the transfer package."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    version = dict(
        line.split("=", 1)
        for line in (root / "version.properties").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )["versionName"]
    parser = argparse.ArgumentParser()
    parser.add_argument("--jdk", type=Path, required=True)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--keys", type=Path, default=root / ".local/signing")
    args = parser.parse_args()
    source = root / f"outputs/android/ozon-plan-{version}-release-unsigned.apk"
    output = root / f"outputs/android/ozon-plan-{version}.apk"
    if not source.is_file():
        raise SystemExit("Build the release APK first")
    args.keys.mkdir(parents=True, exist_ok=True)
    config = args.keys / "signing.json"
    store = args.keys / "ozon-plan.p12"
    if not config.exists():
        if store.exists():
            raise SystemExit(
                "Signing key exists but its configuration is missing; do not overwrite it"
            )
        config.write_text(
            json.dumps({"alias": "ozon-plan", "password": secrets.token_urlsafe(32)}),
            encoding="utf-8",
        )
    settings = json.loads(config.read_text(encoding="utf-8"))
    environment = dict(os.environ, OZON_SIGN_PASS=settings["password"])
    if not store.exists():
        subprocess.run(
            [
                str(args.jdk / "bin/keytool.exe"),
                "-genkeypair",
                "-keystore",
                str(store),
                "-storetype",
                "PKCS12",
                "-alias",
                settings["alias"],
                "-keyalg",
                "RSA",
                "-keysize",
                "3072",
                "-validity",
                "10000",
                "-dname",
                "CN=Ozon Plan",
                "-storepass:env",
                "OZON_SIGN_PASS",
                "-keypass:env",
                "OZON_SIGN_PASS",
            ],
            env=environment,
            check=True,
        )
    tool = args.sdk / "build-tools/36.1.0/lib/apksigner.jar"
    subprocess.run(
        [
            str(args.jdk / "bin/java.exe"),
            "-jar",
            str(tool),
            "sign",
            "--ks",
            str(store),
            "--ks-key-alias",
            settings["alias"],
            "--ks-pass",
            "env:OZON_SIGN_PASS",
            "--key-pass",
            "env:OZON_SIGN_PASS",
            "--out",
            str(output),
            str(source),
        ],
        env=environment,
        check=True,
    )
    subprocess.run(
        [str(args.jdk / "bin/java.exe"), "-jar", str(tool), "verify", "--verbose", str(output)],
        check=True,
    )
    print(f"Signed APK: {output}")
    print(f"Keep a private backup of {args.keys}. Never distribute it with the APK.")


if __name__ == "__main__":
    main()
