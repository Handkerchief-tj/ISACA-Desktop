# Integration Notes

The original prototype was first developed inside a local SLiCAP installation by
adding:

- `SLiCAPnetwork.py`
- `SLiCAPsfg.py`
- extra exports in `SLiCAP.py`

The standalone repository version intentionally avoids modifying the installed
SLiCAP package:

- `src/sfg_prototype/network.py` contains the flattened-network adapter.
- `src/sfg_prototype/sfg.py` contains the SFG construction code.
- `src/sfg_prototype/__init__.py` exposes the public API.

The standalone version still depends on official SLiCAP internals for parsing and
flattening, but it does not require users to copy files into `site-packages`.
