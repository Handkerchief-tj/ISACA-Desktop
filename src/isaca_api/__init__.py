"""Unified local API for netlists, schematics and circuit analyses.

Import ``isaca_api.app`` explicitly to create the FastAPI application. Keeping
the package initializer side-effect free prevents utility imports from creating
analysis directories or initializing SLiCAP global state.
"""
