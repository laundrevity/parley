#!/usr/bin/env python3
"""Shim: `parley project` / parleylib.project. Usage unchanged: tools/project.py --for ID [--since ID] ..."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parleylib.project import main
sys.exit(main())
