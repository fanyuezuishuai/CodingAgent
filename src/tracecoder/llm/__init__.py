"""Language-model adapters."""

from tracecoder.llm.base import ModelClient, ModelProtocolError
from tracecoder.llm.openai_compatible import OpenAICompatibleClient

__all__ = ["ModelClient", "ModelProtocolError", "OpenAICompatibleClient"]
