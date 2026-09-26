#!/usr/bin/env python3
"""Shim: `parley validate` / parleylib.validate. Usage unchanged: tools/validate.py <dir|log.jsonl> [--report]."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parleylib.validate import main
sys.exit(main())
