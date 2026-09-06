# Double-click launcher — .pyw runs under pythonw.exe, so no console window opens.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from export_dub_gui import main

main()
