"""Publish a GDTF Share mirror. See docs/GDTF_SHARE.md."""
import argparse
import fcntl
import hashlib
import http.cookiejar
import io
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

BASE = "https://gdtf-share.com/apis/public/"
LIMIT = 50_000_000


def publish(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        try:
            out.write(data)
            out.flush()
            os.chmod(out.name, 0o644)
            os.replace(out.name, path)
        finally:
            Path(out.name).unlink(missing_ok=True)


def names(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.getinfo("description.xml")
        if info.file_size > 8_000_000:
            raise ValueError("XML demasiado grande")
        xml = archive.read(info)
        if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper() or b"\0" in xml:
            raise ValueError("XML no compatible")
        fixture = ET.fromstring(xml).find("FixtureType")
        if fixture is None:
            raise ValueError("FixtureType ausente")
        return {key: fixture.get(key, "") for key in ("Name", "LongName")}


def sync(root, selected):
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(path, payload=None):
        request = urllib.request.Request(BASE + path, data=payload, headers={"Content-Type": "application/json"})
        with client.open(request, timeout=60) as response:
            data = response.read(LIMIT + 1)
            if len(data) > LIMIT:
                raise ValueError("La respuesta supera el límite")
            return data

    credentials = {"user": os.environ["GDTF_USER"], "password": os.environ["GDTF_PASSWORD"]}
    if json.loads(get("login.php", json.dumps(credentials).encode())).get("result") is not True:
        raise RuntimeError("Acceso GDTF rechazado")
    listing = json.loads(get("getList.php"))
    if listing.get("result") is not True:
        raise RuntimeError("Catálogo GDTF no disponible")
    index = root / "index.json"
    previous = json.loads(index.read_bytes())["fixtures"] if index.exists() else []
    known = {row["rid"]: row for row in previous}
    rows = []
    revisions = listing["list"]
    available = {int(item["rid"]) for item in revisions}
    for rid in sorted(selected - available):
        print(f"La API no incluye la revisión {rid}.", flush=True)
    # The initial Pro L connection test uses an older Robin Tetra2 revision.
    if selected == {76822} and 76822 not in available:
        alternatives = [item for item in revisions
                        if "robe" in item["manufacturer"].casefold()
                        and item["fixture"].casefold().replace(" ", "") in {"robintetra2", "tetra2"}
                        and int(item["filesize"]) <= LIMIT]
        if alternatives:
            latest = max(alternatives, key=lambda item: (item["uploader"] == "Manuf.", int(item["lastModified"])))
            selected = {int(latest["rid"])}
            print(f'Prueba con la revisión disponible del Tetra2: {latest["rid"]}', flush=True)
    for item in revisions:
        rid = int(item["rid"])
        if selected and rid not in selected:
            continue
        if int(item["filesize"]) > LIMIT:
            print(f"Revisión {rid} omitida por superar 50 MB.", flush=True)
            continue
        old = known.get(rid)
        stamp = str(item["lastModified"])
        if old and old["modified"] == stamp and (root / old["file"]).is_file():
            metadata = {key: old[key] for key in ("Name", "LongName", "file")}
        else:
            data = get(f"downloadFile.php?rid={rid}")
            try:
                metadata = names(data)
            except (zipfile.BadZipFile, KeyError, ValueError, ET.ParseError) as error:
                print(f"Revisión {rid} omitida: {error}", flush=True)
                continue
            filename = f'{rid}-{hashlib.sha256(data).hexdigest()}.gdtf'
            publish(root / filename, data)
            metadata["file"] = filename
        rows.append(dict(metadata, rid=rid, modified=stamp, **{k: item[k] for k in
                         ("manufacturer", "fixture", "revision", "uploader")}))
    if not rows:
        candidates = [item for item in revisions if item["uploader"] == "Manuf."
                      and 0 < int(item["filesize"]) < 2_000_000]
        for item in candidates[:5]:
            print(f'Alternativa disponible: {item["rid"]} {item["manufacturer"]} {item["fixture"]}')
        raise RuntimeError("Ningún perfil importable. Revisa los identificadores indicados arriba.")
    publish(index, json.dumps({"updated": int(time.time()), "fixtures": rows}).encode())
    active = {row["file"] for row in rows}
    for row in previous:
        if row["file"] not in active:
            (root / row["file"]).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--rid", type=int, action="append", help="Sincronizar solo estas revisiones")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    with (args.directory / ".sync.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sync(args.directory, set(args.rid or []))
