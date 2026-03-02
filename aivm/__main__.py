#!/usr/bin/env python3
"""Executable module entry point for `python -m aivm`."""

# PYTHON_ARGCOMPLETE_OK

if __name__ == '__main__':
    from .cli import main

    main()
