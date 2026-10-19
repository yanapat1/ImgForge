#!/usr/bin/env python3
"""
Dataset visualizer for YOLO datasets — launcher. The code lives in the dataviz/ package (see README.md).

Usage (from the project root):
    .venv/bin/python scripts/image-tools/app.py --dataset dataset --port 5151
    .venv/bin/python scripts/image-tools/app.py --help
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # makes `dataviz` importable from anywhere

from dataviz.cli import main  # noqa: E402

if __name__ == '__main__':
    main()
