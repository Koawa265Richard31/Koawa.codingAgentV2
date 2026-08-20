"""D2 provider-neutral model protocol, stream assembly, and transport."""

from .openai_client import OpenAICompatibleChatClient
from .protocol import ModelRequest, ModelTurn
from .stream import ModelStreamAssembler, StreamLimits, assemble_model_stream

__all__ = [
    "ModelRequest",
    "ModelStreamAssembler",
    "ModelTurn",
    "OpenAICompatibleChatClient",
    "StreamLimits",
    "assemble_model_stream",
]
