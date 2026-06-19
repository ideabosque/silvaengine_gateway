#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Entry point for ``python -m silvaengine_gateway`` and the
``silvaengine-gateway`` console script (pyproject ``__main__:main``)."""

from .app import run_gateway


def main() -> None:
    run_gateway()


if __name__ == "__main__":
    main()
