#!/bin/bash
# Pokreni Clade relay (foreground). Stop sa Ctrl+C.
cd "$(dirname "$0")/.."
exec .venv/bin/python relay/main.py
