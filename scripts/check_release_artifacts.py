"""Verify project identity and ensure removed execution code cannot ship."""
import json
import tarfile
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
wheels = list((root / "dist").glob("alf_control-*.whl"))
sdists = list((root / "dist").glob("alf_control-*.tar.gz"))
assert wheels and sdists, "Build Alf Control artifacts first"
for wheel in wheels:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "alf_control/server.py" in names
        assert "alf_control/ssh_sidecar.py" in names
        legacy = {p for p in names if p.startswith("agy_mcp/")}
        assert legacy <= {"agy_mcp/" + p for p in (
            "__init__.py", "__main__.py", "cli.py", "doctor.py", "ssh_sidecar.py", "stdio_reconnect.py")}
        metadata = archive.read(next(p for p in names if p.endswith(".dist-info/METADATA"))).decode()
        assert "Name: alf-control" in metadata
        assert not any("adapters/" in p or "device_agent" in p or "_skill_bodies" in p for p in names)
for sdist in sdists:
    with tarfile.open(sdist) as archive:
        names = archive.getnames()
        assert not any("/.venv/" in p or p.endswith("/backends.json") for p in names)
for manifest in ("plugin.json", ".codex-plugin/plugin.json"):
    data = json.loads((root / manifest).read_text())
    interface = data.get("interface") or data["extensions"]["com.openai"]["interface"]
    assert interface["displayName"] == "Alf Control"
print("Alf Control wheel, source archive and metadata verified")
