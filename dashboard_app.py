"""Shared Flask `app` object for the decoupled dashboard.

Both dashboard_module1.py and dashboard_module2.py import `app` from here and
register their routes on it. Kept intentionally tiny: the two route modules are
independent files, so editing one can never overwrite the other's routes — the
whole-file-overwrite risk the old combined dashboard.py warned about is gone.
"""
import logging
from flask import Flask

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
app.secret_key = "module1-local"   # local single-user tool; fine for localhost
