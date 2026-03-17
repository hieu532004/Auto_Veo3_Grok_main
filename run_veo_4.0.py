from __future__ import annotations
import sys
from pathlib import Path
from branding_config import APP_VERSION, WINDOW_TITLE
import License
def _ensure_project_root() -> None:
    # ***<module>._ensure_project_root: Failure: Different control flow
    root = Path(__file__).resolve().parent
def main() -> None:
    _ensure_project_root()
    print(f'[RUNNER] Start launcher for {WINDOW_TITLE} - {APP_VERSION}')
    License.main()
if __name__ == '__main__':
    main()