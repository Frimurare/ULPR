"""Prudencia LPR - nummerplåtsläsning till OpenEye Event API."""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get('LPR_DATA', os.path.join(BASE_DIR, 'data'))

try:
    with open(os.path.join(BASE_DIR, 'VERSION')) as _f:
        VERSION = _f.read().strip()
except OSError:
    VERSION = '0.0.0'

PRODUCT = 'Prudencia LPR'
