from importlib.metadata import version, PackageNotFoundError

from ._server import CocotbDebugger, Command
from ._ast import wrap_code_as_async, format_exception
from ._client import start_sim
from ._config import CocotbDebugConfig

try:
    __version__ = version("cocodbg")
except PackageNotFoundError:
    __version__ = "unknown"

__all__ = [
    "CocotbDebugger",
    "Command",
    "wrap_code_as_async",
    "format_exception",
    "start_sim",
    "CocotbDebugConfig",
]
