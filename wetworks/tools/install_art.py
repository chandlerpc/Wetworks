"""Install the Retro Cybercity Corporate tileset for the Floor.

The pack is licensed to Patrick and may not be redistributed, so it is never committed: this copies
the sheets the Floor uses from the purchased archive (or an extracted folder) into
wetworks/console/art/, which git ignores.

    python3 -m wetworks.tools.install_art ~/Downloads/RCCcorpoTILESETrevamped.7z
"""

import shutil
import sys
import tempfile
from pathlib import Path

from .un7z import main as extract

WANT = {  # file the Floor loads <- file in the pack (native 16x16 interior sheets)
    "interior_A5.png": "corpointeriorA5.png",
    "interior_B.png": "corpointeriorBprops.png",
    "interior_C.png": "corpointeriorCprops.png",
    "interior_D.png": "corpointeriorDprops.png",
    "interior_E.png": "corpointeriorEvariantprops.png",
}
DEST = Path(__file__).resolve().parent.parent / "console" / "art"


def install(source: Path, dest: Path = DEST) -> list[str]:
    if not source.exists():
        hint = " Files downloaded in Windows are under /mnt/c/Users/<you>/Downloads." if str(source).startswith(str(Path.home())) else ""
        raise SystemExit(f"File not found: {source}.{hint}")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        if source.is_file():
            extract(str(source), tmp)
        else:
            root = source
        found = {}
        for p in root.rglob("*.png"):
            if "native" in str(p.parent) and "interior" in str(p.parent) and p.name in WANT.values():
                found[p.name] = p
        missing = [v for v in WANT.values() if v not in found]
        if missing:
            raise SystemExit(f"Couldn't find {', '.join(missing)} in {source}. Is this the Retro Cybercity Corporate pack?")
        dest.mkdir(parents=True, exist_ok=True)
        for ours, theirs in WANT.items():
            shutil.copyfile(found[theirs], dest / ours)
    return sorted(WANT)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    names = install(Path(sys.argv[1]).expanduser())
    print(f"Installed {len(names)} sheets into {DEST}. Rebuild: docker compose up -d --build")
