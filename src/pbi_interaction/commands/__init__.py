"""Registered CLI commands."""

from . import build_xlsx, export, list_, probe
from .base import Command

COMMANDS: list[Command] = [probe.COMMAND, list_.COMMAND, export.COMMAND, build_xlsx.COMMAND]
