"""Copy this file into the WSGI configuration shown in PythonAnywhere's Web tab."""

import os
import sys
from pathlib import Path

project = Path.home() / "jemchuzhena_bot"
sys.path.insert(0, str(project))
os.chdir(project)

from pearl.web import create_app

application = create_app()
