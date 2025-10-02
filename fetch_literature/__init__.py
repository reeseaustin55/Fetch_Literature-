"""Fetch Literature application package."""

from .browser_automation import ensure_playwright_setup
from .fetcher_app import FetcherApp

__all__ = ["FetcherApp", "ensure_playwright_setup"]
