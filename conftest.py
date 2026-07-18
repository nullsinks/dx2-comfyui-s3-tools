"""Root conftest: add repository root to sys.path for test imports."""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
