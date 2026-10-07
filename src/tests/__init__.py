"""Test suite.

Kept as a package (rather than relying on rootdir discovery) so that
test modules can use relative imports if they need to share fixtures
and helpers. Pytest works fine either way; the package layout is the
more conservative choice.
"""
