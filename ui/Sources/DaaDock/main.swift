import AppKit
import DaaDockUI

// Everything with a pixel in it lives in `DaaDockUI`, so the snapshot renderer
// (`Sources/DaaDockSnapshots`) can draw the same views offscreen. This file is
// only the process entry point.
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
