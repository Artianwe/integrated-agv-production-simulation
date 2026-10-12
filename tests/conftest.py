import os
import sys

# Make the project modules importable when pytest is run from the project root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
