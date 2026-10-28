# Third-party notices

PTKit is an independent toolkit. Cisco Packet Tracer is a Cisco product and is
not included with the toolkit. No Cisco installer, IOS image, activity file or
Packet Tracer sample is included in the source or release bundles.

The Windows executable bundles Python, Tcl/Tk, lxml and supporting runtime
libraries. It is packaged with the PyInstaller bootloader. The release builder
copies the installed distributions' license notices into `licenses/`, both in
the executable and in the Windows release ZIP. Preserve that folder when sharing
the release. It includes Python's runtime notices, lxml's notices, Tcl/Tk terms
when present in the Python installation, and PyInstaller's bootloader terms.

Development-only tools such as pytest, Pillow and PyCryptodome are not required
by people running the executable. Their licenses remain with their distributions.

PTKit's own licensable contributions are distributed under the MIT License in
`LICENSE`, with the public copyright notice "Sponge and contributors". This does
not assign other contributors' rights to the publisher or claim rights to Cisco
products or third-party components. Those retain their existing notices and
licenses. See `RESPONSIBLE_USE.md` for the responsibility and risk notice.
