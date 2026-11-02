"""LM Agent — локальный LLM-агент с инструментами на базе Ollama."""

from .agent import Agent, ToolEvent
from .config import Settings
from .tools import ToolRegistry

__all__ = ["Agent", "Settings", "ToolEvent", "ToolRegistry"]
