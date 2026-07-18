"""Configure sys.path so that tests can import nodes.py directly."""

import sys
import os

# Add the repository root to sys.path so ``import nodes`` resolves correctly
# when pytest is invoked from any working directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
