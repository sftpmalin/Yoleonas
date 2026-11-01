#!/usr/bin/env python3
"""Worker détaché : ne dépend pas de la durée de vie du navigateur/Gunicorn."""
import backup

if __name__ == '__main__':
    backup.run_selection_queue()
