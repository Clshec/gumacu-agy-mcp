"""Direct transport readiness command."""
import asyncio
import json

from alf_control.server import alf_doctor


def main():
    report = asyncio.run(alf_doctor())
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["success"] else 1)

if __name__ == "__main__":
    main()
