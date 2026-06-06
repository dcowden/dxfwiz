# Offline CNC References

Downloaded for local reference while working without network access.

## Kiri:Moto / Grid.Space

- `grid-apps/` - Grid.Space source tree containing Kiri:Moto CAM code.
- `gridspace-kiri-engine-api.html` - Kiri engine browser API page when available.

Primary areas to inspect first:

- `grid-apps/src/kiri-mode/cam/`
- `grid-apps/src/kiri/`
- `grid-apps/src/geo/`

## Fusion / UCCNC Posts

- `autodesk-posts/uccnc.cps` - Autodesk Fusion UCCNC post as downloaded from the web page.
- `autodesk-posts/uccnc.raw.cps` - Extracted raw CPS source from the Autodesk wrapper page.
- `autodesk-posts/post-configuration-reference.html` - Autodesk post configuration reference.
- `avid-fusion-post-processor/` - Adjacent Fusion post source reference.
- `OpenBuilds-Fusion360-Postprocessor/` - Adjacent Fusion post source reference.
- `mpcnc_post_processor/` - Adjacent Fusion post examples and sample gcode.

## UCCNC

- `uccnc/` - UCCNC manuals and controller references.

The UCCNC manual download may need to be retried if `UCCNC_usersmanual.pdf` is missing; the vendor server sometimes drops TLS connections.
