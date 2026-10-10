"""Entry for ``python -m sysdroid`` and for the frozen SysDroid.exe (see SysDroid.spec).

The spec must not freeze the root sysdroid.py launcher: PyInstaller puts the entry
script's folder first on its search path, so ``import sysdroid`` would resolve to that
launcher module instead of this package and nothing of the app would be collected.
"""
from sysdroid.app import main

if __name__ == "__main__":
    raise SystemExit(main())
