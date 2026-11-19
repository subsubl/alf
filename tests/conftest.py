"""Pytest bootstrap configuration for dual-runtime offline testing."""

from tests._stubs import ensure_repo_root, install_dependency_stubs

ensure_repo_root()
install_dependency_stubs()
