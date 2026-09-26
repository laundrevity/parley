#!/usr/bin/env python3
"""Shim: `parley gen-docs` / parleylib.gendocs."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parleylib.gendocs import main
sys.exit(main())
