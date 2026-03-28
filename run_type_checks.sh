#!/bin/bash
ty check aivm tests && mypy aivm tests --disallow-untyped-defs --disallow-incomplete-defs
