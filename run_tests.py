#!/usr/bin/env python
"""Module for run tests."""

if __name__ == '__main__':
    import sys

    import pytest

    package_name = 'aivm'
    mod_dpath = 'aivm'
    test_dpath = 'tests'
    pytest_args = [
        '--cov-config',
        'pyproject.toml',
        '--cov-report',
        'html',
        '--cov-report',
        'term',
        '--xdoctest',
        '--cov=' + package_name,
        mod_dpath,
        test_dpath,
    ]
    pytest_args = pytest_args + sys.argv[1:]
    sys.exit(pytest.main(pytest_args))
