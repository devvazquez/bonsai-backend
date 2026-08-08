"""Posa l'arrel del repositori al path perquè `tests/` pugui importar `app`.

Així no cal instal·lar el paquet ni definir PYTHONPATH per córrer les proves.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
