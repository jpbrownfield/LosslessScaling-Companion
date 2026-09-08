"""
Root launcher for LS Companion.
Run: python run_companion.py
"""

import sys
import os

# Add workspace directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from companion.main import main

if __name__ == "__main__":
    main()
