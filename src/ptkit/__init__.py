"""ptkit - read, inspect, edit and write Cisco Packet Tracer .pkt/.pka files."""

from .codec import CodecError, Format, decode, decode_file, encode, encode_file
from .iosconfig import IosConfig
from .model import Activity, Device, Link, Network, Port, PTFile
from .xmlio import XmlDoc, XmlFidelityError

__version__ = "0.2.1"

__all__ = [
    "Activity", "CodecError", "Device", "Format", "IosConfig", "Link", "Network",
    "PTFile", "Port", "XmlDoc", "XmlFidelityError", "decode", "decode_file",
    "encode", "encode_file", "__version__",
]
