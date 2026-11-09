# Third-party runtime notices

The source application has no third-party Python runtime dependencies. A frozen
Windows or macOS distribution includes the Python runtime.

- CPython: Python Software Foundation license and bundled notices.
  <https://docs.python.org/3/license.html>
- PyInstaller bootloader: GPL with the PyInstaller bootloader exception.
  <https://pyinstaller.org/en/stable/license.html>

Release ZIPs include copies in `licenses/`; retain them when redistributing it.
Build/check dependencies and their versions are listed in `requirements-build.txt`.
They are not all bundled into the application.

The default HTML interface uses an installed system browser (preferably Microsoft
Edge on Windows), under that browser's own license. No browser binary, third-party
JavaScript framework, external font, or CDN asset is redistributed. HTML, CSS,
JavaScript and SVG assets in `ui/` are original project code under the MIT License.
Node.js is required only for developer browser acceptance, not for end users.

The project's own source is licensed under the MIT License in `LICENSE`.
The MIT License does not replace the above components' individual license terms.
